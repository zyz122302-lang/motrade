"""每周赛事使用率流水线：抓取 MTGO 官方 Challenge + 每日 League 5-0 牌表，
统计 Modern / Legacy（次要：Standard / Pauper）里各张卡的使用率，存成周度快照。

运行方式：python metagame_pipeline.py
建议节奏：每周二跑一次（覆盖上周一到上周日的赛事，给 mtgo.com 和聚合站点
留出发布延迟的时间），不需要每天跑。

数据源：mtgo.com/decklists 官方牌表（第一方数据，不抓第三方聚合站点）。
MTGTop8 线下 2 星以上赛事作为补充数据源，另见 mtgtop8_fetcher.py。
"""

from datetime import date, datetime, timedelta

import storage
from fetchers import mtgo_metagame_fetcher

FORMATS = ("modern", "legacy", "standard", "pauper")
LOOKBACK_DAYS = 8  # 覆盖上一整周 + 一点缓冲


def week_of_monday(d: date) -> str:
    return (d - timedelta(days=d.weekday())).isoformat()


def run():
    today = date.today()
    week_key = week_of_monday(today)
    cutoff = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()

    with storage.connect() as conn:
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

        total_rows = 0
        for fmt in FORMATS:
            usage = usage_by_format[fmt]
            usage["__sample_size__"] = sample_by_format[fmt]
            n = storage.upsert_metagame_usage(conn, week_key, fmt, "mtgo_official", usage)
            total_rows += n
            print(f"[storage] {fmt}: {n} card rows, sample_size={sample_by_format[fmt]}")

        storage.set_meta(conn, "metagame_last_run", datetime.now().isoformat())
        print(f"[done] week_of={week_key}, total rows written={total_rows}")


if __name__ == "__main__":
    run()
