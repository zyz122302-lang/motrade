"""抓一份当前的官方禁限赛制名单快照，写到 data/banned_restricted_current.json。

用法：python check_banned_restricted.py

只负责抓取+落地本地文件；跟"昨天的快照"比对、判断是否有变化、写回仪表盘数据库这些
带状态的步骤，交给每日 routine（有 Artifact 工具的那个 Claude 会话）来做——见
.claude/skills/motrade-analyze/SKILL.md 里"官方禁限赛制公告监控"那一节。
云端每次都是全新环境，本脚本自己不保留历史，所以不在这里做 diff。
"""

import json

from config import DATA_DIR
from fetchers.wotc_news_fetcher import fetch_banned_restricted_list

OUT_PATH = DATA_DIR / "banned_restricted_current.json"


def main():
    snapshot = fetch_banned_restricted_list()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    total = sum(len(v) for v in snapshot.values())
    print(f"wrote {OUT_PATH}: {len(snapshot)} formats, {total} banned/restricted entries")


if __name__ == "__main__":
    main()
