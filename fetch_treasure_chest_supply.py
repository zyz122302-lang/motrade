"""Treasure Chest 供给端代理指标：每日 League 5-0 数量。

背景（用户指出、已核实代码逻辑）：Treasure Chest 的供给主要来自两类赛事奖励——
Challenge 按最终名次分级发放固定数量（比如 Challenge32 只有前16名拿箱子，报名人数
从32涨到50不会让发的箱子变多，供给基本是常数，只有"这场有没有正常开赛"这种偶发
事件才会影响），League 则是每次 5-0 战绩发放奖励，5-0 的人数逐日真实波动，是
唯一值得当连续变量追踪的供给端信号。

MTGO 官方本身就只发布 5-0 战绩的 League 牌表（`mtgo_metagame_fetcher.fetch_event_decklists`
对 League 页面强制按 `wins.wins == "5"` 过滤，这是数据源本身的限制，不是我们扫描不全），
所以"某天有多少份 5-0 牌表"这个数字可以直接从已有的抓取逻辑里数出来，不需要新的数据源。

用法：python fetch_treasure_chest_supply.py
输出：data/treasure_chest_supply.json，结构：
    { "series": [[date_str, total_5_0_count], ...], "byFormat": {date: {format: count}} }

历史靠"导入 + 追加"：如果 data/treasure_chest_supply_import.json 存在（每日 routine 从
仪表盘数据库 read_db 回来的旧历史），在它基础上追加最近几天（mtgo.com/decklists 首页
只列出最近约2周的赛事，足够每天增量追加，不需要每次全量重抓）；不存在时退回抓取首页
能看到的全部 League 事件作为历史基线（比日常更新慢，只会发生一次）。
"""

import json
from datetime import datetime

from config import DATA_DIR
from fetchers import mtgo_metagame_fetcher as mtgo

OUTPUT_PATH = DATA_DIR / "treasure_chest_supply.json"
IMPORT_PATH = DATA_DIR / "treasure_chest_supply_import.json"
ALL_FORMATS = ("modern", "legacy", "standard", "pauper", "pioneer", "vintage", "premodern")


def load_prior():
    if not IMPORT_PATH.exists():
        return {}
    data = json.loads(IMPORT_PATH.read_text(encoding="utf-8"))
    return data.get("byFormat", {})


def run():
    by_format = load_prior()
    print(f"[bridge] {len(by_format)} dates already known from imported history" if by_format else "[bridge] no imported history, doing a fresh pull of the recent-events index")

    events = mtgo.fetch_recent_events(only_formats=ALL_FORMATS)
    leagues = [e for e in events if e["event_type"] == "league"]
    print(f"[mtgo] {len(leagues)} league event pages found on the recent-events index")

    for i, e in enumerate(leagues, 1):
        # slug 形如 "modern-league-2026-09-11XXXXX"，日期是抓取时已知的固定格式，直接切片更稳，
        # 不依赖 fetch_event_decklists 返回的 date 字段（League 页面的 publish_date 有时缺失）。
        slug = e["slug"]
        date_str = slug.split("-league-")[-1][:10]
        fmt = e["format"]
        if date_str in by_format and fmt in by_format[date_str]:
            continue  # 已经有这一天这个赛制的数据，跳过，节省请求
        try:
            result = mtgo.fetch_event_decklists(e["url"])
        except Exception as ex:
            print(f"[mtgo] {i}/{len(leagues)} {slug} failed: {ex}, skipping")
            continue
        n = len(result.get("decks", []))
        by_format.setdefault(date_str, {})[fmt] = n
        print(f"[mtgo] {i}/{len(leagues)} {date_str} {fmt}: {n} five-oh decks")

    series = [[d, sum(fmts.values())] for d, fmts in sorted(by_format.items())]

    output = {
        "generatedAt": datetime.now().isoformat(),
        "series": series,
        "byFormat": by_format,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[done] wrote {OUTPUT_PATH} -- {len(series)} days of data, latest: {series[-1] if series else None}")


if __name__ == "__main__":
    run()
