"""每周研究流水线：用 GoatBots 历史卖价回溯构造"当前特征出现后 N 天价格是否真的
反弹"的训练样本，训练 LightGBM 二分类模型（同时训练 7/14/30 天三个预测窗口），
按时间切分做验证（不是随机切分，避免未来数据泄露到训练集），再把训练好的模型
套到当前候选池上打分。

目的是**校准**已经写进 strategies/*.yaml 的人工规则（尤其是
supply_vs_demand_framework.yaml 里"跌势放缓不等于会反弹"这条），不是替代它、
也不接入每日自动化流程——这里跑一次要下载两年的历史价格归档，比较重，
每周跑一次就够了。产出写到 data/research_output.json，供每周 routine 读取后
写进仪表盘数据库的 research_runs 集合；每个窗口挑出来的候选卡还会额外附带完整的
versions 数组（跟每日 watchlist 文档同一套字段），对应的价格曲线数据另外写到
data/research_price_history_export.json（{mtgo_id: {series: [...]}}），供 routine
批量写进 price_history 集合——这样点开任何一张研究候选卡都能看到完整的版本切换器
和价格曲线，不会因为它没在每日观察池里就只显示一个阉割版详情页。

特征除了价格历史指标，还包括赛制使用率、官方禁限公告事件、系列发售时间这三类——
**但这三类数据都是最近才开始积累的（使用率/禁限监控大约从 2026-09 才开始有真实
快照），两年的回溯训练集里绝大多数历史锚点在这几个特征上会是缺失值（LightGBM
原生支持缺失值，不需要插补，也不会报错），只有最近这一两周的锚点才有真实取值。
这不是 bug，是数据积累时间不够长的真实反映——`dataCoverage` 字段会如实报告每个
特征的非缺失覆盖率，随着每周持续跑、历史数据不断累积，覆盖率会自然提高，不需要
每次都重新设计特征。

运行方式：
    先跑 pipeline.py 同款的"赛事使用率桥接"步骤，把 metagame_usage 集合读回本地写成
    data/metagame_import.json（结构见 storage.import_metagame_usage_snapshot）；
    再把 news_events 集合整体读回写成 data/br_events_import.json（结构：
    [{date, format, added: [...], removed: [...]}, ...]，两个字段都没有的话跳过）；
    两个文件都是可选的——不存在就跳过对应特征，不影响其他部分。
    然后：python research_pipeline.py
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
PRICE_HISTORY_EXPORT_PATH = DATA_DIR / "research_price_history_export.json"
METAGAME_IMPORT_PATH = DATA_DIR / "metagame_import.json"
BR_EVENTS_IMPORT_PATH = DATA_DIR / "br_events_import.json"
PRICE_HISTORY_MAX_POINTS = 30

LOOKAHEAD_WINDOWS = [7, 14, 30]   # 同时训练这几个预测窗口（天）
DISPLAY_MIN_PROB = 0.5            # 仪表盘只展示预测反弹概率超过这个阈值的候选
DISPLAY_MAX_PER_WINDOW = 10       # 每个窗口最多保留几张（概率降序截断）
REBOUND_THRESHOLD = 0.08          # 涨幅 >= 8% 算反弹（正类），三个窗口用同一个阈值——
                                   # 注意这意味着窗口越长，达到阈值天然越容易，
                                   # 三个窗口的正类比例不可直接比较"预测难度"，
                                   # 只能比较各自窗口内 模型 vs 基线 的相对提升
ANCHOR_STRIDE_DAYS = 10           # 同一张卡每隔多少天取一个训练锚点
MIN_HISTORY_FOR_ANCHOR = 35       # 锚点之前至少要有这么多天历史才够算 chg_30d 等特征
MAX_CANDIDATE_NAMES = 1200        # 训练用的卡名上限（按今日价格从高到低截取）
VALID_FRACTION = 0.2              # 按锚点日期切分，最近这一部分比例做验证集
BR_EVENT_WINDOW_DAYS = 30         # "最近N天内有没有禁限变动"这个特征的回看窗口

RARITY_CODE = {"Common": 0, "Uncommon": 1, "Rare": 2, "Mythic": 3, "Special": 4}

FEATURE_COLUMNS = [
    "chg_7d_pct", "chg_30d_pct", "chg_90d_pct",
    "near_90d_low", "near_90d_high", "big_drop_7d", "big_gain_7d",
    "days_of_history", "ma7", "ma30", "price",
    "rarity_code", "foil", "legal_modern", "legal_legacy",
    "usage_modern", "usage_legacy", "set_age_days", "br_event_recent",
]


def import_metagame_usage_if_present(conn):
    if not METAGAME_IMPORT_PATH.exists():
        print("[metagame] no metagame_import.json found, usage_* 特征全部缺失")
        return
    snapshot = json.loads(METAGAME_IMPORT_PATH.read_text(encoding="utf-8"))
    n = storage.import_metagame_usage_snapshot(conn, snapshot)
    print(f"[metagame] imported {n} rows from metagame_import.json")


def load_br_events() -> list[dict]:
    if not BR_EVENTS_IMPORT_PATH.exists():
        print("[br_events] no br_events_import.json found, br_event_recent 特征全部为0")
        return []
    events = json.loads(BR_EVENTS_IMPORT_PATH.read_text(encoding="utf-8"))
    print(f"[br_events] loaded {len(events)} historical banned/restricted change events")
    return events


def build_br_index(events: list[dict]) -> dict:
    """{card_name: [(date_str, direction), ...]}，direction 是 'added'（新增禁限，利空）
    或 'removed'（解除，利好），按日期升序。"""
    idx: dict = {}
    for ev in events:
        d = ev.get("date")
        if not d:
            continue
        for name in ev.get("added") or []:
            idx.setdefault(name, []).append((d, "added"))
        for name in ev.get("removed") or []:
            idx.setdefault(name, []).append((d, "removed"))
    for name in idx:
        idx[name].sort()
    return idx


def br_event_recent(idx: dict, name: str, as_of_str: str) -> int:
    """名字是否在 as_of 之前 BR_EVENT_WINDOW_DAYS 天内出现过禁限变动。返回 0/1，
    不区分利空利好方向——"最近有没有变动"本身就是一个值得模型知道的信号，
    方向性影响已经体现在标签（价格实际涨跌）里了，不需要在特征里重复编码。"""
    events = idx.get(name)
    if not events:
        return 0
    as_of = date.fromisoformat(as_of_str)
    cutoff = (as_of - timedelta(days=BR_EVENT_WINDOW_DAYS)).isoformat()
    for d, _direction in events:
        if cutoff <= d <= as_of_str:
            return 1
        if d > as_of_str:
            break
    return 0


def build_usage_index(conn, names: list[str]) -> dict:
    """{name: {"modern": [(week_of, play_rate), ...], "legacy": [...]}}，升序。
    一次性把每张卡的使用率序列查出来缓存住，避免几万个锚点重复查数据库。"""
    idx = {}
    for name in names:
        idx[name] = {
            fmt: storage.metagame_usage_trend(conn, name, fmt, limit_weeks=500)
            for fmt in ("modern", "legacy")
        }
    return idx


def usage_as_of(idx: dict, name: str, fmt: str, as_of_str: str):
    trend = (idx.get(name) or {}).get(fmt) or []
    val = None
    for week_of, rate in trend:
        if week_of <= as_of_str:
            val = rate
        else:
            break
    return val


def build_set_first_seen(conn) -> dict:
    """{cardset: earliest_date_str}——某个系列里，任意一个被本地追踪的印刷版本
    第一次在 daily_prices 里出现价格记录的日期，作为"这个系列大概什么时候上线
    MTGO"的数据驱动代理指标（不额外抓取任何新数据源，只是换个角度用已有数据）。"""
    rows = conn.execute(
        """SELECT c.cardset, MIN(dp.date) FROM daily_prices dp
           JOIN cards c ON c.mtgo_id = dp.mtgo_id
           WHERE c.cardset IS NOT NULL AND c.cardset != ''
           GROUP BY c.cardset"""
    ).fetchall()
    return {cardset: d for cardset, d in rows if d}


