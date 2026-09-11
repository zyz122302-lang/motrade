"""MTGO 专属可交易物品 Treasure Chest 的价格追踪。

Treasure Chest 是 MTGO 独有的可交易物品（不是印刷的 Magic 卡牌），在 Vintage/Legacy
Challenge 等赛事里作为奖励发放，玩家之间可以像交易单卡一样在 GoatBots 上交易它本身。
GoatBots 的 card-definitions 数据把它记成一条 rarity="Booster"、cardset="O99" 的特殊
条目（name 是 "Treasure Chest Booster"），跟真正的单卡完全分开处理：不进
watchlist_builder 的候选池（不会出现在涨跌观察列表里），单独在仪表盘首页展示。

用法：python fetch_treasure_chest.py
输出：data/treasure_chest.json

历史价格靠"导入 + 追加"而不是每次重新下载多年归档：如果 data/treasure_chest_import.json
存在（每日 routine 从仪表盘数据库 read_db 回来的旧历史，见 SKILL.md），直接在它基础上
追加今天这一个价格点；如果不存在（本地首次建库/云端环境第一次跑这个脚本），退回一次性
下载最近两年的 GoatBots 年度归档，只保留这一个 mtgo_id 的价格，作为历史基线。
"""

import json
from datetime import date, datetime

import indicators
from config import DATA_DIR
from fetchers import goatbots_fetcher

OUTPUT_PATH = DATA_DIR / "treasure_chest.json"
IMPORT_PATH = DATA_DIR / "treasure_chest_import.json"
ITEM_NAME = "Treasure Chest Booster"
ITEM_CARDSET = "O99"
DISPLAY_NAME = "Treasure Chest"


def find_mtgo_id(card_defs: dict) -> int:
    for mid, info in card_defs.items():
        if info.get("name") == ITEM_NAME and info.get("cardset") == ITEM_CARDSET:
            return int(mid)
    raise SystemExit(
        f"未在 GoatBots card-definitions 里找到 name={ITEM_NAME!r} cardset={ITEM_CARDSET!r}，"
        "可能 GoatBots 改了命名，需要人工确认新的条目长什么样"
    )


def load_prior_series() -> list:
    if not IMPORT_PATH.exists():
        return []
    data = json.loads(IMPORT_PATH.read_text(encoding="utf-8"))
    return [tuple(row) for row in data.get("series", [])]


def bootstrap_series(mtgo_id: int) -> list:
    """首次建库：下载最近两年的 GoatBots 年度归档，只留这一个 mtgo_id 的价格。"""
    out = []
    for year in (date.today().year - 1, date.today().year):
        print(f"[treasure_chest] downloading {year} yearly price history (one-time) ...")
        for day_str, prices in goatbots_fetcher.iter_year_history(year):
            p = prices.get(str(mtgo_id))
            if p is not None:
                out.append((day_str, p))
    out.sort(key=lambda x: x[0])
    return out


def merge_series(prior: list, day_str: str, price: float) -> list:
    by_date = dict(prior)
    by_date[day_str] = price
    return sorted(by_date.items(), key=lambda x: x[0])


def run():
    print("[goatbots] fetching card definitions ...")
    card_defs = goatbots_fetcher.fetch_card_definitions()
    mtgo_id = find_mtgo_id(card_defs)
    info = card_defs[str(mtgo_id)]
    print(f"[treasure_chest] mtgo_id={mtgo_id} cardset={info.get('cardset')}")

    print("[goatbots] fetching today's sell prices ...")
    today_prices, price_date = goatbots_fetcher.fetch_today_prices()
    price = today_prices.get(str(mtgo_id))
    if price is None:
        raise SystemExit(f"今天的价格快照里没有 mtgo_id={mtgo_id} 的价格，跳过")

    prior = load_prior_series()
    if prior:
        print(f"[treasure_chest] using imported history ({len(prior)} points), appending today")
        series = merge_series(prior, price_date, price)
    else:
        print("[treasure_chest] no imported history found, bootstrapping from yearly archives")
        series = merge_series(bootstrap_series(mtgo_id), price_date, price)
    print(f"[treasure_chest] series has {len(series)} points, {price_date}: {price}")

    ind = indicators.from_history(series, price, date.today())

    output = {
        "mtgoId": mtgo_id,
        "name": DISPLAY_NAME,
        "cardset": info.get("cardset"),
        "asOf": price_date,
        "generatedAt": datetime.now().isoformat(),
        "price": price,
        "chg_7d_pct": ind.get("chg_7d_pct"),
        "chg_30d_pct": ind.get("chg_30d_pct"),
        "chg_90d_pct": ind.get("chg_90d_pct"),
        "low_90d": ind.get("low_90d"),
        "high_90d": ind.get("high_90d"),
        "ma7": ind.get("ma7"),
        "ma30": ind.get("ma30"),
        "days_of_history": ind.get("days_of_history", 0),
        "series": series,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[done] wrote {OUTPUT_PATH} -- price={price} asOf={price_date}")


if __name__ == "__main__":
    run()
