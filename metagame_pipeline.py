"""每日赛事使用率流水线：抓取 MTGO 官方 Challenge + 每日 League 5-0 牌表
（主数据源），加上 MTGTop8 线下 2 星以上赛事牌表（补充数据源），统计
Modern / Legacy（次要：Standard / Pauper）里各张卡的使用率。

运行方式：python metagame_pipeline.py
节奏：每天跑一次。每次统计的是"截至今天的过去 7 天滚动窗口"（不是自然周），
这样每天都能拿到一个样本量足够（一周的量）、又比等一整周更新更快的快照，
方便尽快积累出能做趋势/相关性分析的历史序列。

数据源合规性：
- mtgo.com/decklists 官方牌表，第一方数据，无 robots.txt 限制。
- mtgtop8.com 线下（非 MTGO）2 星以上赛事，无 robots.txt 限制，纯服务端
  渲染 HTML。只抓每个赛制星级最高的几场，控制请求量。
- 不碰 mtggoldfish.com（robots.txt 明确 Disallow ClaudeBot）。
"""

import json
from datetime import date, datetime, timedelta

import storage
from config import DATA_DIR
from fetchers import mtgo_metagame_fetcher, mtgtop8_fetcher

FORMATS = ("modern", "legacy", "standard", "pauper")
TOP8_FORMATS = ("modern", "legacy")  # 线下补充数据源只做这两个赛制，优先级最高
TOP8_MAX_EVENTS_PER_FORMAT = 3  # 每个赛制只抓星级最高的几场，控制请求量
ROLLING_WINDOW_DAYS = 7
EXPORT_PATH = DATA_DIR / "metagame_export.json"


def fetch_mtgo_official(snapshot_key: str, cutoff: str):
    print("[mtgo] fetching recent event index ...")
    events = mtgo_metagame_fetcher.fetch_recent_events(only_formats=FORMATS)

    def event_date(e):
        import re
        m = re.search(r"(\d{4}-\d{2}-\d{2})", e["slug"])
        return m.group(1) if m else "0000-00-00"

    events = [e for e in events if event_date(e) >= cutoff]
    print(f"[mtgo] {len(events)} events since {cutoff}")

    usage_by_format = {fmt: {} for fmt in FORMATS}
    sample_by_format = {fmt: 0 for fmt in FORMATS}

    for i, e in enumerate(events):
        try:
            data = mtgo_metagame_fetcher.fetch_event_decklists(e["url"])
        except Exception as exc:
            print(f"  [skip] {e['slug']}: {exc}")
            continue
        decks = data["decks"]
        if not decks:
            continue
        fmt = e["format"]
        sample_by_format[fmt] += len(decks)
        for deck in decks:
            for name in deck["cards"]:
                usage_by_format[fmt][name] = usage_by_format[fmt].get(name, 0) + 1
        print(f"  [{i+1}/{len(events)}] {e['slug']} -> {len(decks)} decks")

    return usage_by_format, sample_by_format


def fetch_mtgtop8_offline():
    usage_by_format = {fmt: {} for fmt in TOP8_FORMATS}
    sample_by_format = {fmt: 0 for fmt in TOP8_FORMATS}

    for fmt in TOP8_FORMATS:
        print(f"[mtgtop8] fetching offline 2+ star events for {fmt} ...")
        try:
            events = mtgtop8_fetcher.fetch_offline_events(fmt, min_stars=2)
        except Exception as exc:
            print(f"  [skip format {fmt}] {exc}")
            continue
        events.sort(key=lambda e: -e["stars"])
        events = events[:TOP8_MAX_EVENTS_PER_FORMAT]
        print(f"[mtgtop8] {fmt}: using top {len(events)} events by star rating")

        for e in events:
            try:
                deck_ids = mtgtop8_fetcher.fetch_event_deck_ids(e["url"])
            except Exception as exc:
                print(f"  [skip event {e['event_id']}] {exc}")
                continue
            got = 0
            for did in deck_ids:
                try:
                    cards = mtgtop8_fetcher.fetch_deck_cards(e["event_id"], did, fmt)
                except Exception:
                    continue
                if not cards:
                    continue
                sample_by_format[fmt] += 1
                got += 1
                for name in cards:
                    usage_by_format[fmt][name] = usage_by_format[fmt].get(name, 0) + 1
            print(f"  [{fmt}] event {e['event_id']} ({e['title']}, {e['stars']}★) -> {got}/{len(deck_ids)} decks")

    return usage_by_format, sample_by_format


def run():
    today = date.today()
    snapshot_key = today.isoformat()  # 存进 metagame_usage 表的 week_of 字段，语义是"快照日期"
    cutoff = (today - timedelta(days=ROLLING_WINDOW_DAYS)).isoformat()

    with storage.connect() as conn:
        mtgo_usage, mtgo_sample = fetch_mtgo_official(snapshot_key, cutoff)
        top8_usage, top8_sample = fetch_mtgtop8_offline()

        total_rows = 0
        export = {}
        for fmt in FORMATS:
            usage = dict(mtgo_usage[fmt])
            usage["__sample_size__"] = mtgo_sample[fmt]
            n = storage.upsert_metagame_usage(conn, snapshot_key, fmt, "mtgo_official", usage)
            total_rows += n
            print(f"[storage] {fmt}/mtgo_official: {n} card rows, sample_size={mtgo_sample[fmt]}")

            fmt_export = []
            if mtgo_sample[fmt] > 0:
                fmt_export.append({"week_of": snapshot_key, "source": "mtgo_official",
                                    "sample_size": mtgo_sample[fmt], "usage": mtgo_usage[fmt]})

            if fmt in TOP8_FORMATS and top8_sample.get(fmt, 0) > 0:
                usage2 = dict(top8_usage[fmt])
                usage2["__sample_size__"] = top8_sample[fmt]
                n2 = storage.upsert_metagame_usage(conn, snapshot_key, fmt, "mtgtop8_offline", usage2)
                total_rows += n2
                print(f"[storage] {fmt}/mtgtop8_offline: {n2} card rows, sample_size={top8_sample[fmt]}")
                fmt_export.append({"week_of": snapshot_key, "source": "mtgtop8_offline",
                                    "sample_size": top8_sample[fmt], "usage": top8_usage[fmt]})

            if fmt_export:
                export[fmt] = fmt_export

        storage.set_meta(conn, "metagame_last_run", datetime.now().isoformat())

        # 导出今天这份快照，供云端 routine 读取后 write_db 到 Artifact 仪表盘数据库
        # （本地 SQLite 在云端每次运行都是全新的，靠这个文件桥接到仪表盘那边持久化）。
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        EXPORT_PATH.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[done] snapshot_date={snapshot_key}, total rows written={total_rows}, exported to {EXPORT_PATH}")


if __name__ == "__main__":
    run()