def select_research_universe(conn, today_prices: dict) -> list[dict]:
    """返回 [{mtgo_id, name, rarity, foil, cardset, legal_modern, legal_legacy,
    today_price}, ...]，每个卡名只取今日 GoatBots 价最低的那个版本，按今日价格
    从高到低截断到 MAX_CANDIDATE_NAMES 张。"""
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
            "cardset": cheapest_version["set"],
            "legal_modern": bool(leg.get("modern", False)),
            "legal_legacy": bool(leg.get("legacy", False)),
            "today_price": cheapest_price,
        })

    universe.sort(key=lambda u: -u["today_price"])
    return universe[:MAX_CANDIDATE_NAMES]


def build_versions_for_names(conn, names: set, today_prices: dict, cardhoarder_prices: dict) -> dict:
    """返回 {name: [version_dict, ...]}，字段跟 pipeline.py 每日写进仪表盘 watchlist
    文档里的 versions 数组同一套 camelCase 结构——这样单卡详情页不用区分"这张卡是
    每日观察池选的还是研究流水线选的"，两边给的数据形状一样，前端直接复用同一套
    渲染逻辑（版本切换器、价格曲线）就行，不需要额外写一套简化版展示。"""
    versions_map = watchlist_builder.versions_by_name(conn, names)
    today = date.today()
    out = {}
    for name, vinfos in versions_map.items():
        versions = []
        for vinfo in vinfos:
            mid = vinfo["mtgo_id"]
            gb_price = today_prices.get(str(mid))
            ch_price = cardhoarder_prices.get(mid)
            if gb_price is None and ch_price is None:
                continue
            ind = indicators.compute(conn, mid, gb_price, as_of=today) if gb_price is not None else {}
            versions.append({
                "mtgoId": mid,
                "set": vinfo["set"],
                "foil": bool(vinfo["foil"]),
                "collectorNumber": vinfo.get("collector_number"),
                "goatbotsPrice": gb_price,
                "cardhoarderPrice": ch_price,
                "chg7d": ind.get("chg_7d_pct"),
                "chg30d": ind.get("chg_30d_pct"),
                "low90": ind.get("low_90d"),
                "ma7": ind.get("ma7"),
                "ma30": ind.get("ma30"),
            })
        if versions:
            out[name] = versions
    return out


