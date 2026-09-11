"""研判命中率回评：到期后回头检查过去的 verdict/note 说对了没有。

背景：`watchlist`/`research_runs/latest` 都是"只保留最新一份"的文档，历史研判被下一次覆盖
或删除后就永久丢失了，没有地方能回答"上周说会企稳的那些卡，后来真的没有继续跌吗"。本脚本
配合一个新的只追加(append-only)集合 `verdict_log`（daily/weekly routine 在写 watchlist/
research 候选的同时，各自额外存一份存档，见 SKILL.md），到期后自动回头核对。

不同 verdict/来源用不同的判定口径（不是所有研判都在预测同一件事）：
- falling_knife：预警"还在跌"，到期时净跌（actualChgPct < 0）算命中。
- momentum：预警"有动能"，到期时净涨（actualChgPct > 0）算命中。
- stabilizing / established：预警"企稳/没有剧烈变动"，到期时变动幅度 < 10% 算命中。
- caution / new_set：本来就是"数值异常但不确定"，不强行打分（hit=null），但照样标记为已处理，
  避免每天重复扫描。
- research-rebound-*：模型预测"会涨超过10%"，到期时涨幅 >= 10% 算命中（跟训练时同一个阈值）。
- research-decline-*：模型预测"会继续跌超过10%"，到期时跌幅 <= -10% 算命中。

如果同一次 routine 已经跑过 `compute_spread_stats.py` 产出了 `data/spread_stats.json`，
且这条记录带着 `rarity` 字段、对应稀有度的价差样本又足够，顺带算一个 `estimatedRoundTripPct`
（= actualChgPct - 该稀有度典型价差百分比），作为"扣掉真实买卖价差之后大概还剩多少"的参考——
样本不足或没有 rarity 时这个字段留空，不硬算。

用法：python score_verdicts.py
输入：data/verdict_log_import.json（每日 routine 从仪表盘数据库 read_db `verdict_log`
      集合里 `scored=false` 的记录整批写好的，见 SKILL.md，结构 {"records": [{id, date, name,
      mtgoId, source, verdict, direction, priceAtCall, horizonDays, rarity, scored}, ...]}）
      data/spread_stats.json（可选，compute_spread_stats.py 的输出，用于价差调整）
输出：data/verdict_scoring_output.json，结构：
    { "updates": [{"id": docId, "fields": {scored, scoredAt, priceAtHorizon, actualChgPct,
      hit, estimatedRoundTripPct}}, ...],
      "trackRecord": {"byVerdict": {...}, "bySource": {...}, "updatedAt"} }
routine 用 write_db 的 batch（每条 update）+ 最后一条 set 写 site_meta/verdict_track_record
落库；trackRecord 是基于**全部**已评分记录（不只是这次新评的）重新聚合的，所以 routine 需要
先把这次的 updates 落库、再 read_db 查一遍全部 verdict_log 记录传给下一步——不过这个二次聚合
交给 routine 自己按 SKILL.md 步骤做，本脚本只基于这次桥接进来的记录（已评分+新到期的）算一份
增量参考版本，不保证是全局最终版本。
"""

import json
from datetime import date, datetime, timedelta

import storage
from config import DATA_DIR

IMPORT_PATH = DATA_DIR / "verdict_log_import.json"
SPREAD_STATS_PATH = DATA_DIR / "spread_stats.json"
OUTPUT_PATH = DATA_DIR / "verdict_scoring_output.json"

NOT_SCORED_VERDICTS = {"caution", "new_set"}
LOOKUP_TOLERANCE_DAYS = 5
RESEARCH_THRESHOLD_PCT = 10  # 跟 research_pipeline.py 的 REBOUND_THRESHOLD/DECLINE_THRESHOLD 一致
STABILIZING_BAND_PCT = 10


def load_records() -> list[dict]:
    if not IMPORT_PATH.exists():
        return []
    data = json.loads(IMPORT_PATH.read_text(encoding="utf-8"))
    return data.get("records", [])


def load_spread_stats() -> dict:
    if not SPREAD_STATS_PATH.exists():
        return {}
    return json.loads(SPREAD_STATS_PATH.read_text(encoding="utf-8")).get("byRarity", {})


