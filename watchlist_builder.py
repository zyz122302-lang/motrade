"""从卡牌信息 + 今日卖价 + 赛制合法性缓存里，筛出"常用单卡"候选池。

规则（见 config.py 可调）：
- 非 foil（foil 版本二期再做）
- 今日卖价 >= WATCHLIST_MIN_PRICE_TIX（过滤纯废卡）
- 在 WATCHLIST_FORMATS 里至少一个赛制合法（默认 Modern + Legacy）
- 排除 Booster / token / 玩家奖励等非卡牌条目（rarity 不在正常稀有度范围内）
"""

from config import WATCHLIST_FORMATS, WATCHLIST_MIN_PRICE_TIX

NORMAL_RARITIES = {"Common", "Uncommon", "Rare", "Mythic", "Special"}


def build_candidate_ids(conn, today_prices: dict) -> list[int]:
    cards = {
        row[0]: {"name": row[1], "cardset": row[2], "rarity": row[3], "foil": row[4]}
        for row in conn.execute("SELECT mtgo_id, name, cardset, rarity, foil FROM cards")
    }
    legality = {
        row[0]: {"modern": bool(row[1]), "legacy": bool(row[2])}
        for row in conn.execute("SELECT name_lower, modern, legacy FROM legality_cache")
    }

    candidates = []
    for mid_str, price in today_prices.items():
        mid = int(mid_str)
        info = cards.get(mid)
        if not info:
            continue
        if info["foil"]:
            continue
        if info["rarity"] not in NORMAL_RARITIES:
            continue
        if price is None or price < WATCHLIST_MIN_PRICE_TIX:
            continue
        leg = legality.get((info["name"] or "").lower())
        if not leg:
            continue
        if not any(leg.get(fmt, False) for fmt in WATCHLIST_FORMATS):
            continue
        candidates.append(mid)
    return candidates
