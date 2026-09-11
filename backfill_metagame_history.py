"""一次性回填 MTGO 官方赛事使用率历史，往前补到7月初。

不是常规定时任务——只跑一次把 2026年7月/8月/9月前几天的历史使用率数据补进
Artifact 仪表盘数据库的 metagame_usage 集合，回填完之后每日 metagame_pipeline.py
会自然接续往后统计，不需要重复跑这个脚本。

数据来源合规性：mtgo.com/decklists 页面本身自带一个"年份/月份下拉菜单 + Go 按钮"
的归档浏览功能（页面 HTML 里能看到 `id="decklistYear"`/`id="decklistMonth"`），
生成的 URL 是 `?month=MM&year=YYYY`——这是官方设计好、写在页面公开导航元素里的
浏览方式，我们只是直接构造这个 URL 而不是先点开菜单再点 Go，跟人工操作能达到的
效果完全一样，不是遍历/猜测不透明的事件 ID。跟每日/每周流程一样，只用官方公开
数据，不碰账号、不抓买价。

统计方法：把目标月份范围内所有 Challenge + 每日 League(5-0) 的牌表按 7 天一组
（从 7月1日开始，不跟自然周对齐，纯粹是最简单的等长分桶）切成多个历史"快照"，
每个快照的 week_of 用这组窗口的最后一天，跟 metagame_pipeline.py 日常产出的
{format}-{source}-{week_of} 文档是同一个集合、同一套字段，只是 source 标成
"mtgo_official_backfill" 以区分这是历史批量回填而不是当天的滚动7日快照
（避免以后有人分析时把两种不同统计口径的数据混为一谈）。

运行方式：python backfill_metagame_history.py
输出：data/metagame_backfill_export.json，结构 {format: [{week_of, source,
sample_size, usage}, ...]}，供人工/routine 读取后用 Artifact write_db 批量写入。
"""

import json
from datetime import date, timedelta

from config import DATA_DIR
from fetchers import mtgo_metagame_fetcher as mtgo

OUTPUT_PATH = DATA_DIR / "metagame_backfill_export.json"

TARGET_MONTHS = [(2026, 7), (2026, 8), (2026, 9)]
TRACKED_FORMATS = ("modern", "legacy", "standard", "pauper")
BUCKET_DAYS = 7
BACKFILL_START = date(2026, 7, 1)
BACKFILL_END = date(2026, 9, 6)  # 9月7日之后已经有每日流程的真实数据了，不用回填
SOURCE_TAG = "mtgo_official_backfill"


def collect_events():
    all_events = []
    seen_urls = set()
    for year, month in TARGET_MONTHS:
        print(f"[fetch] {year}-{month:02d} archive index ...")
        events = mtgo.fetch_events_for_month(year, month, only_formats=TRACKED_FORMATS)
        for e in events:
            if e["url"] in seen_urls or not e["date"]:
                continue
            d = date.fromisoformat(e["date"])
            if not (BACKFILL_START <= d <= BACKFILL_END):
                continue
            if e["event_type"] not in ("challenge", "league"):
                continue
            seen_urls.add(e["url"])
            all_events.append(e)
        print(f"[fetch] {year}-{month:02d}: {len(events)} raw events, {len(all_events)} kept so far")
    return all_events


def bucket_key(d: date) -> str:
    offset = (d - BACKFILL_START).days // BUCKET_DAYS
    bucket_end = BACKFILL_START + timedelta(days=offset * BUCKET_DAYS + BUCKET_DAYS - 1)
    return bucket_end.isoformat()


def run():
    events = collect_events()
    print(f"[total] {len(events)} events to fetch decklists for")

    # {(format, week_of): {"usage": {name: count}, "sample_size": int}}
    buckets: dict = {}
    for i, e in enumerate(events, start=1):
        if i % 25 == 0 or i == len(events):
            print(f"[decklists] {i}/{len(events)} ...")
        try:
            data = mtgo.fetch_event_decklists(e["url"])
        except Exception as ex:
            print(f"[decklists] skip {e['url']} ({ex})")
            continue
        d = date.fromisoformat(e["date"])
        wk = bucket_key(d)
        key = (e["format"], wk)
        bucket = buckets.setdefault(key, {"usage": {}, "sample_size": 0})
        for deck in data.get("decks", []):
            bucket["sample_size"] += 1
            for name in deck.get("cards", {}):
                bucket["usage"][name] = bucket["usage"].get(name, 0) + 1

    export: dict = {fmt: [] for fmt in TRACKED_FORMATS}
    for (fmt, week_of), bucket in buckets.items():
        if bucket["sample_size"] == 0:
            continue
        export[fmt].append({
            "week_of": week_of,
            "source": SOURCE_TAG,
            "sample_size": bucket["sample_size"],
            "usage": bucket["usage"],
        })
    for fmt in export:
        export[fmt].sort(key=lambda w: w["week_of"])

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(export, ensure_ascii=False), encoding="utf-8")
    total_weeks = sum(len(v) for v in export.values())
    print(f"[done] wrote {OUTPUT_PATH}: {total_weeks} (format, week) snapshots")


if __name__ == "__main__":
    run()
