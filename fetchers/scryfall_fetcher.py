"""Scryfall 官方 API 抓取：仅用于赛制合法性判断。

使用官方 bulk data 端点（一次性拿全量 oracle 卡数据），符合 Scryfall 自己
建议的"批量分析用 bulk data，不要逐卡查询"用法。
"""

import gzip
import json

import requests

from config import HTTP_USER_AGENT, SCRYFALL_BULK_DATA_API

_HEADERS = {"User-Agent": f"MOTrade/0.1 ({HTTP_USER_AGENT})"}


def fetch_legality_by_name() -> dict:
    """返回 {name_lower: {"modern": bool, "legacy": bool}}。

    以卡名（小写）为 key —— 同名卡在不同印刷版本之间的赛制合法性
    通常一致（Un-系列等银边卡例外，不在 Modern/Legacy 观察范围内）。
    """
    resp = requests.get(SCRYFALL_BULK_DATA_API, headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    entries = resp.json()["data"]
    oracle_entry = next(e for e in entries if e["type"] == "oracle_cards")
    jsonl_url = oracle_entry["jsonl_download_uri"]

    data_resp = requests.get(jsonl_url, headers=_HEADERS, timeout=120, stream=True)
    data_resp.raise_for_status()

    result = {}
    raw = data_resp.content
    if jsonl_url.endswith(".gz"):
        raw = gzip.decompress(raw)
    for line in raw.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        card = json.loads(line)
        legalities = card.get("legalities", {})
        result[card["name"].lower()] = {
            "modern": legalities.get("modern") == "legal",
            "legacy": legalities.get("legacy") == "legal",
        }
    return result
