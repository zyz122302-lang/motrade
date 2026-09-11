"""每周研究流水线：用 GoatBots 历史卖价回溯构造"当前特征出现后 N 天价格是否真的
反弹"的训练样本，训练一个 LightGBM 二分类模型，按时间切分做验证（不是随机切分，
避免未来数据泄露到训练集），再把训练好的模型套到当前候选池上打分。

目的是**校准**已经写进 strategies/*.yaml 的人工规则（尤其是
supply_vs_demand_framework.yaml 里"跌势放缓不等于会反弹"这条），不是替代它、
也不接入每日自动化流程——这里跑一次要下载两年的历史价格归档，比较重，
每周跑一次就够了。产出写到 data/research_output.json，供每周 routine 读取后
写进仪表盘数据库的 research_runs 集合。

运行方式：python research_pipeline.py
"""

import json
from datetime import date, datetime, timedelta

import lightgbm as lgb
import numpy as np

import indicators
import storage
import watchlist_builder
from config import DATA_DIR
from fetchers import goatbots_fetcher, scryfall_fetcher

OUTPUT_PATH = DATA_DIR / "research_output.json"

LOOKAHEAD_DAYS = 14          # 预测"这之后14天"会不会反弹
REBOUND_THRESHOLD = 0.08     # 涨幅 >= 8% 算反弹（正类）
ANCHOR_STRIDE_DAYS = 10      # 同一张卡每隔多少天取一个训练锚点，减少高度自相关的样本
MIN_HISTORY_FOR_ANCHOR = 35  # 锚点之前至少要有这么多天历史才够算 chg_30d 等特征
MAX_CANDIDATE_NAMES = 1200   # 训练用的卡名上限（按今日价格从高到低截取，价格太低的卡噪声大）
VALID_FRACTION = 0.2         # 按锚点日期切分，最近这一部分比例做验证集（不是随机切分）

RARITY_CODE = {"Common": 0, "Uncommon": 1, "Rare": 2, "Mythic": 3, "Special": 4}

FEATURE_COLUMNS = [
    "chg_7d_pct", "chg_30d_pct", "chg_90d_pct",
    "near_90d_low", "near_90d_high", "big_drop_7d", "big_gain_7d",
    "days_of_history", "ma7", "ma30", "price",
    "rarity_code", "foil", "legal_modern", "legal_legacy",
]


def bootstrap_multi_year_history(conn, version_ids):
    this_year = date.today().year
    for year in (this_year - 1, this_year):
        flag_key = f"research_bootstrap_done_{year}"
        if storage.get_meta(conn, flag_key):
            print(f"[bootstrap] {year} already done, skip")
            continue
        print(f"[bootstrap] downloading {year} yearly price history ...")
        try:
            total_rows = 0
            for day_str, prices in goatbots_fetcher.iter_year_history(year):
                total_rows += storage.upsert_daily_prices(conn, day_str, prices, only_ids=version_ids)
            storage.set_meta(conn, flag_key, datetime.now().isoformat())
            print(f"[bootstrap] {year}: {total_rows} rows ingested")
        except Exception as e:
            print(f"[bootstrap] {year} unavailable ({e}), skipping that year")


def select_research_universe(conn, today_prices: dict) -> list[dict]:
    """返回 [{mtgo_id, name, rarity, foil, legal_modern, legal_legacy, today_price}, ...]，
    每个卡名只取今日 GoatBots 价最低的那个版本（历史价格序列只有 GoatBots 数据，
    Cardhoarder 价格不在 daily_prices 里逐日存档），按今日价格从高到低截断到
    MAX_CANDIDATE_NAMES 张——太便宜的卡百分比指标噪声太大，且大多是散件，价值有限。
    """
    candidate_names = watchlist_builder.build_candidate_names(conn, today_prices)
    versions_map = watchlist_builder.versions_by_name(conn, candidate_names)

    cards, legality = watchlist_builder.load_cards_and_legality(conn)

    universe = []
    for name, versions in versions_map.items():
        priced = [
            (v, today_prices.get(str(v["mtgo_id"])))
            for v in versions
            if today_prices.get(str(v["mtgo_id"])) is not None
        ]
        if not priced:
            continue
        cheapest_version, cheapest_price = min(priced, key=lambda vp: vp[1])
        leg = legality.get(name.lower(), {"modern": False, "legacy": False})
        universe.append({
            "mtgo_id": cheapest_version["mtgo_id"],
            "name": name,
            "rarity": cheapest_version["rarity"],
            "foil": bool(cheapest_version["foil"]),
            "legal_modern": bool(leg.get("modern", False)),
            "legal_legacy": bool(leg.get("legacy", False)),
            "today_price": cheapest_price,
        })

    universe.sort(key=lambda u: -u["today_price"])
    return universe[:MAX_CANDIDATE_NAMES]


