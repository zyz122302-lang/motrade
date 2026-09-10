"""每日流水线：抓取 -> 按卡名聚合候选池（含所有版本）-> 算指标 -> 输出 JSON。

运行方式：python pipeline.py
输出：data/latest_watchlist.json，供 /schedule 定时任务读取后
      通过 Artifact write_db 写入仪表盘页面的数据库。

数据按**卡名**聚合：一张卡（比如 Fatal Push）下面可能有多个版本
（不同系列、foil/非foil），每个版本各有自己的 GoatBots/Cardhoarder 价格
和历史走势。列表展示用的是该卡在两个数据源、所有版本里的最低价。

本脚本本身不接触任何第三方通知/LLM API，也不碰用户账号信息，
只处理公开市场数据。
"""

import json
from datetime import date, datetime

import indicators
import storage
import watchlist_builder
from config import DATA_DIR
from fetchers import goatbots_fetcher, scryfall_fetcher

LEGALITY_REFRESH_DAYS = 7
OUTPUT_PATH = DATA_DIR / "latest_watchlist.json"
METAGAME_FORMATS = ("standard", "modern", "legacy", "pauper")
USAGE_RELEVANCE_THRESHOLD = 0.02  # 至少 2% 的牌表用到才算"在这个赛制里活跃"


def format_usage_for(conn, name: str) -> dict:
    """返回 {format: {playRate, prevPlayRate, weeksOfData}}，只包含最新一周
    play_rate 达到 USAGE_RELEVANCE_THRESHOLD 的赛制（卡真的在被用，不是零星一两套牌）。"""
    out = {}
    for fmt in METAGAME_FORMATS:
        trend = storage.metagame_usage_trend(conn, name, fmt, limit_weeks=8)
        if not trend:
            continue
        latest_rate = trend[-1][1]
        if latest_rate < USAGE_RELEVANCE_THRESHOLD:
            continue
        prev_rate = trend[-2][1] if len(trend) >= 2 else None
        out[fmt] = {
            "playRate": round(latest_rate, 4),
            "prevPlayRate": round(prev_rate, 4) if prev_rate is not None else None,
            "weeksOfData": len(trend),
        }
    return out


def refresh_legality_if_stale(conn):
    fetched_at = storage.get_meta(conn, "legality_fetched_at")
    stale = True
    if fetched_at:
        last = datetime.fromisoformat(fetched_at).date()
        stale = (date.today() - last).days >= LEGALITY_REFRESH_DAYS
    if not stale:
        print("[legality] cache is fresh, skip Scryfall fetch")
        return
    print("[legality] fetching Scryfall oracle-cards bulk data ...")
    legality = scryfall_fetcher.fetch_legality_by_name()
    storage.upsert_legality(conn, legality, datetime.now().isoformat())
    storage.set_meta(conn, "legality_fetched_at", datetime.now().isoformat())
    print(f"[legality] cached {len(legality)} card names")


def bootstrap_year_history_if_needed(conn, year: int, version_ids: set):
    flag_key = f"bootstrap_done_{year}"
    if storage.get_meta(conn, flag_key):
        print(f"[bootstrap] {year} already bootstrapped, skip")
        return
    print(f"[bootstrap] downloading {year} yearly price history (one-time) ...")
    total_rows = 0
    for day_str, prices in goatbots_fetcher.iter_year_history(year):
        total_rows += storage.upsert_daily_prices(conn, day_str, prices, only_ids=version_ids)
    storage.set_meta(conn, flag_key, datetime.now().isoformat())
    print(f"[bootstrap] ingested {total_rows} price rows for {len(version_ids)} tracked versions")


