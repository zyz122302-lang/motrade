"""买卖价差统计：把用户手动录入的买价观察记录，换算成"典型价差有多大"的统计量。

背景：GoatBots/Cardhoarder 的买价（bot 愿意收多少钱）故意做了防抓取处理
（SVG 字形渲染，见 docs/PRINCIPLES.md），不能也不应该绕过去自动抓。但仪表盘"买价记录"
页签（`buy_observations` 集合）已经在积累一份完全合规的买价数据——用户自己手动打开
MTGO 客户端或 GoatBots 网页查看 bot 报价后，手动录入的 {name, set, bot, price, noBid, date}
记录。这份数据目前只用在持仓页面算"如果现在平仓能拿到多少钱"，从没被拿来做过统计。

本脚本把每条买价观察跟同一天（或最近一天）本地已有的 GoatBots 卖价对比，算出价差百分比
（(卖价-买价)/卖价），按稀有度分桶统计中位数——这样"预测涨跌"之外，还能回答"就算判断方向对了，
扣掉真实买卖价差之后还剩多少"这个问题。`noBid=true`（bot 完全不收）的记录不计入价差，只计入
"拒收率"，本身就是流动性差的信号。

用法：python compute_spread_stats.py
输入：data/buy_observations_import.json（每日 routine 从仪表盘数据库 read_db 回来的
      buy_observations 集合全量记录，见 SKILL.md；不存在或为空时直接输出"样本不足"，不报错）
输出：data/spread_stats.json
"""

import json
from datetime import date, datetime, timedelta
from statistics import median

import storage
from config import DATA_DIR

IMPORT_PATH = DATA_DIR / "buy_observations_import.json"
OUTPUT_PATH = DATA_DIR / "spread_stats.json"
MIN_SAMPLE_SIZE = 5
LOOKBACK_DAYS = 5  # 观察记录当天本地没有卖价快照时，往前找最近几天的


def load_observations() -> list[dict]:
    if not IMPORT_PATH.exists():
        return []
    data = json.loads(IMPORT_PATH.read_text(encoding="utf-8"))
    return data.get("observations", data if isinstance(data, list) else [])


def find_ask_and_rarity(conn, name: str, set_code: str, obs_date: str):
    """在本地 cards/daily_prices 表里找这张卡（按 name+cardset 匹配到的全部 mtgo_id）
    在 obs_date 当天或最近几天的卖价，返回 (ask_price, rarity)，找不到返回 (None, None)。"""
    rows = conn.execute(
        "SELECT mtgo_id, rarity FROM cards WHERE name = ? AND cardset = ?",
        (name, set_code),
    ).fetchall()
    if not rows:
        return None, None
    rarity = rows[0][1]
    mtgo_ids = [r[0] for r in rows]
    try:
        target = date.fromisoformat(obs_date)
    except (TypeError, ValueError):
        return None, rarity
    for delta in range(LOOKBACK_DAYS + 1):
        day_str = (target - timedelta(days=delta)).isoformat()
        placeholders = ",".join("?" for _ in mtgo_ids)
        prices = conn.execute(
            f"SELECT price FROM daily_prices WHERE date = ? AND mtgo_id IN ({placeholders})",
            (day_str, *mtgo_ids),
        ).fetchall()
        if prices:
            vals = [p[0] for p in prices]
            return sum(vals) / len(vals), rarity
    return None, rarity


def run():
    observations = load_observations()
    print(f"[spread] {len(observations)} buy_observations 记录待处理" if observations else "[spread] 没有买价观察记录，样本不足")

    by_rarity: dict[str, list[float]] = {}
    no_bid_counts: dict[str, list[int]] = {}
    matched, skipped_no_ask, skipped_no_bid = 0, 0, 0

    with storage.connect() as conn:
        for obs in observations:
            name = (obs.get("name") or "").strip()
            set_code = (obs.get("set") or "").strip()
            if not name or not set_code:
                continue  # Treasure Chest 等 set 留空的记录不参与按稀有度统计
            # 手动录入表单的 placeholder 举例是"MH3 F"（用空格+F 标注 foil），跟数据库里纯净的
            # cardset 代码（foil 是独立字段，不掺进 set 里）对不上，这里做一次轻量归一化再匹配。
            if set_code[-2:].lower() == " f":
                set_code = set_code[:-2].strip()
            ask, rarity = find_ask_and_rarity(conn, name, set_code, obs.get("date"))
            if rarity is None:
                skipped_no_ask += 1
                continue
            key = rarity or "unknown"
            no_bid_counts.setdefault(key, [0, 0])
            no_bid_counts[key][1] += 1
            if obs.get("noBid"):
                no_bid_counts[key][0] += 1
                skipped_no_bid += 1
                continue
            bid = obs.get("price")
            if ask is None or bid is None or ask <= 0:
                skipped_no_ask += 1
                continue
            spread_pct = (ask - bid) / ask * 100
            by_rarity.setdefault(key, []).append(spread_pct)
            matched += 1

    def bucket_stats(spreads: list[float], nobid: list[int]) -> dict:
        sample_size = len(spreads)
        nobid_hits, nobid_total = nobid if nobid else (0, 0)
        return {
            "medianSpreadPct": round(median(spreads), 1) if sample_size >= MIN_SAMPLE_SIZE else None,
            "sampleSize": sample_size,
            "noBidRate": round(nobid_hits / nobid_total, 3) if nobid_total >= MIN_SAMPLE_SIZE else None,
            "noBidSampleSize": nobid_total,
            "insufficientSample": sample_size < MIN_SAMPLE_SIZE,
        }

    by_rarity_out = {}
    all_rarities = set(by_rarity) | set(no_bid_counts)
    for key in all_rarities:
        by_rarity_out[key] = bucket_stats(by_rarity.get(key, []), no_bid_counts.get(key, [0, 0]))

    all_spreads = [v for vals in by_rarity.values() for v in vals]
    all_nobid_hits = sum(v[0] for v in no_bid_counts.values())
    all_nobid_total = sum(v[1] for v in no_bid_counts.values())

    output = {
        "generatedAt": datetime.now().isoformat(),
        "totalObservations": len(observations),
        "matched": matched,
        "skippedNoAskData": skipped_no_ask,
        "skippedNoBid": skipped_no_bid,
        "byRarity": by_rarity_out,
        "overall": bucket_stats(all_spreads, [all_nobid_hits, all_nobid_total]),
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[done] wrote {OUTPUT_PATH} -- matched={matched}, overall sampleSize={output['overall']['sampleSize']}")


if __name__ == "__main__":
    run()
