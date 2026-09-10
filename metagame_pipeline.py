"""每日赛事使用率流水线：抓取 MTGO 官方 Challenge + 每日 League 5-0 牌表，
统计 Modern / Legacy（次要：Standard / Pauper）里各张卡的使用率。

运行方式：python metagame_pipeline.py
节奏：每天跑一次。每次统计的是"截至今天的过去 7 天滚动窗口"（不是自然周），
这样每天都能拿到一个样本量足够（一周的量）、又比等一整周更新更快的快照，
方便尽快积累出能做趋势/相关性分析的历史序列。

数据源：mtgo.com/decklists 官方牌表（第一方数据，不抓第三方聚合站点）。
MTGTop8 线下 2 星以上赛事作为补充数据源，另见 mtgtop8_fetcher.py。
"""

import json
from datetime import date, datetime, timedelta

import storage
from config import DATA_DIR
from fetchers import mtgo_metagame_fetcher

FORMATS = ("modern", "legacy", "standard", "pauper")
ROLLING_WINDOW_DAYS = 7
EXPORT_PATH = DATA_DIR / "metagame_export.json"


def run():
    today = date.today()
    snapshot_key = today.isoformat()  # 存进 metagame_usage 表的 week_of 字段，语义是"快照日期"
    cutoff = (today - timedelta(days=ROLLING_WINDOW_DAYS)).isoformat()

    with storage.connect() as conn:
        print("[mtgo] fetching recent event index ...")
        events = mtgo_metagame_fetcher.fetch_recent_events(only_formats=FORMATS)

        def event_date(e):
            import re
            m = re.search(r"(\d{4}-\d{2}-\d{2})", e["slug"])
            return m.group(1) if m else "0000-00-00"

        events = [e for e in events if event_date(e) >= cutoff]
        print(f"[mtgo] {len(events)} events since {cutoff} (rolling {ROLLING_WINDOW_DAYS}-day window ending {snapshot_key})")

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

        total_rows = 0
        for fmt in FORMATS:
            usage = usage_by_format[fmt]
            usage["__sample_size__"] = sample_by_format[fmt]
            n = storage.upsert_metagame_usage(conn, snapshot_key, fmt, "mtgo_official", usage)
            total_rows += n
            print(f"[storage] {fmt}: {n} card rows, sample_size={sample_by_format[fmt]}")

        storage.set_meta(conn, "metagame_last_run", datetime.now().isoformat())

        # 导出今天这份快照，供云端 routine 读取后 write_db 到 Artifact 仪表盘数据库
        # （本地 SQLite 在云端每次运行都是全新的，靠这个文件桥接到仪表盘那边持久化）。
        export = {
            fmt: [{"week_of": snapshot_key, "sample_size": sample_by_format[fmt], "usage": usage_by_format[fmt]}]
            for fmt in FORMATS
            if sample_by_format[fmt] > 0
        }
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        EXPORT_PATH.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[done] snapshot_date={snapshot_key}, total rows written={total_rows}, exported to {EXPORT_PATH}")


if __name__ == "__main__":
    run()