def is_matured(record: dict, today: date) -> bool:
    try:
        call_date = date.fromisoformat(record["date"])
    except (KeyError, TypeError, ValueError):
        return False
    horizon = record.get("horizonDays")
    if not horizon:
        return False
    return call_date + timedelta(days=horizon) <= today


def price_near(history: list[tuple], target: date) -> float | None:
    """history 是 [(date_str, price), ...]（升序）。找离 target 最近的价格，容忍
    LOOKUP_TOLERANCE_DAYS 天内的缺口（GoatBots 偶尔会有个别日期没发快照）。"""
    by_date = {d: p for d, p in history}
    for delta in range(LOOKUP_TOLERANCE_DAYS + 1):
        for candidate in ((target + timedelta(days=delta)).isoformat(), (target - timedelta(days=delta)).isoformat()):
            if candidate in by_date:
                return by_date[candidate]
    return None


def classify_hit(source: str, verdict: str, actual_chg_pct: float) -> bool | None:
    if source.startswith("research-rebound"):
        return actual_chg_pct >= RESEARCH_THRESHOLD_PCT
    if source.startswith("research-decline"):
        return actual_chg_pct <= -RESEARCH_THRESHOLD_PCT
    if verdict in NOT_SCORED_VERDICTS:
        return None
    if verdict == "falling_knife":
        return actual_chg_pct < 0
    if verdict == "momentum":
        return actual_chg_pct > 0
    if verdict in ("stabilizing", "established"):
        return abs(actual_chg_pct) < STABILIZING_BAND_PCT
    return None  # 未知 verdict，不强行打分


def run():
    records = load_records()
    print(f"[score] {len(records)} 条待评估记录（含尚未到期的，脚本会自己筛）" if records else "[score] 没有待评估记录")
    spread_by_rarity = load_spread_stats()
    today = date.today()

    updates = []
    by_verdict: dict[str, list[bool]] = {}
    by_source: dict[str, list[bool]] = {}
    matured_count, skipped_no_price = 0, 0

    with storage.connect() as conn:
        for r in records:
            if r.get("scored"):
                continue
            if not is_matured(r, today):
                continue
            matured_count += 1
            mtgo_id = r.get("mtgoId")
            if mtgo_id is None:
                skipped_no_price += 1
                continue
            history = storage.price_history_for(conn, int(mtgo_id), limit_days=800)
            call_date = date.fromisoformat(r["date"])
            target_date = call_date + timedelta(days=r["horizonDays"])
            price_at_horizon = price_near(history, target_date)
            price_at_call = r.get("priceAtCall")
            if price_at_horizon is None or price_at_call is None or price_at_call <= 0:
                skipped_no_price += 1
                continue

            actual_chg_pct = round((price_at_horizon - price_at_call) / price_at_call * 100, 1)
            hit = classify_hit(r.get("source", ""), r.get("verdict", ""), actual_chg_pct)

            fields = {
                "scored": True,
                "scoredAt": datetime.now().isoformat(),
                "priceAtHorizon": price_at_horizon,
                "actualChgPct": actual_chg_pct,
                "hit": hit,
            }
            rarity = r.get("rarity")
            if rarity and rarity in spread_by_rarity:
                bucket = spread_by_rarity[rarity]
                if bucket.get("medianSpreadPct") is not None:
                    fields["estimatedRoundTripPct"] = round(actual_chg_pct - bucket["medianSpreadPct"], 1)

            updates.append({"id": r["id"], "fields": fields})

            if hit is not None:
                by_verdict.setdefault(r.get("verdict", "unknown"), []).append(hit)
                by_source.setdefault(r.get("source", "unknown"), []).append(hit)

    def rate_stats(groups: dict[str, list[bool]]) -> dict:
        out = {}
        for key, hits in groups.items():
            out[key] = {
                "hits": sum(1 for h in hits if h),
                "total": len(hits),
                "hitRate": round(sum(1 for h in hits if h) / len(hits), 3) if hits else None,
            }
        return out

    output = {
        "updates": updates,
        "trackRecord": {
            "byVerdict": rate_stats(by_verdict),
            "bySource": rate_stats(by_source),
            "updatedAt": datetime.now().isoformat(),
        },
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[done] wrote {OUTPUT_PATH} -- {matured_count} matured, {len(updates)} scored, "
          f"{skipped_no_price} skipped (no price data yet)")


if __name__ == "__main__":
    run()