def _feature_row(ind: dict, price: float, info: dict) -> dict:
    return {
        "chg_7d_pct": ind.get("chg_7d_pct"),
        "chg_30d_pct": ind.get("chg_30d_pct"),
        "chg_90d_pct": ind.get("chg_90d_pct"),
        "near_90d_low": int(bool(ind.get("near_90d_low"))),
        "near_90d_high": int(bool(ind.get("near_90d_high"))),
        "big_drop_7d": int(bool(ind.get("big_drop_7d"))),
        "big_gain_7d": int(bool(ind.get("big_gain_7d"))),
        "days_of_history": ind.get("days_of_history", 0),
        "ma7": ind.get("ma7"),
        "ma30": ind.get("ma30"),
        "price": price,
        "rarity_code": RARITY_CODE.get(info["rarity"], -1),
        "foil": int(info["foil"]),
        "legal_modern": int(info["legal_modern"]),
        "legal_legacy": int(info["legal_legacy"]),
    }


def build_dataset(conn, universe: list[dict]):
    """回溯遍历每张卡的历史价格序列，每隔 ANCHOR_STRIDE_DAYS 天取一个锚点，用锚点
    当天及之前的历史算特征（复用 indicators.from_history，纯内存计算，不重复查库），
    用锚点之后 LOOKAHEAD_DAYS 天的价格算标签。返回 (rows, labels, anchor_dates)。
    """
    rows, labels, anchor_dates = [], [], []
    for info in universe:
        mid = info["mtgo_id"]
        history = storage.price_history_for(conn, mid, limit_days=800)
        n = len(history)
        if n < MIN_HISTORY_FOR_ANCHOR + LOOKAHEAD_DAYS:
            continue
        dates = [d for d, _ in history]
        prices = [p for _, p in history]
        for i in range(MIN_HISTORY_FOR_ANCHOR, n - LOOKAHEAD_DAYS, ANCHOR_STRIDE_DAYS):
            anchor_price = prices[i]
            if not anchor_price or anchor_price <= 0:
                continue
            future_price = prices[i + LOOKAHEAD_DAYS]
            if future_price is None:
                continue
            as_of = date.fromisoformat(dates[i])
            ind = indicators.from_history(history[: i + 1], anchor_price, as_of)
            rows.append(_feature_row(ind, anchor_price, info))
            labels.append(1 if future_price >= anchor_price * (1 + REBOUND_THRESHOLD) else 0)
            anchor_dates.append(dates[i])
    return rows, labels, anchor_dates


def _to_matrix(rows: list[dict]):
    """LightGBM 的 Dataset 只吃 ndarray（或者它自己的 Sequence 包装），不接受带 None
    的普通 Python 列表；转成 float64 数组，None 变成 np.nan（LightGBM 原生支持 NaN，
    会自动学出缺失值的最优分裂方向，不需要我们自己插补）。"""
    return np.array(
        [[np.nan if r[col] is None else r[col] for col in FEATURE_COLUMNS] for r in rows],
        dtype=np.float64,
    )


def _auc(y_true: list[int], y_score: list[float]):
    n_pos = sum(y_true)
    n_neg = len(y_true) - n_pos
    if n_pos == 0 or n_neg == 0:
        return None
    ranked = sorted(range(len(y_score)), key=lambda i: y_score[i])
    ranks = [0] * len(y_score)
    for rank, idx in enumerate(ranked, start=1):
        ranks[idx] = rank
    rank_sum_pos = sum(ranks[i] for i in range(len(y_true)) if y_true[i] == 1)
    auc = (rank_sum_pos - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)
    return round(auc, 4)