def run():
    with storage.connect() as conn:
        print("[goatbots] fetching card definitions ...")
        card_defs = goatbots_fetcher.fetch_card_definitions()
        storage.upsert_cards(conn, card_defs)
        print(f"[goatbots] {len(card_defs)} card definitions loaded")

        print("[goatbots] fetching today's sell prices ...")
        today_prices, price_date = goatbots_fetcher.fetch_today_prices()
        print(f"[goatbots] price snapshot date: {price_date}")

        refresh_legality_if_stale(conn)

        candidate_names = watchlist_builder.build_candidate_names(conn, today_prices)
        print(f"[watchlist] {len(candidate_names)} candidate card names (Modern/Legacy, price >= threshold)")

        versions_map = watchlist_builder.versions_by_name(conn, candidate_names)
        all_version_ids = {v["mtgo_id"] for versions in versions_map.values() for v in versions}
        print(f"[watchlist] {len(all_version_ids)} total tracked printings across all candidate names")

        bootstrap_year_history_if_needed(conn, date.today().year, all_version_ids)

        rows_added = storage.upsert_daily_prices(conn, price_date, today_prices, only_ids=all_version_ids)
        print(f"[storage] recorded {rows_added} rows for {price_date}")

        print("[scryfall] fetching Cardhoarder tix prices (default_cards bulk) ...")
        cardhoarder_prices = scryfall_fetcher.fetch_cardhoarder_prices_by_mtgo_id()
        print(f"[scryfall] {len(cardhoarder_prices)} Cardhoarder prices loaded")

        card_results = []
        for name, version_infos in versions_map.items():
            versions = []
            for vinfo in version_infos:
                mid = vinfo["mtgo_id"]
                gb_price = today_prices.get(str(mid))
                ch_price = cardhoarder_prices.get(mid)
                if gb_price is None and ch_price is None:
                    continue
                ind = indicators.compute(conn, mid, gb_price) if gb_price is not None else {}
                versions.append({
                    "mtgo_id": mid,
                    "set": vinfo["set"],
                    "foil": vinfo["foil"],
                    "rarity": vinfo["rarity"],
                    "goatbotsPrice": gb_price,
                    "cardhoarderPrice": ch_price,
                    "chg_7d_pct": ind.get("chg_7d_pct"),
                    "chg_30d_pct": ind.get("chg_30d_pct"),
                    "chg_90d_pct": ind.get("chg_90d_pct"),
                    "low_90d": ind.get("low_90d"),
                    "high_90d": ind.get("high_90d"),
                    "near_90d_low": ind.get("near_90d_low", False),
                    "near_90d_high": ind.get("near_90d_high", False),
                    "big_drop_7d": ind.get("big_drop_7d", False),
                    "big_gain_7d": ind.get("big_gain_7d", False),
                    "ma7": ind.get("ma7"),
                    "ma30": ind.get("ma30"),
                    "days_of_history": ind.get("days_of_history", 0),
                })
            if not versions:
                continue

            def best_of(v):
                candidates = [p for p in (v["goatbotsPrice"], v["cardhoarderPrice"]) if p is not None]
                return min(candidates) if candidates else float("inf")

            primary = min(versions, key=best_of)
            best_price = best_of(primary)
            best_source = "goatbots" if primary["goatbotsPrice"] == best_price else "cardhoarder"
            format_usage = format_usage_for(conn, name)

            card_results.append({
                "name": name,
                "rarity": primary["rarity"],
                "formats": sorted(format_usage.keys()),
                "formatUsage": format_usage,
                "versionCount": len(versions),
                "bestPrice": best_price,
                "bestSource": best_source,
                "primaryMtgoId": primary["mtgo_id"],
                "primarySet": primary["set"],
                "chg_7d_pct": primary["chg_7d_pct"],
                "chg_30d_pct": primary["chg_30d_pct"],
                "chg_90d_pct": primary["chg_90d_pct"],
                "low_90d": primary["low_90d"],
                "ma7": primary["ma7"],
                "ma30": primary["ma30"],
                "near_90d_low": primary["near_90d_low"],
                "near_90d_high": primary["near_90d_high"],
                "big_drop_7d": primary["big_drop_7d"],
                "big_gain_7d": primary["big_gain_7d"],
                "days_of_history": primary["days_of_history"],
                "versions": versions,
            })

        signals = [r for r in card_results if r["near_90d_low"] or r["big_drop_7d"]]
        signals.sort(key=lambda r: (r["chg_30d_pct"] if r["chg_30d_pct"] is not None else 0))

        risers = [r for r in card_results if r["near_90d_high"] or r["big_gain_7d"]]
        risers.sort(key=lambda r: -(r["chg_30d_pct"] if r["chg_30d_pct"] is not None else 0))

        output = {
            "asOf": price_date,
            "generatedAt": datetime.now().isoformat(),
            "candidateCount": len(card_results),
            "signalCount": len(signals),
            "riserCount": len(risers),
            "signals": signals[:40],
            "risers": risers[:40],
        }
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[done] wrote {OUTPUT_PATH} — {len(card_results)} candidate cards, {len(signals)} signals, {len(risers)} risers")


if __name__ == "__main__":
    run()
