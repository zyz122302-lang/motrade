"""Scryfall 官方 API 抓取：赛制合法性 + Cardhoarder 卖价（第二数据源）。

使用官方 bulk data 端点，符合 Scryfall 自己建议的"批量分析用 bulk data，
不要逐卡查询"用法。Scryfall 的 `default_cards` 数据集里每条记录（对应具体某个
印刷版本）自带 `mtgo_id` 和 `prices.tix`（来自 Cardhoarder），可以直接用
mtgo_id 跟 GoatBots 的数据精确匹配，不需要抓 Cardhoarder 自己的网站。
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


def fetch_cardhoarder_prices_by_mtgo_id() -> dict:
    """返回 {mtgo_id(int): tix_price(float)}，来自 Scryfall `default_cards`
    数据集里的 `prices.tix` 字段（Scryfall 标注该字段来源为 Cardhoarder）。

    按具体印刷版本匹配（用 mtgo_id 做 key），跟 GoatBots 按 mtgo_id 记录
    的卖价是同一个粒度，可以直接对照同一张卡在两个平台的价格。
    """
    resp = requests.get(SCRYFALL_BULK_DATA_API, headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    entries = resp.json()["data"]
    default_entry = next(e for e in entries if e["type"] == "default_cards")
    jsonl_url = default_entry["jsonl_download_uri"]

    data_resp = requests.get(jsonl_url, headers=_HEADERS, timeout=180, stream=True)
    data_resp.raise_for_status()

    raw = data_resp.content
    if jsonl_url.endswith(".gz"):
        raw = gzip.decompress(raw)

    result = {}
    for line in raw.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        card = json.loads(line)
        mtgo_id = card.get("mtgo_id")
        tix = (card.get("prices") or {}).get("tix")
        if mtgo_id is not None and tix is not None:
            result[int(mtgo_id)] = float(tix)
    return result


def fetch_card_image_bytes(mtgo_id: int) -> bytes | None:
    """按 mtgo_id 取卡图原始字节（Scryfall 官方按 MTGO id 查图的接口，
    `requests` 默认会跟随 302 重定向到 cards.scryfall.io 的 CDN）。
    找不到图（比如冷门促销版本没收录）时返回 None，调用方应该跳过而不是报错中断。
    """
    url = f"https://api.scryfall.com/cards/mtgo/{mtgo_id}?format=image"
    resp = requests.get(url, headers=_HEADERS, timeout=20)
    if resp.status_code != 200:
        return None
    return resp.content
