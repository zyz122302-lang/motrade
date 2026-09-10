---
name: "motrade_analyze"
description: "生成 MOTrade 每日 MTGO 单卡分析报告并更新仪表盘。当需要跑每日数据更新、生成候选卡研判、或刷新 MOTrade 仪表盘信号时调用。"
---

# MOTrade 每日分析

本技能基于 `pipeline.py`（纯规则数据层）+ `strategies/*.yaml`（自然语言策略）+ Claude 自身的
综合判断，产出 MOTrade 的每日候选卡研判，并把结果写入仪表盘 Artifact 的数据库。

参考 ZhuLinsen/daily_stock_analysis 的分工方式：**指标计算是确定性代码，"这个信号值不值得关注"
是 Claude 结合策略描述做的综合判断**，两者不要混在一起写死。

## 每日赛事使用率（另一个独立的定时任务，不是这个技能负责触发）

`metagame_pipeline.py` 统计各卡在 Modern/Legacy/Standard/Pauper 的使用率，数据源两个：
MTGO 官方赛事牌表（mtgo.com/decklists，Challenge + 每日 League 5-0，四个赛制都做）+
MTGTop8 线下 2 星以上赛事牌表（只做 Modern/Legacy，每个赛制取星级最高的 3 场）。
**这个脚本由另一个 routine（每天 11:00 UTC 跑，比本技能的每日 routine 早 1 小时）负责触发，
不属于本技能的每日流程**，但本技能第 1 步会用到它产出的数据算出每张卡的 `formats`/`formatUsage`。
统计的是"过去 7 天滚动窗口"，不是自然周，所以每天都能拿到新的一份快照。

云端 routine 每次都是全新 checkout，本地 SQLite 不会跨次保留，所以持久化走 Artifact 仪表盘
自己的数据库（`metagame_usage` 集合，不是 git 仓库——测试过云端 routine 没有 push 权限）：
- **每日 metagame routine** 跑完 `metagame_pipeline.py` 后，读 `data/metagame_export.json`
  （脚本自动生成，结构 `{format: [{week_of, source, sample_size, usage}, ...]}`，同一个 format
  可能有 `mtgo_official` 和 `mtgtop8_offline` 两条 source），对**每个 format 的每个 source**
  用 `write_db`（`db_op: "set"`）各写一个文档到 `metagame_usage` 集合，doc_id 格式
  `{format}-{source}-{week_of}`（如 `modern-mtgo_official-2026-09-10`，注意不同 source 是
  不同文档，不能用同一个 doc_id 互相覆盖），内容为 `{format, week_of, source, sample_size, usage}`。
- **每日本技能**执行第 1 步之前，先用 `read_db`（`db_op: "query"`）查 `metagame_usage` 集合，
  按 `format` 分别查最近 60 条（`where: [["format","==","modern"]], order_by: {field:"week_of",
  direction:"desc"}, limit: 60`——因为现在一天最多两条（两个 source），60 条约等于一个月），
  把查到的文档拼成 `{format: [{week_of, source, sample_size, usage}, ...]}` 这个结构，写到本地
  `data/metagame_import.json`，再跑 `pipeline.py`——它会自动把这个文件灌回本地 SQLite
  （`import_metagame_usage_if_present`），同一天两个 source 的样本会自动合并算总占比。如果
  `metagame_usage` 集合还是空的（metagame routine 还没跑过一次），跳过这一步直接开始正常跑，
  `formats` 会是空数组，这是正常现象。

## 运行步骤

### 1. 跑数据管道（确定性代码，不需要 LLM）

```
cd "C:\Users\94243\OneDrive\桌面\MOTrade"
python pipeline.py
```

（记得先完成上面"每周赛事使用率"那一节里"每日本技能"要做的 read_db 桥接步骤，
再跑这个命令，不然 formats 会是空的。）