def collect_version_ids_for_names(conn, names: set) -> set:
    """只拿这些卡名下所有已知印刷版本的 mtgo_id 集合，不算指标——用于在正式
    枚举版本数据之前，先知道要不要给这些"非主力版本"（训练用的 1200 张候选池
    只回填了每张卡最便宜那个版本的历史价格）额外补一次历史，不然它们的版本
    切换器会有 set/foil/现价，但价格曲线和 chg7d/chg30d 是空的。"""
    versions_map = watchlist_builder.versions_by_name(conn, names)
    return {v["mtgo_id"] for vs in versions_map.values() for v in vs}


def bootstrap_ids_for_years(conn, ids: set, years):
    """强制为指定 id 集合回补历史，不看 `research_bootstrap_done_<year>` 标记——
    那个标记只代表"训练用的 1200 张主力版本已经回填过"，这里要补的是研究候选卡
    的非主力版本，是完全不同的一批 id，用同一个标记会误判成"已经做过"而跳过。"""
    if not ids:
        return
    for year in years:
        print(f"[bootstrap-extra] {year} for {len(ids)} secondary-version ids ...")
        try:
            total = 0
            for day_str, prices in goatbots_fetcher.iter_year_history(year):
                total += storage.upsert_daily_prices(conn, day_str, prices, only_ids=ids)
            print(f"[bootstrap-extra] {year}: {total} rows ingested")
        except Exception as e:
            print(f"[bootstrap-extra] {year} unavailable ({e}), skipping")


def thin_series(history: list[tuple[str, float]], max_points: int = PRICE_HISTORY_MAX_POINTS):
    """跟每日流程一样，价格曲线抽稀到最多 max_points 个点，避免单个文档太大。"""
    if len(history) <= max_points:
        return [[d, p] for d, p in history]
    step = len(history) / max_points
    out, i = [], 0.0
    while int(i) < len(history):
        out.append(list(history[int(i)]))
        i += step
    if list(history[-1]) != out[-1]:
        out.append(list(history[-1]))
    return out


