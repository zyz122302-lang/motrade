"""每日流水线：抓取 -> 建候选池 -> 算指标 -> 输出 JSON。

运行方式：python pipeline.py
输出：data/latest_watchlist.json，供 /schedule 定时任务读取后
      通过 Artifact write_db 写入仪表盘页面的数据库。

本脚本本身不接触任何第三方通知/LLM API，也不碰用户账号信息，
只处理公开市场数据。
"""

import json
from datetime import date, datetime, timedelta

import indicators
import storage
import watchlist_builder
from config import DATA_DIR
from fetchers import goatbots_fetcher, scryfall_fetcher

LEGALITY_REFRESH_DAYS = 7
OUTPUT_PATH = DATA_DIR / "latest_watchlist.json"


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


def bootstrap_year_history_if_needed(conn, year: int, candidate_ids: set):
    flag_key = f"bootstrap_done_{year}"
    if storage.get_meta(conn, flag_key):
        print(f"[bootstrap] {year} already bootstrapped, skip")
        return
    print(f"[bootstrap] downloading {year} yearly price history (one-time) ...")
    total_rows = 0
    for day_str, prices in goatbots_fetcher.iter_year_history(year):
        total_rows += storage.upsert_daily_prices(conn, day_str, prices, only_ids=candidate_ids)
    storage.set_meta(conn, flag_key, datetime.now().isoformat())
    print(f"[bootstrap] ingested {total_rows} price rows for {len(candidate_ids)} candidate cards")


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

        candidate_ids = watchlist_builder.build_candidate_ids(conn, today_prices)
        print(f"[watchlist] {len(candidate_ids)} candidates (Modern/Legacy, price >= threshold)")
        candidate_id_set = set(candidate_ids)

        bootstrap_year_history_if_needed(conn, date.today().year, candidate_id_set)

        rows_added = storage.upsert_daily_prices(conn, price_date, today_prices, only_ids=candidate_id_set)
        print(f"[storage] recorded {rows_added} rows for {price_date}")

        name_map = {
            row[0]: {"name": row[1], "cardset": row[2], "rarity": row[3]}
            for row in conn.execute(
                "SELECT mtgo_id, name, cardset, rarity FROM cards WHERE mtgo_id IN (%s)"
                % ",".join("?" * len(candidate_ids)),
                candidate_ids,
            )
        } if candidate_ids else {}

        print("[scryfall] fetching Cardhoarder tix prices (default_cards bulk) ...")
        cardhoarder_prices = scryfall_fetcher.fetch_cardhoarder_prices_by_mtgo_id()
        print(f"[scryfall] {len(cardhoarder_prices)} Cardhoarder prices loaded")

        results = []
        for mid in candidate_ids:
            price = today_prices.get(str(mid))
            if price is None:
                continue
            info = name_map.get(mid, {})
            ind = indicators.compute(conn, mid, price)
            results.append({
                "mtgo_id": mid,
                "name": info.get("name"),
                "set": info.get("cardset"),
                "rarity": info.get("rarity"),
                "price": price,
                "cardhoarder_price": cardhoarder_prices.get(mid),
                **ind,
            })

        signals = [r for r in results if r["near_90d_low"] or r["big_drop_7d"]]
        signals.sort(key=lambda r: (r["chg_30d_pct"] if r["chg_30d_pct"] is not None else 0))

        risers = [r for r in results if r["near_90d_high"] or r["big_gain_7d"]]
        risers.sort(key=lambda r: -(r["chg_30d_pct"] if r["chg_30d_pct"] is not None else 0))

        output = {
            "asOf": price_date,
            "generatedAt": datetime.now().isoformat(),
            "candidateCount": len(results),
            "signalCount": len(signals),
            "riserCount": len(risers),
            "signals": signals[:40],
            "risers": risers[:40],
            "watchlist": sorted(results, key=lambda r: -(r["price"] or 0))[:60],
        }
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[done] wrote {OUTPUT_PATH} — {len(results)} candidates, {len(signals)} signals")


if __name__ == "__main__":
    run()
