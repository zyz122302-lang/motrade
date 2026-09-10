"""MTGTop8 线下（非 MTGO）2 星以上赛事牌表抓取，作为 MTGO 官方数据之外的
补充情报源（纸牌环境跟 MTGO 环境不完全一样，但仍有参考价值）。

合规性：mtgtop8.com 没有 robots.txt 限制（不像 mtggoldfish.com 明确 Disallow
ClaudeBot），页面是纯服务端渲染 HTML，不需要 JS 执行。

数据结构：
- format?f=MO / f=LE 这类赛制总览页列出近期赛事，每场赛事标了星级
  （<img src=/graph/star.png> 出现几次就是几星）和是否 MTGO 线上赛事
  （online/mtgo.png 图标）。
- 每场赛事的 event?e=<id>&f=<fmt> 页面左侧是名次榜（含每个名次对应的
  deck id），右侧默认显示第一名的牌表；具体某个 deck 的牌表用
  event?e=<id>&d=<deck_id>&f=<fmt> 打开，牌表直接内嵌在 HTML 里
  （id="md..." 是主牌库行，"sb..." 是备牌行，数量+卡名在同一个 div 里）。
"""

import re

import requests

from config import HTTP_USER_AGENT

_HEADERS = {"User-Agent": HTTP_USER_AGENT}
_BASE = "https://www.mtgtop8.com"

# MTGTop8 的赛制代码
FORMAT_CODES = {"modern": "MO", "legacy": "LE", "standard": "ST", "pauper": "PAU"}

_ROW_RE = re.compile(r'event\?e=(\d+)&f=\w+[^>]*>([^<]+)</a>')
_DECK_LINE_RE = re.compile(
    r'id=(md|sb)\w+ class="deck_line hover_tr" onclick="AffCard\([^)]*\);">\s*(\d+)\s*<span class=L14>([^<]+)</span>'
)


def fetch_offline_events(format_key: str, min_stars: int = 2) -> list[dict]:
    """返回某赛制近期"线下"（非 MTGO）赛事列表：
    [{event_id, title, stars, url}, ...]，只保留 stars >= min_stars 的。
    """
    fmt_code = FORMAT_CODES[format_key]
    resp = requests.get(f"{_BASE}/format?f={fmt_code}", headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    text = resp.content.decode("latin-1")  # 页面本身声明的是 latin-1/iso-8859-1

    events = []
    for block in re.split(r"(?=<tr)", text):
        m = _ROW_RE.search(block)
        if not m:
            continue
        event_id, title = m.group(1), m.group(2).strip()
        stars = block.count("graph/star.png")
        is_mtgo = "online/mtgo.png" in block
        if is_mtgo or stars < min_stars:
            continue
        events.append({
            "event_id": event_id,
            "title": title,
            "stars": stars,
            "url": f"{_BASE}/event?e={event_id}&f={fmt_code}",
        })
    # 去重（同一场赛事在页面里可能重复出现）
    seen = set()
    unique = []
    for e in events:
        if e["event_id"] in seen:
            continue
        seen.add(e["event_id"])
        unique.append(e)
    return unique


def fetch_event_deck_ids(event_url: str) -> list[str]:
    """从赛事页面的名次榜里取出所有 deck id。"""
    resp = requests.get(event_url, headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    text = resp.content.decode("latin-1")
    ids = re.findall(r"[?&]d=(\d+)", text)
    seen = set()
    out = []
    for i in ids:
        if i in seen:
            continue
        seen.add(i)
        out.append(i)
    return out


def fetch_deck_cards(event_id: str, deck_id: str, format_key: str) -> dict:
    """返回 {name: qty}，只统计主牌库（不含备牌，备牌不代表核心使用率）。"""
    fmt_code = FORMAT_CODES[format_key]
    url = f"{_BASE}/event?e={event_id}&d={deck_id}&f={fmt_code}"
    resp = requests.get(url, headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    text = resp.content.decode("latin-1")

    cards = {}
    for prefix, qty, name in _DECK_LINE_RE.findall(text):
        if prefix != "md":
            continue
        name = name.strip()
        cards[name] = cards.get(name, 0) + int(qty)
    return cards