def train_and_evaluate(rows, labels, anchor_dates):
    order = sorted(range(len(rows)), key=lambda i: anchor_dates[i])
    rows = [rows[i] for i in order]
    labels = [labels[i] for i in order]
    anchor_dates = [anchor_dates[i] for i in order]

    split = int(len(rows) * (1 - VALID_FRACTION))
    train_X, valid_X = _to_matrix(rows[:split]), _to_matrix(rows[split:])
    train_y, valid_y = labels[:split], labels[split:]

    train_set = lgb.Dataset(train_X, label=train_y, feature_name=FEATURE_COLUMNS)
    valid_set = lgb.Dataset(valid_X, label=valid_y, feature_name=FEATURE_COLUMNS, reference=train_set)

    params = {
        "objective": "binary",
        "metric": "binary_logloss",
        "verbosity": -1,
        "num_leaves": 31,
        "learning_rate": 0.05,
        "min_data_in_leaf": 30,
    }
    model = lgb.train(
        params, train_set,
        num_boost_round=300,
        valid_sets=[valid_set],
        callbacks=[lgb.early_stopping(stopping_rounds=20, verbose=False), lgb.log_evaluation(period=0)],
    )

    valid_pred = model.predict(valid_X, num_iteration=model.best_iteration)
    valid_pred_label = [1 if p >= 0.5 else 0 for p in valid_pred]
    accuracy = sum(1 for p, y in zip(valid_pred_label, valid_y) if p == y) / len(valid_y)
    baseline_rate = sum(valid_y) / len(valid_y)
    baseline_accuracy = max(baseline_rate, 1 - baseline_rate)
    auc = _auc(valid_y, list(valid_pred))

    importances = model.feature_importance(importance_type="gain")
    feature_importance = sorted(
        [{"feature": f, "importance": round(float(imp), 1)} for f, imp in zip(FEATURE_COLUMNS, importances)],
        key=lambda x: -x["importance"],
    )

    return model, {
        "trainRows": len(train_X),
        "validRows": len(valid_X),
        "validDateFrom": anchor_dates[split] if split < len(anchor_dates) else None,
        "validDateTo": anchor_dates[-1] if anchor_dates else None,
        "validAccuracy": round(accuracy, 4),
        "baselineAccuracy": round(baseline_accuracy, 4),
        "validAuc": auc,
        "validPositiveRate": round(baseline_rate, 4),
        "featureImportance": feature_importance[:10],
    }


def score_today(conn, model, universe: list[dict], today_prices: dict):
    today = date.today()
    feats, meta = [], []
    for info in universe:
        mid = info["mtgo_id"]
        price = today_prices.get(str(mid))
        if price is None:
            continue
        ind = indicators.compute(conn, mid, price, as_of=today)
        feats.append(_feature_row(ind, price, info))
        meta.append({
            "name": info["name"],
            "mtgoId": mid,
            "price": price,
            "chg7d": ind.get("chg_7d_pct"),
            "chg30d": ind.get("chg_30d_pct"),
        })
    if not feats:
        return []
    probs = model.predict(_to_matrix(feats), num_iteration=model.best_iteration)
    results = []
    for m, prob in zip(meta, probs):
        results.append({**m, "predictedReboundProb": round(float(prob), 4)})
    results.sort(key=lambda r: -r["predictedReboundProb"])
    return results[:20]


def run():
    with storage.connect() as conn:
        print("[goatbots] fetching card definitions ...")
        card_defs = goatbots_fetcher.fetch_card_definitions()
        storage.upsert_cards(conn, card_defs)

        print("[goatbots] fetching today's sell prices ...")
        today_prices, price_date = goatbots_fetcher.fetch_today_prices()
        print(f"[goatbots] price snapshot date: {price_date}")

        print("[scryfall] refreshing legality cache ...")
        legality = scryfall_fetcher.fetch_legality_by_name()
        storage.upsert_legality(conn, legality, datetime.now().isoformat())

        universe = select_research_universe(conn, today_prices)
        print(f"[universe] {len(universe)} representative printings selected for research")
        version_ids = {u["mtgo_id"] for u in universe}

        bootstrap_multi_year_history(conn, version_ids)

        rows_added = storage.upsert_daily_prices(conn, price_date, today_prices, only_ids=version_ids)
        print(f"[storage] recorded {rows_added} rows for {price_date}")

        print("[dataset] building backtest feature/label rows ...")
        rows, labels, anchor_dates = build_dataset(conn, universe)
        print(f"[dataset] {len(rows)} anchor rows, positive rate {sum(labels)/len(labels):.3f}" if rows else "[dataset] no rows built")

        if len(rows) < 500:
            output = {
                "runDate": date.today().isoformat(),
                "status": "insufficient_data",
                "message": f"只构造出 {len(rows)} 条训练样本，数据量不够训练，跳过本次。",
            }
        else:
            print("[train] training LightGBM classifier ...")
            model, metrics = train_and_evaluate(rows, labels, anchor_dates)
            print(f"[train] valid accuracy={metrics['validAccuracy']} baseline={metrics['baselineAccuracy']} auc={metrics['validAuc']}")

            print("[score] scoring today's candidate pool ...")
            top_candidates = score_today(conn, model, universe, today_prices)

            output = {
                "runDate": date.today().isoformat(),
                "status": "ok",
                "lookaheadDays": LOOKAHEAD_DAYS,
                "reboundThreshold": REBOUND_THRESHOLD,
                "universeSize": len(universe),
                **metrics,
                "topCandidates": top_candidates,
            }

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[done] wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    run()