这一步会：
- 抓取 GoatBots 官方卖价批量数据（今日快照 + 首次运行时的年度历史）
- 抓取/复用 Scryfall 赛制合法性缓存（Modern + Legacy），以及 Cardhoarder 卖价（经 Scryfall
  `default_cards` 官方数据，按 mtgo_id 精确匹配，不抓 Cardhoarder 自己的网站）
- **按卡名聚合**候选池（不是按单个印刷版本）——同一张卡的所有已知版本（不同系列、foil/非foil）
  都归在这张卡下面，每个版本各自算 7日/30日/90日涨跌幅、MA7/MA30 等指标
- 写出 `data/latest_watchlist.json`：`signals`（下跌候选）和 `risers`（上涨候选）两个数组，
  每个元素是**一张卡**，带 `versions` 数组（该卡的所有版本明细）+ `bestPrice`/`bestSource`
  （该卡在所有版本、两个数据源里的最低价，用哪个数据源）+ 用"最低价版本"（primary version）算出
  的 chg7d/chg30d/low90/ma7/ma30 等指标（挂在卡这一级）

### 2. 读取输出，套用策略做研判

读取 `data/latest_watchlist.json` 的 `signals`（下跌候选）和 `risers`（上涨候选）两个数组，
每个元素是一张卡，含 `versions` 数组。对每一张候选卡（基于其 primary version 的指标），参考
`strategies/*.yaml` 里的五条策略描述（企稳 falling_knife 仍在下跌 / new_set_decay 新卡衰减 /
established_staple_dip 老卡折价 / momentum_up 上涨动量），判断这张卡属于哪一类，并结合你自己对
Modern/Legacy 赛制环境的了解，给出简短研判。如果同一张卡不同版本走势差异很大（比如某个促销版本
单独暴跌，其他版本稳定），在 note 里说明是哪个版本驱动的信号，不要笼统地说"这张卡"。
不确定的信息（比如某张卡是否真的还在被广泛使用）要明确标注是"数据面"还是"情报面"
（后者是你的定性判断，不是抓取来的事实）。

**分类不要只看跌幅/涨幅排名，要用比值判断动能方向**：`|chg_7d_pct| / |chg_30d_pct|`
比值高（比如 >0.6）说明大部分变动发生在最近一周，还在加速中（跌的归 falling_knife，
不是 stabilizing；涨的要提醒追高风险）；比值低说明变动已经发生一段时间、近期趋缓，
才是真正的 stabilizing / established 候选。另外注意：`chg_30d_pct` 在基准价格接近 0 的
低价卡上会出现几百甚至几千个百分点的失真（比如从 $0.06 涨到 $3.8 就是 +6000%），
这种情况**不要凭训练知识猜原因**——你的知识截止日期早于今天，赛制变化/新系列/禁限赛制公告这些
最近事件你大概率不知道。看到异常涨跌（尤其是低基数暴涨、或者知名卡突然大跌）时，先用 `WebSearch`
搜一下这张卡最近的英文/中文资讯（比如 "<卡名> price spike/drop <月份> <年份>"、"<卡名> banned
Pauper/Modern/Legacy"），确认真实原因后再写 note。确实搜不到解释的，才在 note 里如实写"低基数补价
修正/原因未确认"，不要不搜就下结论——之前就因为没搜索，把 Zeta Set 降级重印带来的 Pauper 新合法性
（真实需求）误判成了"bot 补库存价格修正"，这是要避免重犯的错误。

每张卡的研判尽量精炼成这几部分（对应原项目的四段式 dashboard，但不需要字段名完全一致）：
- **结论**：一句话，属于哪个策略分类 + 要不要现在关注
- **数据面**：现价、7日/30日变动、是否接近90日低点
- **情报面**：如果你知道这张卡在当前赛制里的地位变化（例如新禁令、环境降温），补充说明；
  不知道就不要编
- **操作建议**：具体到"可以试探性低吸/建议观望/风险较高"，并提醒买价仍需去 MTGO 客户端
  实际查看后手动记录（`docs/PRINCIPLES.md` 里的原则：买价不做自动化抓取）