def build_price_history_export(conn, mtgo_ids: set) -> dict:
    """{mtgo_id(str): {series: [[date, price], ...]}}，供 routine 写进 price_history
    集合——跟每日流程用的是同一个集合/同一套 doc_id 规则（mtgo_id 字符串做 doc_id），
    所以仪表盘详情页原有的 loadChartFor() 不用改一行代码就能读到这些数据。"""
    out = {}
    for mid in mtgo_ids:
        history = storage.price_history_for(conn, mid, limit_days=800)
        if len(history) < 2:
            continue
        out[str(mid)] = {"series": thin_series(history)}
    return out


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


def _feature_row(ind: dict, price: float, info: dict, extra: dict) -> dict:
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
        "usage_modern": extra.get("usage_modern"),
        "usage_legacy": extra.get("usage_legacy"),
        "set_age_days": extra.get("set_age_days"),
        "br_event_recent": extra.get("br_event_recent", 0),
    }


def build_dataset(conn, universe: list[dict], usage_idx: dict, br_idx: dict, set_first_seen: dict):
    """回溯遍历每张卡的历史价格序列，每隔 ANCHOR_STRIDE_DAYS 天取一个锚点。
    返回 (rows, labels_by_window, anchor_dates)，labels_by_window 是
    {window_days: [0/1, ...]}，跟 rows/anchor_dates 一一对应（同一组锚点，
    只是标签窗口不同，方便三个窗口共用同一份特征矩阵）。
    """
    max_window = max(LOOKAHEAD_WINDOWS)
    rows, anchor_dates = [], []
    labels_by_window = {w: [] for w in LOOKAHEAD_WINDOWS}

    for info in universe:
        mid = info["mtgo_id"]
        name = info["name"]
        cardset = info["cardset"]
        set_seen = set_first_seen.get(cardset)
        history = storage.price_history_for(conn, mid, limit_days=800)
        n = len(history)
        if n < MIN_HISTORY_FOR_ANCHOR + max_window:
            continue
        dates = [d for d, _ in history]
        prices = [p for _, p in history]

        for i in range(MIN_HISTORY_FOR_ANCHOR, n - max_window, ANCHOR_STRIDE_DAYS):
            anchor_price = prices[i]
            if not anchor_price or anchor_price <= 0:
                continue
            anchor_date_str = dates[i]
            future_prices = {w: prices[i + w] for w in LOOKAHEAD_WINDOWS if i + w < n}
            if len(future_prices) < len(LOOKAHEAD_WINDOWS):
                continue

            as_of = date.fromisoformat(anchor_date_str)
            ind = indicators.from_history(history[: i + 1], anchor_price, as_of)
            extra = {
                "usage_modern": usage_as_of(usage_idx, name, "modern", anchor_date_str),
                "usage_legacy": usage_as_of(usage_idx, name, "legacy", anchor_date_str),
                "set_age_days": (as_of - date.fromisoformat(set_seen)).days if set_seen else None,
                "br_event_recent": br_event_recent(br_idx, name, anchor_date_str),
            }
            rows.append(_feature_row(ind, anchor_price, info, extra))
            anchor_dates.append(anchor_date_str)
            for w in LOOKAHEAD_WINDOWS:
                labels_by_window[w].append(1 if future_prices[w] >= anchor_price * (1 + REBOUND_THRESHOLD) else 0)

    return rows, labels_by_window, anchor_dates


def _to_matrix(rows: list[dict]) -> np.ndarray:
    """LightGBM 的 Dataset 只吃 ndarray，不接受带 None 的普通 Python 列表；
    转成 float64 数组，None 变成 np.nan（LightGBM 原生支持 NaN 分裂，不需要插补）。"""
    return np.array(
        [[np.nan if r[col] is None else r[col] for col in FEATURE_COLUMNS] for r in rows],
        dtype=np.float64,
    )


def _coverage(rows: list[dict]) -> dict:
    """报告每个特征的非缺失覆盖率，尤其是 usage_modern/usage_legacy/br_event_recent/
    set_age_days 这几个刚开始积累的特征——如实告诉用户现在覆盖率有多低。"""
    n = len(rows) or 1
    return {
        col: round(sum(1 for r in rows if r[col] is not None) / n, 4)
        for col in FEATURE_COLUMNS
    }


