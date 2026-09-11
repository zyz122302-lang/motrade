"""从历史卖价序列计算简单的趋势指标。纯规则/数学，不依赖 LLM。"""

from datetime import date, timedelta

import storage


def _price_n_days_ago(history: list[tuple[str, float]], n: int, as_of: date):
    """history 是 [(date_str, price), ...]，按时间升序。找 >= n 天前最近的一条。"""
    if not history:
        return None
    target = (as_of - timedelta(days=n)).isoformat()
    candidate = None
    for d, p in history:
        if d <= target:
            candidate = p
        else:
            break
    return candidate


def from_history(history: list[tuple[str, float]], current_price: float, as_of: date) -> dict:
    """纯函数版本：直接吃一段已经在内存里的 (date_str, price) 历史序列，不查数据库。

    `research_pipeline.py` 回溯构造训练样本时会调用几万次，每次都重新查一遍 SQLite
    太浪费——调用方自己把某张卡的全部历史一次性查出来，切片到 as_of 当天为止，
    传进来复用。`compute()`（生产环境每日流水线用）是这个函数的一层数据库查询包装。
    """
    if as_of < date.today():
        as_of_str = as_of.isoformat()
        history = [(d, p) for d, p in history if d <= as_of_str]
    prices_only = [p for _, p in history]

    p7 = _price_n_days_ago(history, 7, as_of)
    p30 = _price_n_days_ago(history, 30, as_of)
    p90 = _price_n_days_ago(history, 90, as_of)

    def pct(old, new):
        if old is None or old == 0:
            return None
        return round((new - old) / old * 100, 1)

    window_90 = prices_only[-90:] if prices_only else []
    low_90 = min(window_90) if window_90 else None
    high_90 = max(window_90) if window_90 else None

    near_low = (
        low_90 is not None and low_90 > 0 and current_price <= low_90 * 1.05
    )
    near_high = (
        high_90 is not None and current_price >= high_90 * 0.95
    )
    chg_7d = pct(p7, current_price)
    big_drop_7d = chg_7d is not None and chg_7d <= -15
    big_gain_7d = chg_7d is not None and chg_7d >= 15

    ma7 = round(sum(prices_only[-7:]) / len(prices_only[-7:]), 2) if prices_only else None
    ma30 = round(sum(prices_only[-30:]) / len(prices_only[-30:]), 2) if prices_only else None

    return {
        "chg_7d_pct": chg_7d,
        "chg_30d_pct": pct(p30, current_price),
        "chg_90d_pct": pct(p90, current_price),
        "low_90d": low_90,
        "high_90d": high_90,
        "near_90d_low": near_low,
        "near_90d_high": near_high,
        "big_drop_7d": big_drop_7d,
        "big_gain_7d": big_gain_7d,
        "ma7": ma7,
        "ma30": ma30,
        "days_of_history": len(prices_only),
    }


def compute(conn, mtgo_id: int, current_price: float, as_of: date | None = None) -> dict:
    """指标计算，查数据库版本（每日流水线用）。`as_of` 默认今天；传一个历史日期可以
    算出"如果站在那一天看，这些指标会是什么样子"，避免用到未来数据。"""
    as_of = as_of or date.today()
    history = storage.price_history_for(conn, mtgo_id, limit_days=800)
    return from_history(history, current_price, as_of)
