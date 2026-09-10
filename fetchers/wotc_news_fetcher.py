"""Wizards of the Coast 官方禁限赛制公告抓取——"情报面"的利空/利好信号来源之一。

用官方 Banned & Restricted 页面（第一方数据，robots.txt 未限制，纯服务端渲染 HTML），
不猜测/遍历公告文章的 URL（那属于"撞库"，本项目明确不做），只抓这一个稳定的当前状态页面，
每天存一份快照，靠前后两天的差异来发现"新增禁牌/新增限制/解禁"这类事件。
"""

import html as html_lib
import re

import requests

from config import HTTP_USER_AGENT

BANNED_RESTRICTED_URL = "https://magic.wizards.com/en/banned-restricted-list"
_HEADERS = {"User-Agent": f"MOTrade/0.1 ({HTTP_USER_AGENT})"}

_SECTION_RE = re.compile(
    r'<h[23][^>]*>([A-Za-z][A-Za-z ]*?) (Banned(?: and Restricted)?|Restricted) Cards</h[23]>(.*?)(?=<h[23]|\Z)',
    re.S | re.I,
)
_UL_RE = re.compile(r"<ul>(.*?)</ul>", re.S)
_LI_RE = re.compile(r"<li>(.*?)</li>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")

# 页面里一些格式共用的"分类性"条目（不是具体卡名，比如"25张类型为Conspiracy的卡"），
# 不是我们关心的单卡信号，过滤掉避免弄脏事件记录。
_NOISE_HINTS = ("click", "cards with", "cards that", "work in progress", "no current")


def _clean_entries(raw_lis):
    out = []
    for li in raw_lis:
        text = html_lib.unescape(_TAG_RE.sub("", li)).strip()
        text = re.sub(r"\s+", " ", text)
        if not text:
            continue
        lower = text.lower()
        if any(hint in lower for hint in _NOISE_HINTS):
            continue
        out.append(text)
    return out


def fetch_banned_restricted_list() -> dict:
    """返回 {format_name: [card_name, ...]}，key 是页面上出现的格式名（如 "Modern"、
    "Legacy"、"Standard"、"Pauper"），value 是该格式当前被禁/限制的具体卡名列表
    （已经过滤掉"25张XX类型的卡"这类分类性描述条目）。同一个格式如果页面同时有
    Banned 和 Restricted 两个小节，会合并到一个 key 下面。"""
    resp = requests.get(BANNED_RESTRICTED_URL, headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    content = resp.text

    result: dict[str, list[str]] = {}
    for m in _SECTION_RE.finditer(content):
        fmt = m.group(1).strip()
        body = m.group(3)
        ul = _UL_RE.search(body)
        if not ul:
            continue
        names = _clean_entries(_LI_RE.findall(ul.group(1)))
        if not names:
            continue
        result.setdefault(fmt, [])
        for n in names:
            if n not in result[fmt]:
                result[fmt].append(n)
    return result


def diff_banned_restricted(previous: dict, current: dict) -> dict:
    """比较两次快照，返回 {format: {"added": [...], "removed": [...]}}，
    只包含真的有变化的格式。added = 新出现的禁/限卡（利空），
    removed = 从名单里消失的卡，也就是解禁（利好）。"""
    diff = {}
    formats = set(previous.keys()) | set(current.keys())
    for fmt in formats:
        before = set(previous.get(fmt, []))
        after = set(current.get(fmt, []))
        added = sorted(after - before)
        removed = sorted(before - after)
        if added or removed:
            diff[fmt] = {"added": added, "removed": removed}
    return diff
