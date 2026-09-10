"""下载单卡卡图（按 mtgo_id），保存到本地文件。

每日分析 routine 用这个脚本把当天新入选卡的卡图存到本地，再用 Artifact 工具的
upload_asset 上传成仪表盘能显示的 /_blob/<id> 链接（详见
.claude/skills/motrade-analyze/SKILL.md 第 4 步）。

用法：python fetch_card_image.py <mtgo_id> <输出路径.jpg>
找不到图时打印 NO_IMAGE 到 stderr 并以状态码 1 退出，调用方应该跳过这张卡的
imageUrl 字段而不是让整个流水线失败。
"""

import sys

from fetchers.scryfall_fetcher import fetch_card_image_bytes


def main():
    if len(sys.argv) != 3:
        print("usage: python fetch_card_image.py <mtgo_id> <output_path>", file=sys.stderr)
        sys.exit(2)
    mtgo_id = int(sys.argv[1])
    out_path = sys.argv[2]
    data = fetch_card_image_bytes(mtgo_id)
    if data is None:
        print("NO_IMAGE", file=sys.stderr)
        sys.exit(1)
    with open(out_path, "wb") as f:
        f.write(data)
    print(out_path)


if __name__ == "__main__":
    main()
