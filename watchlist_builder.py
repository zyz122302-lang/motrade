"""从卡牌信息 + 今日卖价 + 赛制合法性缓存里，筛出"常用单卡"候选池。

按**卡名**聚合，不按单个印刷版本聚合——同一张卡的不同系列/foil版本算作
该卡下面的"版本"，不是独立的候选。

规则（见 config.py 可调）：
- 一张卡只要有任意一个版本（含 foil）今日卖价 >= WATCHLIST_MIN_PRICE_TIX，
  这张卡（所有版本）就算入选候选池
- 在 WATCHLIST_FORMATS 里至少一个赛制合法（默认 Modern + Legacy）
- 排除 Booster / token / 玩家奖励等非卡牌条目（rarity 不在正常稀有度范围内）
"""

from config import WATCHLIST_FORMATS, WATCHLIST_MIN_PRICE_TIX

NORMAL_RARITIES = {"Common", "Uncommon", "Rare", "Mythic", "Special"}


def load_cards_and_legality(conn):
    cards = {
        row[0]: {"name": row[1], "cardset": row[2], "rarity": row[3], "foil": row[4], "version": row[5]}
        for row in conn.execute("SELECT mtgo_id, name, cardset, rarity, foil, version FROM cards")
    }
    legality = {
        row[0]: {"modern": bool(row[1]), "legacy": bool(row[2])}
        for row in conn.execute("SELECT name_lower, modern, legacy FROM legality_cache")
    }
    return cards, legality


def build_candidate_names(conn, today_prices: dict) -> set[str]:
    """返回入选候选池的卡名集合（不区分版本/foil）。"""
    cards, legality = load_cards_and_legality(conn)

    names = set()
    for mid_str, price in today_prices.items():
        mid = int(mid_str)
        info = cards.get(mid)
        if not info:
            continue
        if info["rarity"] not in NORMAL_RARITIES:
            continue
        if price is None or price < WATCHLIST_MIN_PRICE_TIX:
            continue
        name = info["name"] or ""
        leg = legality.get(name.lower())
        if not leg:
            continue
        if not any(leg.get(fmt, False) for fmt in WATCHLIST_FORMATS):
            continue
        names.add(name)
    return names


def versions_by_name(conn, names: set[str]) -> dict[str, list[dict]]:
    """返回 {name: [{mtgo_id, set, rarity, foil, collector_number}, ...]}，包含该卡名下
    所有已知印刷版本（不管价格高低、foil 与否），用于"点进去看所有版本"的详情页。

    `collector_number` 来自 GoatBots card-definitions 里的 `version` 字段——同一个
    `cardset` 代码下常常混着好几种实际印刷（普通版/无边框版/复古边框版等，比如
    Exploration 的 DMR 版就有两种，`cardset` 都是 "DMR"），光看 cardset+foil 区分不
    开，这个字段（其实是收藏编号）是目前能拿到的、能把它们区分开的信息。"""
    cards, _ = load_cards_and_legality(conn)
    result = {name: [] for name in names}
    for mid, info in cards.items():
        name = info["name"] or ""
        if name in result:
            result[name].append({
                "mtgo_id": mid,
                "set": info["cardset"],
                "rarity": info["rarity"],
                "foil": info["foil"],
                "collector_number": info.get("version"),
            })
    return result