### 3. 挑选本轮真正值得推送的候选（不要全量塞进仪表盘）

下跌候选里只挑 `falling_knife`（仍在下跌，但值得关注/警示）和 `established`（企稳或老卡折价）
这两类；上涨候选里只挑 `momentum`（真实动能）和 `caution`（数值异常但值得记录，比如低基数补价）
——不要为了凑数硬塞噪音（比如纯 EDH/Commander 需求驱动、和 Modern/Legacy 竞技关系不大的卡，
可以直接不选或标 caution 并说明原因）。

仪表盘按赛制（标准/摩登/薪传/纯铁）分开展示，每个赛制最多显示 10 张卡，所以选卡时按
`data/latest_watchlist.json` 里每张卡的 `formatUsage` 字段（见下一节的周度数据）分别看：
每个赛制下跌/上涨各挑不超过 10 张最值得看的；没有 `formatUsage`（不在任何赛制的周度使用率
统计里）的卡仍然可以选，仪表盘会放进"全部"里，但不会出现在具体赛制分类下——这类卡通常是
Cube/Commander 向的，情报面判断时要说明"未见近期 Modern/Legacy/Standard/Pauper 竞技赛事采用"。

### 4. 写入仪表盘数据库

目标 artifact：`https://claude.ai/code/artifact/1e6623c1-9314-4778-bb5a-83f689947e9e`

**a) `watchlist` 集合**（doc_id 用卡名的 slug，比如 "Baleful Strix" → `baleful-strix`：
小写、非字母数字换成短横线），用 `write_db`（`db_op: "batch"`）写入，**一张卡一个文档**：
```
{
  name, rarity, versionCount, asOf,
  bestPrice, bestSource,       // "goatbots" | "cardhoarder"，所有版本+两个数据源里的最低价
  primaryMtgoId,                // 达成 bestPrice 的那个版本的 mtgo_id
  chg7d, chg30d, low90, ma7, ma30,   // 取自 primary version（最低价那个版本）的指标
  formats: ["modern","legacy"],   // 直接抄 latest_watchlist.json 里这张卡的 formats 数组
  direction: "rise",   // 上涨候选必须带这个字段；下跌候选不用带（省略即默认下跌）
  verdict: "stabilizing" | "falling_knife" | "established" | "momentum" | "caution" | "new_set",
  note: "一两句话的研判，中文，供仪表盘详情页展示",
  imageUrl, imageAssetId,   // 卡图，见下面"卡图"小节；抓不到图就都不要写这两个字段
  versions: [   // 该卡的全部已知版本，仪表盘详情页用这个渲染版本切换器
    { mtgoId, set, foil, goatbotsPrice, cardhoarderPrice, chg7d, chg30d, low90, ma7, ma30 },
    ...
  ]
}
```
verdict 的取值必须是上面枚举里的英文 key（仪表盘 CSS/文案按这几个 key 渲染），不要自己发明新词。
`versions` 数组直接从 `data/latest_watchlist.json` 里对应卡片的 `versions` 字段取，字段名要转成
驼峰（`mtgo_id`→`mtgoId`、`goatbotsPrice`/`cardhoarderPrice`/`chg_7d_pct`→`chg7d` 等）。

**卡图（每张入选卡都要配）**：写入前先 `read_db`（`db_op: "get"`）看这张卡在 `watchlist`
集合里已有的文档：
- 如果已有文档的 `primaryMtgoId` 跟这次一样、且带着 `imageAssetId`（assets 路径）或者
  `card_images` 集合里已经有这个 mtgoId 的文档（data URI 路径，见下面兜底方案），说明图已经
  有了，不用重新下载上传。
