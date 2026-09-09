"""GoatBots 官方卖价批量数据抓取。

只使用 https://www.goatbots.com/download-prices 页面上公开的两类下载：
- card-definitions.zip  卡牌基础信息（名称/系列/稀有度/foil），MTGO ID 索引
- price-history.zip     当日卖价快照
- price-history-<year>.zip  年度历史卖价归档（每天一个文件），仅用于首次
  建库时回填历史趋势，不做高频重复下载。

页面原文写明这些数据是"for your own project"公开提供的，不做绕过反爬、
不抓取买价（买价刻意做了防抓取处理，见 docs/PRINCIPLES.md）。
"""

import io
import json
import zipfile
from datetime import date

import requests

from config import (
    GOATBOTS_BASE,
    GOATBOTS_CARD_DEFINITIONS_URL,
    GOATBOTS_PRICE_HISTORY_URL,
    HTTP_USER_AGENT,
)

_HEADERS = {"User-Agent": HTTP_USER_AGENT}


def _download_zip(url: str) -> zipfile.ZipFile:
    resp = requests.get(url, headers=_HEADERS, timeout=60)
    resp.raise_for_status()
    return zipfile.ZipFile(io.BytesIO(resp.content))


def fetch_card_definitions() -> dict:
    """返回 {mtgo_id(str): {name, cardset, rarity, version, foil}}。"""
    zf = _download_zip(GOATBOTS_CARD_DEFINITIONS_URL)
    name = zf.namelist()[0]
    with zf.open(name) as f:
        return json.load(f)


def fetch_today_prices() -> tuple[dict, str]:
    """返回 (({mtgo_id(str): price}), 数据日期字符串)。"""
    zf = _download_zip(GOATBOTS_PRICE_HISTORY_URL)
    name = zf.namelist()[0]
    # 文件名形如 price-history-2026-09-08.txt
    price_date = name.replace("price-history-", "").replace(".txt", "")
    with zf.open(name) as f:
        return json.load(f), price_date


def iter_year_history(year: int):
    """流式生成整年的历史卖价，仅用于首次建库回填。

    yields (date_str, {mtgo_id(str): price})
    """
    url = f"{GOATBOTS_BASE}/download/prices/price-history-{year}.zip"
    zf = _download_zip(url)
    for name in sorted(zf.namelist()):
        if not name.startswith("price-history-"):
            continue
        price_date = name.replace("price-history-", "").replace(".txt", "")
        with zf.open(name) as f:
            yield price_date, json.load(f)