def _auc(y_true: list[int], y_score) -> float | None:
    n_pos = sum(y_true)
    n_neg = len(y_true) - n_pos
    if n_pos == 0 or n_neg == 0:
        return None
    y_score = list(y_score)
    ranked = sorted(range(len(y_score)), key=lambda i: y_score[i])
    ranks = [0] * len(y_score)
    for rank, idx in enumerate(ranked, start=1):
        ranks[idx] = rank
    rank_sum_pos = sum(ranks[i] for i in range(len(y_true)) if y_true[i] == 1)
    return round((rank_sum_pos - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg), 4)


def train_one_window(X: np.ndarray, y: list[int], anchor_dates: list[str]):
    order = sorted(range(len(y)), key=lambda i: anchor_dates[i])
    X = X[order]
    y = [y[i] for i in order]
    sorted_dates = [anchor_dates[i] for i in order]

    split = int(len(y) * (1 - VALID_FRACTION))
    train_X, valid_X = X[:split], X[split:]
    train_y, valid_y = y[:split], y[split:]

    train_set = lgb.Dataset(train_X, label=train_y, feature_name=FEATURE_COLUMNS)
    valid_set = lgb.Dataset(valid_X, label=valid_y, feature_name=FEATURE_COLUMNS, reference=train_set)

    params = {
        "objective": "binary", "metric": "binary_logloss", "verbosity": -1,
        "num_leaves": 31, "learning_rate": 0.05, "min_data_in_leaf": 30,
    }
    model = lgb.train(
        params, train_set, num_boost_round=300, valid_sets=[valid_set],
        callbacks=[lgb.early_stopping(stopping_rounds=20, verbose=False), lgb.log_evaluation(period=0)],
    )

    valid_pred = model.predict(valid_X, num_iteration=model.best_iteration)
    valid_pred_label = [1 if p >= 0.5 else 0 for p in valid_pred]
    accuracy = sum(1 for p, yy in zip(valid_pred_label, valid_y) if p == yy) / len(valid_y)
    baseline_rate = sum(valid_y) / len(valid_y)
    baseline_accuracy = max(baseline_rate, 1 - baseline_rate)

    importances = model.feature_importance(importance_type="gain")
    feature_importance = sorted(
        [{"feature": f, "importance": round(float(imp), 1)} for f, imp in zip(FEATURE_COLUMNS, importances)],
        key=lambda x: -x["importance"],
    )

    metrics = {
        "trainRows": int(train_X.shape[0]),
        "validRows": int(valid_X.shape[0]),
        "validDateFrom": sorted_dates[split] if split < len(sorted_dates) else None,
        "validDateTo": sorted_dates[-1] if sorted_dates else None,
        "validAccuracy": round(accuracy, 4),
        "baselineAccuracy": round(baseline_accuracy, 4),
        "validAuc": _auc(valid_y, valid_pred),
        "validPositiveRate": round(baseline_rate, 4),
        "featureImportance": feature_importance[:12],
    }
    return model, metrics


def score_today(conn, models: dict, universe: list[dict], today_prices: dict, usage_idx: dict, br_idx: dict, set_first_seen: dict):
    today = date.today()
    today_str = today.isoformat()
    feats, meta = [], []
    for info in universe:
        mid = info["mtgo_id"]
        price = today_prices.get(str(mid))
        if price is None:
            continue
        ind = indicators.compute(conn, mid, price, as_of=today)
        set_seen = set_first_seen.get(info["cardset"])
        extra = {
            "usage_modern": usage_as_of(usage_idx, info["name"], "modern", today_str),
            "usage_legacy": usage_as_of(usage_idx, info["name"], "legacy", today_str),
            "set_age_days": (today - date.fromisoformat(set_seen)).days if set_seen else None,
            "br_event_recent": br_event_recent(br_idx, info["name"], today_str),
        }
        feats.append(_feature_row(ind, price, info, extra))
        meta.append({
            "name": info["name"], "mtgoId": mid, "price": price,
            "rarity": info["rarity"],
            "chg7d": ind.get("chg_7d_pct"), "chg30d": ind.get("chg_30d_pct"),
        })
    if not feats:
        return {}
    X = _to_matrix(feats)
    out = {}
    for w, model in models.items():
        probs = model.predict(X, num_iteration=model.best_iteration)
        ranked = sorted(
            ({**m, "predictedReboundProb": round(float(p), 4)} for m, p in zip(meta, probs)),
            key=lambda r: -r["predictedReboundProb"],
        )
        # 只展示真的过了概率门槛的，不为了凑数塞进排名靠前但其实没到50%的候选——
        # 某个窗口这周一个都没过线是正常现象，仪表盘会如实显示"没有候选"，不用硬凑10个
        shown = [r for r in ranked if r["predictedReboundProb"] > DISPLAY_MIN_PROB][:DISPLAY_MAX_PER_WINDOW]
        out[w] = shown
    return out