- 否则（新卡，或者 `primaryMtgoId` 变了——比如更便宜的新版本上线）：
  1. `python fetch_card_image.py <primaryMtgoId> data/images/<primaryMtgoId>.jpg`
     下载卡图到本地。如果脚本退出码是 1（打印 `NO_IMAGE`），说明 Scryfall 没收录这个印刷
     版本的图，跳过卡图，不要中断整个流水线。
  2. **优先方案**：用 `Artifact` 工具的 `upload_asset`（`url` = 仪表盘 artifact 链接，
     `file_path` = 刚下载的图片路径）上传，拿到返回的 `{id, url}`，把 `imageUrl = url`、
     `imageAssetId = id` 填进这张卡的 `watchlist` 文档里。
  3. **兜底方案（`upload_asset`/`list_assets` 这类 asset 相关 action 在当前环境不可用时）**：
     把图片文件 base64 编码成 `data:image/jpeg;base64,<...>` 格式的 data URI（单张图一般
     100-300KB，编码后一般在几百 KB，远低于单个文档 1MB 上限，不用担心超限），写入
     `card_images` 集合，doc_id 用 `String(primaryMtgoId)`，文档内容 `{dataUri: "..."}`
     （多张一起写时注意 `write_db` 单次 batch 请求体上限 1MB，建议每批 4 张左右）。这种情况下
     **不要**往 `watchlist` 文档里塞 `imageUrl`/`imageAssetId` 字段——仪表盘详情页会在没有
     `imageUrl` 时自动按 `primaryMtgoId` 去 `card_images` 集合里找对应的图（`loadCardArt`
     函数），两条路径都支持，不需要在 SKILL 这边判断走哪条，只要 `upload_asset` 报错
     （比如报 "Invalid input" 提示这个 action 不在允许列表里）就直接切到兜底方案即可。

**清理旧卡的图**：本节前面"写入前建议先 `read_db`...把这次不再入选的旧文档删掉"那一步，
删除旧 `watchlist` 文档时：如果它带 `imageAssetId`，用 `Artifact` 工具的 `delete_asset`
（`url` = 仪表盘 artifact 链接，`asset_id` = 那个 `imageAssetId`）把对应的图也删掉；如果这张
卡走的是兜底方案（没有 `imageAssetId`），改为用 `write_db`（`db_op: "delete"`）删掉
`card_images` 集合里 doc_id 为该卡 `primaryMtgoId` 的文档。两种情况都是为了避免评级下滑/
换卡之后旧图片一直占着数据库/资源配额。

**b) `price_history` 集合**（doc_id 用 mtgo_id 字符串，**每个版本一份**，不是每张卡一份）：
```
{ series: [[date_str, price], ...] }   // 从 storage.price_history_for(conn, mtgo_id) 取，
                                          // 按需抽稀到 20-30 个点即可（平段可以跳过中间重复值），
                                          // 单文档不要超过几十 KB
```
一张卡如果有 N 个版本，就要写 N 份 `price_history` 文档（用各自的 mtgo_id 做 doc_id），
仪表盘详情页切换版本时会按 mtgoId 单独去取对应这份历史画图。
仪表盘详情页会用这份数据画价格曲线 + MA7/MA30（MA 由页面前端从 series 现算，不用额外传）。

写入前建议先 `read_db`（`db_op: "list"`, collection `watchlist`）看当前有哪些文档，
把这次不再入选的旧文档删掉（连同它对应的 `price_history` 文档一起删），避免仪表盘堆积过期信号。

### 5. 通知（可选）

如果本轮出现了"老卡折价"这类可信度较高的信号，且历史上不常见（比如常年主力卡罕见地大跌），
可以用 `PushNotification` 简短提醒用户去看一眼仪表盘。信号平淡的日子不用打扰。

## 输出去哪里

分析结果 = 仪表盘数据库更新（用户随时打开链接查看），不需要在本地再生成额外的报告文件。
如果想留档，可以把当天的研判追加写入 `data/reports/YYYY-MM-DD.md`（纯本地文件，不会被
git 跟踪，见 `.gitignore`）。

## 原则提醒

调用本技能时同样必须遵守 `docs/PRINCIPLES.md`：只用 GoatBots/Scryfall 官方公开数据、
不抓买价、不做客户端自动化、不把用户账号信息当作分析输入。
