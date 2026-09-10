"""MTGO 官网赛事牌表抓取（官方第一方数据，mtgo.com/decklists）。

用途：统计 Modern / Legacy（次要：Standard / Pauper）赛制里，最近一周
Challenge 赛事 + 每日 League 5-0 牌表中，各张卡的"使用率"（出现在百分之
多少套牌表里），作为价格技术面之外的情报面输入。

合规性：mtgo.com 没有 robots.txt 限制；赛事数据是 Wizards/Daybreak 官方
在牌表页面里直接内嵌的结构化 JSON（服务端渲染，不需要执行 JS），属于
第一方公开数据，不是抓取第三方聚合站点。

注意：赛事牌表只记录"卡名 + 数量"，不区分具体印刷版本/foil，所以这里
统计出来的使用率天然是按卡名聚合的，跟 watchlist_builder 的按名聚合
结构一致。
"""

import json
import re

import requests

from config import HTTP_USER_AGENT

_HEADERS = {"User-Agent": HTTP_USER_AGENT}
_INDEX_URL = "https://www.mtgo.com/decklists"
_BASE_URL = "https://www.mtgo.com"

# 赛事 slug 前缀 -> 内部赛制 key
FORMAT_PREFIXES = {
    "modern": "modern",
    "legacy": "legacy",
    "pauper": "pauper",
    "standard": "standard",
    "pioneer": "pioneer",
    "vintage": "vintage",
    "premodern": "premodern",
}

_DATA_RE = re.compile(r"window\.MTGO\.decklists\.data = (\{.*?\});", re.DOTALL)


def _slug_format(slug: str) -> str | None:
    for prefix, fmt in FORMAT_PREFIXES.items():
        if slug.startswith(prefix + "-"):
            return fmt
    return None


def fetch_recent_events(only_formats=("modern", "legacy", "standard", "pauper")) -> list[dict]:
    """解析 mtgo.com/decklists 首页，返回最近的赛事列表：
    [{format, event_type: "challenge"|"league"|其他, slug, url}, ...]
    event_type 从 slug 里判断（含 "-challenge-" 或 "-league-"）。
    """
    resp = requests.get(_INDEX_URL, headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    hrefs = re.findall(r'href="(/decklist/[^"]+)"', resp.text)

    events = []
    seen = set()
    for href in hrefs:
        if href in seen:
            continue
        seen.add(href)
        slug = href.split("/decklist/")[-1]
        fmt = _slug_format(slug)
        if fmt is None or fmt not in only_formats:
            continue
        if "-challenge-" in slug:
            event_type = "challenge"
        elif "-league-" in slug:
            event_type = "league"
        else:
            event_type = "other"
        events.append({"format": fmt, "event_type": event_type, "slug": slug, "url": _BASE_URL + href})
    return events


def fetch_event_decklists(url: str) -> dict:
    """抓取单场赛事页面，返回 {name, publish_date_or_starttime, format, decks: [{player, cards: {name: qty}}]}。

    League 页面只发布 5-0 战绩的套牌（Wizards 官方约定），这里额外按
    deck.wins.wins == "5" 过滤一遍以防万一；Challenge 页面则是该赛事全部
    参赛牌表（不筛战绩，因为 Challenge 本身就是竞技场次，能参赛的都算
    有效样本）。
    """
    resp = requests.get(url, headers=_HEADERS, timeout=90)
    resp.raise_for_status()
    m = _DATA_RE.search(resp.text)
    if not m:
        return {"name": None, "decks": []}
    data = json.loads(m.group(1))

    decks_out = []
    for deck in data.get("decklists", []):
        wins_info = deck.get("wins")
        if isinstance(wins_info, dict) and wins_info.get("wins") is not None:
            if wins_info.get("wins") != "5":
                continue
        cards = {}
        for entry in deck.get("main_deck", []):
            attrs = entry.get("card_attributes") or {}
            name = attrs.get("card_name")
            if not name:
                continue
            qty = int(entry.get("qty") or 0)
            cards[name] = cards.get(name, 0) + qty
        if cards:
            decks_out.append({"player": deck.get("player"), "cards": cards})

    return {
        "name": data.get("name") or data.get("description"),
        "date": data.get("publish_date") or (data.get("starttime") or "")[:10],
        "format": data.get("format"),
        "decks": decks_out,
    }