def run():
    with storage.connect() as conn:
        import_metagame_usage_if_present(conn)
        br_events = load_br_events()
        br_idx = build_br_index(br_events)

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

        usage_idx = build_usage_index(conn, [u["name"] for u in universe])
        set_first_seen = build_set_first_seen(conn)

        print("[dataset] building backtest feature/label rows ...")
        rows, labels_by_window, anchor_dates = build_dataset(conn, universe, usage_idx, br_idx, set_first_seen)
        print(f"[dataset] {len(rows)} anchor rows" if rows else "[dataset] no rows built")

        if len(rows) < 500:
            output = {
                "runDate": date.today().isoformat(),
                "status": "insufficient_data",
                "message": f"只构造出 {len(rows)} 条训练样本，数据量不够训练，跳过本次。",
            }
        else:
            coverage = _coverage(rows)
            print(f"[coverage] usage_modern={coverage['usage_modern']} usage_legacy={coverage['usage_legacy']} "
                  f"set_age_days={coverage['set_age_days']} br_event_recent={coverage['br_event_recent']}")

            X = _to_matrix(rows)
            windows_out = {}
            models = {}
            for w in LOOKAHEAD_WINDOWS:
                print(f"[train] window={w}d training LightGBM classifier ...")
                model, metrics = train_one_window(X, labels_by_window[w], anchor_dates)
                print(f"[train] window={w}d valid accuracy={metrics['validAccuracy']} "
                      f"baseline={metrics['baselineAccuracy']} auc={metrics['validAuc']}")
                models[w] = model
                windows_out[str(w)] = metrics

            print("[score] scoring today's candidate pool for all windows ...")
            top_by_window = score_today(conn, models, universe, today_prices, usage_idx, br_idx, set_first_seen)

            print("[enrich] fetching Cardhoarder prices for version enrichment ...")
            cardhoarder_prices = scryfall_fetcher.fetch_cardhoarder_prices_by_mtgo_id()

            candidate_names = {c["name"] for w in LOOKAHEAD_WINDOWS for c in top_by_window.get(w, [])}
            print(f"[enrich] building full version data for {len(candidate_names)} candidate names "
                  f"(so clicking any of them in the dashboard gets the same detail view as a daily-watchlist card) ...")

            candidate_version_ids = collect_version_ids_for_names(conn, candidate_names)
            missing_ids = candidate_version_ids - version_ids  # version_ids = 训练用主力版本集合，已经有历史
            if missing_ids:
                print(f"[enrich] {len(missing_ids)} of {len(candidate_version_ids)} candidate printings are "
                      f"non-primary versions with no local history yet, backfilling ...")
                bootstrap_ids_for_years(conn, missing_ids, (date.today().year - 1, date.today().year))

            versions_by_candidate = build_versions_for_names(conn, candidate_names, today_prices, cardhoarder_prices)

            all_version_mtgo_ids = set()
            for w in LOOKAHEAD_WINDOWS:
                for c in top_by_window.get(w, []):
                    vs = versions_by_candidate.get(c["name"], [])
                    c["versions"] = vs
                    all_version_mtgo_ids.update(v["mtgoId"] for v in vs)
                windows_out[str(w)]["topCandidates"] = top_by_window.get(w, [])

            price_history_export = build_price_history_export(conn, all_version_mtgo_ids)
            PRICE_HISTORY_EXPORT_PATH.write_text(json.dumps(price_history_export, ensure_ascii=False), encoding="utf-8")
            print(f"[enrich] wrote {PRICE_HISTORY_EXPORT_PATH} with {len(price_history_export)} printings' price history")

            output = {
                "runDate": date.today().isoformat(),
                "status": "ok",
                "reboundThreshold": REBOUND_THRESHOLD,
                "universeSize": len(universe),
                "totalAnchorRows": len(rows),
                "featureCoverage": coverage,
                "windows": windows_out,
            }

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[done] wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    run()
