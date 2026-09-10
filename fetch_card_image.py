"""下载单卡卡图（按 mtgo_id），保存到本地文件。支持传多个 mtgo_id 按顺序依次尝试，
第一个能找到图的就用哪个（比如最低价版本没被 Scryfall 收录时，退而求其次用次便宜的版本）。

每日分析 routine 用这个脚本把当天新入选卡的卡图存到本地，再用 Artifact 工具的
upload_asset（或者 upload_asset 不可用时的 base64 兜底方案）上传成仪表盘能显示的图片，
详见 .claude/skills/motrade-analyze/SKILL.md 第 4 步。

用法：python fetch_card_image.py <mtgo_id_1> [mtgo_id_2 ...] <输出路径.jpg>
（最后一个参数是输出路径，前面的都是按价格从低到高排好序的候选 mtgo_id）
全部候选都找不到图时打印 NO_IMAGE 到 stderr 并以状态码 1 退出，调用方应该跳过这张卡的
imageUrl/card_images 字段而不是让整个流水线失败。
"""

import sys

from fetchers.scryfall_fetcher import fetch_card_image_bytes


def main():
    if len(sys.argv) < 3:
        print("usage: python fetch_card_image.py <mtgo_id_1> [mtgo_id_2 ...] <output_path>", file=sys.stderr)
        sys.exit(2)
    *mtgo_ids, out_path = sys.argv[1:]
    for mtgo_id in mtgo_ids:
        data = fetch_card_image_bytes(int(mtgo_id))
        if data is not None:
            with open(out_path, "wb") as f:
                f.write(data)
            print(out_path)
            return
    print("NO_IMAGE", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
