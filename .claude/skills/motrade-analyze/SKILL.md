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

## 每周研究任务（另一个独立的定时任务，不是这个技能负责触发）

`research_pipeline.py` 用 GoatBots 两年历史卖价回溯构造训练样本，分两个**板块**（互相独立的
二分类问题，共用同一份特征矩阵/同一批训练锚点，只是标签方向和阈值不同）：
- **反弹板块**（`rebound`）：某个信号出现后，价格是否会涨超过 10%（`REBOUND_THRESHOLD`）。
- **持续下跌板块**（`decline`）：价格是否会继续跌超过 10%（`DECLINE_THRESHOLD`）——这是给
  已经在跌的卡做"还会不会继续跌"的风险提示，不是"预测该抄底"，跟反弹板块的解读方向相反，
  写研判时不要把两个板块的候选混着套同一套措辞。

每个板块下都**同时**训练 7/14/30 三个预测窗口的 LightGBM 分类器（一共 6 个模型：
2 板块 × 3 窗口）。目的是**校准** `strategies/*.yaml` 里的人工规则（尤其是
`supply_vs_demand_framework.yaml`），不是替代它，也**不接入本技能的每日流程**——由另一个
routine（每周日 09:00 UTC 跑一次，比每日流程慢得多，要下载多年历史归档）单独触发。

特征除了价格历史，还包括赛制使用率、官方禁限事件、系列发售时间、30日换手率代理指标
（`liquidity_rate`，从已有卖价序列直接算出，不额外抓取新数据源）。该 routine 跑
`research_pipeline.py` **之前**要先做两步桥接（用 Artifact
工具，跟每日本技能第 1 步桥接使用率是同一个思路）：
1. `read_db`（`db_op: "query"`）查 `metagame_usage` 集合（跟每日本技能一样，按 format 分别
   查最近的记录），拼成 `{format: [{week_of, source, sample_size, usage}, ...]}`，写到
   `data/metagame_import.json`。
2. `read_db`（`db_op: "list"`）查 `news_events` 集合的全部文档，转成
   `[{date, format, added: [...], removed: [...]}, ...]`（直接取每个文档的 `date`/`format`/
   `added`/`removed` 字段即可），写到 `data/br_events_import.json`。
两个文件都是可选的——不存在时 `research_pipeline.py` 会跳过对应特征（打印提示，不报错）。
使用率/禁限事件这两类特征目前历史覆盖率很低（`metagame_usage`/`news_events` 才刚开始积累，
而且"需要未来N天数据算标签"这个要求会让训练锚点的日期天然落后于"今天"，所以覆盖率要再过
一段时间才会明显提高），`research_output.json` 里的 `featureCoverage` 字段会如实报告每个
特征的非缺失比例，不需要因为覆盖率低就怀疑代码有问题——这是数据积累时间不够的正常反映。

产出写进仪表盘数据库的 `research_runs/latest` 文档（结构：`{runDate, status, universeSize,
totalAnchorRows, featureCoverage, panels: {"rebound": {label, threshold, windows: {"7": {...},
"14": {...}, "30": {...}}}, "decline": {label, threshold, windows: {...}}}}`，每个窗口下有
自己的 `trainRows`/`validRows`/`validAccuracy`/`baselineAccuracy`/`validAuc`/
`featureImportance`/`topCandidates`），前端"数据研究"页签有板块切换 + 窗口切换两层按钮，
直接展示各板块各窗口的模型表现和特征重要性，供参考，不影响每日研判逻辑。

`topCandidates` 已经在 `research_pipeline.py` 里按预测概率 >50% 过滤、每个板块每个窗口最多
截断到 10 张（见脚本里的 `DISPLAY_MIN_PROB`/`DISPLAY_MAX_PER_WINDOW`），routine **不需要**
自己再做这层过滤/截断，但**需要**给两个板块、每个窗口筛出来的候选卡逐一研判——不能只把裸的
`predictedReboundProb`/`predictedDeclineProb` 数字丢给用户。反弹板块的候选卡字段是
`predictedReboundProb`，持续下跌板块是 `predictedDeclineProb`，两个板块合计最多 60 张候选
（2 板块 × 3 窗口 × 10 张），研判方法跟每日本技能第 2 步基本一致，但方向解读不同：
- 参考 `strategies/*.yaml`（尤其是 `supply_vs_demand_framework.yaml`——研究流水线的模型信号
  本身不知道供给/需求驱动的区别，这一步就是补上这个判断），给每张候选卡定性成
  `stabilizing`/`falling_knife`/`established`/`momentum`/`caution` 里的一个（取值必须是这几个
  英文 key，仪表盘按这几个渲染）。
- **反弹板块**：模型预测"会涨"，判断这个信号能不能追溯到真实的需求端证据（解禁、新套牌采用、
  赛制合法性变化），能就可以标 `momentum`/`established`，找不到证据的（尤其是低基数补价这类）
  倾向 `caution`，规则跟每日本技能第 2 步的"反弹判断"完全一样。
- **持续下跌板块**：模型预测"会继续跌"，这是风险提示不是买入信号——如果能确认下跌还在加速
  （比如 `|chg_7d_pct|/|chg_30d_pct|` 比值高）或者有具体利空（新禁令、供给持续释放且没有见底
  迹象），倾向标 `falling_knife`，提醒"还没到抄底的时候"；如果价格已经跌了很久、模型只是
  基于历史相似形态给出较高的继续下跌概率但近期跌势其实已经放缓，如实在 note 里点破这种
  "模型信号跟当前技术面出现分歧"的情况，标 `caution`，不要因为板块叫"持续下跌"就无脑标
  `falling_knife`。
- 两个板块的候选卡如果是纯指挥官(EDH)/收藏向/Un-系列卡（不在摩登/薪传竞技环境里，哪怕
  Scryfall 判定它"legal"），要在 note 里点破——这类卡的赛制合法性对模型来说是"合法"，但实际
  没有竞技情报支撑，信号可信度天然更低，一般应该标 `caution`。
- 涨跌幅明显、原因不确定的，先 `WebSearch` 核实（跟每日流程同一条规则，不要凭训练知识猜，
  确实搜不到就如实写"原因未确认"）。
- 如果某张候选卡名字跟当日 `watchlist` 集合里的卡重复（比如同一张卡这周同时被选中），直接
  沿用当日观察池那边已经写好的研判结论，保持口径一致，不要重新编一套不一样的说法；同一张卡
  如果同时出现在反弹板块和持续下跌板块（不同窗口给出相反方向的高概率预测，理论上可能发生），
  在两边的 note 里都如实说明这个矛盾，不要各写各的、互相打架。
- 把结果写回每个候选卡对象的 `verdict`/`note` 字段（跟每日 `watchlist` 文档同一套字段名），
  再整体写入 `research_runs/latest`。

**研判存档**（供"研判命中率回评"到期后核对用，见每日本技能里的同名小节）：给两个板块、每个
窗口里最终写进 `research_runs/latest` 的每一张 `topCandidate`，用 `write_db`
（`db_op: "batch"`）各写一条 `verdict_log` 集合的文档，doc_id 用
`{runDate}-{卡名slug}-research-{板块}-{窗口天数}`（比如
`2026-09-13-baleful-strix-research-rebound-14`），内容：
```
{
  date: runDate, name, mtgoId, source: "research-rebound-14",   // source 按板块+窗口拼，
                                                                  // 板块是 "rebound"/"decline"
  verdict, direction: null, priceAtCall: price, horizonDays: 14,   // horizonDays 就是这个候选卡所在的窗口（7/14/30）
  rarity, scored: false, scoredAt: null, priceAtHorizon: null, actualChgPct: null, hit: null
}
```
这一步只是存档，不影响 `research_runs/latest` 本身的写法。

这一步需要 routine 的 `allowed_tools` 里有 `WebSearch`（创建/更新这个 routine 时记得带上，
不要只给 `Bash`/`Read`/`Write`/`Artifact`）。

## 官方禁限赛制公告监控（利空/利好情报，本技能自己负责，每天都跑）

价格和使用率是"技术面"，禁限赛制变化和新系列上线是"情报面"里最硬的两类利空/利好信号——
禁一张卡=这张卡的竞技需求基本归零（利空），解禁=需求可能回归（利好），这两种事件经常先于
价格变化出现，或者是价格突变的真正原因（比如之前 Phlage 被禁 Modern 导致它持续下跌）。

**数据源**：`https://magic.wizards.com/en/banned-restricted-list`——官方当前禁限名单页，
纯服务端渲染 HTML，`robots.txt` 无限制。**不**去猜测/遍历具体某次公告文章的 URL（那属于
"撞库"而不是访问公开数据，本项目不做），只抓这一个稳定的"当前状态"页面，靠每天的快照差异
发现变化，不需要知道公告是哪天发的。

步骤：
1. `python check_banned_restricted.py`——抓取当前禁限名单，写到
   `data/banned_restricted_current.json`（结构 `{format: [card_name, ...]}`）。
2. `read_db`（`db_op: "get"`，collection `site_meta`，doc_id `banned_restricted_snapshot`）
   读昨天的快照。如果这个文档不存在（第一次跑），跳过对比，直接把今天的快照写进去做基线，
   不生成事件。
3. 如果昨天的快照存在，用 `fetchers/wotc_news_fetcher.py` 里的
   `diff_banned_restricted(previous, current)` 函数算出差异（按格式返回
   `{format: {"added": [...], "removed": [...]}}`，`added` = 新增禁/限 = 利空，
   `removed` = 解除禁/限 = 利好）。可以直接 `python -c` 调用这个函数，传入两份 JSON。
4. 对每个有变化的格式，判断变化的卡是否跟本轮候选池、或者仪表盘现有 `watchlist` 文档里的卡
   同名：
   - 如果是，**这条信息优先于其他一切情报**写进那张卡本轮的 `note` 里（比如"该卡已于近日被
     官方公告禁用于 Modern，利空，需求会大幅下滑"），并相应调整 `verdict`（新增禁令通常应该
     标 `falling_knife` 或至少 `caution`，不要标 `momentum`；解除禁令通常是 `momentum` 候选）。
   - 不管是否命中候选池，都要用 `write_db`（`db_op: "set"`）写一条 `news_events` 集合的文档，
     doc_id 用 `{date}-br-{format的slug}`（如 `2026-09-11-br-modern`），内容：
     ```
     { date, type: "banned_restricted", format, added: [...], removed: [...],
       summary: "一句话中文说明，比如：Modern 新增禁用 X 张卡（含 XX），Legacy 解除禁用 Y" }
     ```
     这条会在仪表盘顶部的"情报速递"条里显示，即使没命中当前候选池，用户也能看到。
5. 把 `data/banned_restricted_current.json` 的内容 `write_db`（`db_op: "set"`）覆盖写回
   `site_meta/banned_restricted_snapshot`，作为明天对比的新基线（不管今天有没有变化都要写，
   保持基线是"昨天"而不是越来越旧）。这份文档同时也是仪表盘首页"禁限牌情况概览"板块直接读取
   展示的数据源（前端按 `{format: [card_name, ...]}` 原样渲染），所以哪怕今天没有变化、不需要
   写 `news_events` 事件，这一步也不能省略。

**供给侧信号监控（`news_events` 的 `type: "supply_open"`，定性判断，不是确定性代码）**：
MTGO 单卡不是股票，供给几乎全由 WotC/MTGO 运营方单方面控制——用户明确指出过这一点，下面
三类是目前已知会集中释放供给的具体机制，**不要只被动等某张候选卡出现异常涨跌才去查**，
每天/每周主动过一遍这三类，能提前解释未来的价格异动，而不是事后才补研判：

1. **新系列上线，且系列里有老卡重印**：新系列上线本身只是"日期"，真正影响供给的是它有没有
   重印某些老卡（拆包/新版本进入流通会压低老版本溢价）。`WebSearch` "<系列名> reprints
   spoiler"或"<系列名> MTGO release date"，确认具体重印了哪些卡（不是笼统说"这个系列会
   释放供给"，要点名，比如"Reality Fracture 重印了 Chandra, Torch of Defiance"）。
2. **Treasure Chest 卡池/内容概率表更新**：MTGO 官方大约每 3-4 周刷新一次 Treasure Chest
   的内容概率表（可以拆出什么，参考"Treasure Chest 价格追踪"一节）。如果 `WebSearch` 能
   查到这次刷新具体加入/移出了哪些单卡（不只是影响 Treasure Chest 自己的价格），把受影响
   的卡也点名写进事件里——被移出卡池的卡少了一个供给渠道（偏利多），新加入的卡多了一个
   供给渠道（偏利空）。查不到具体卡名的话，只记刷新日期本身，不用编。
3. **MTGO 限时寻回老系列的 Limited 活动**（Retired Set Draft/Sealed，跟前面"每周研究任务"
   提到的 `set_age_days` 特征背后的假设是同一个道理）：MTGO 会不定期用退役多年的老系列开
   限时的 Draft/Sealed 活动（比如 2026 年出现过的 Urza Block Keeper Draft、LOTR/Hobbit
   Block Sealed），这类活动如果是 "Keeper"（玩家能留下抽到/拆到的卡），就是让那个老系列的
   实体重新流入市场——`WebSearch` "MTGO limited events schedule" 或者
   `mtgo.com/limited-events` 能查到近期安排。不确定某场活动是不是 Keeper 模式时，如实在
   note 里写"具体是否可保留视官方公告而定"，不要替用户下结论。

三类只要确认了，都用 `write_db`（`db_op: "set"`）写一条 `news_events` 集合的文档，doc_id 用
`{date}-supply-{关键词slug}`（如 `2026-09-11-supply-reality-fracture-reprints`），内容：
```
{ date, type: "supply_open", cards: ["Chandra, Torch of Defiance", ...], summary: "一句话
  中文说明，比如：Reality Fracture（10月2日发售）重印 Chandra, Torch of Defiance 等卡，
  预计释放这些老版本的供给，价格下跌不代表需求下滑" }
```
`cards` 数组点名越具体越好，实在没有具体卡名（比如只知道刷新日期）就传空数组，`summary`
里说清楚是哪一类机制。这条会出现在仪表盘首页"供应端动态"板块。如果这次监控发现的供给事件
正好命中当前候选池里的某张卡，除了写事件，**还要**把判断同步写进那张卡本轮的 `note` 里，
保持两边口径一致，不要各说各话。这一步是事件驱动的（没有新发现就不用硬写，大多数天三类都
没有新变化是正常情况），不像禁限公告监控那样每天必须产出内容，但每天都要主动检查一遍。

## MTGO 综合资讯监控（首页"最新资讯"板块，判断尺度交给你自己把握，本技能自己负责，每天都跑）

首页的"最新资讯"板块**不能只有禁限赛制公告**——用户明确要求过这一点。禁限公告只是"情报面"里最
确定的一类信号，但 MTGO 客户端更新、新系列上线/维护窗口安排、赛事结构变化（比如 Challenge/
League 的奖励结构调整、Qualifier 积分规则变化）等，只要可能影响到单卡或 Treasure Chest 的
供给/需求，都值得记录。**这里没有确定性代码可以判断"这条新闻算不算数"，需要你自己看着办**：

- 每天（或至少每周）`WebSearch` 一下 "MTGO weekly announcement" + 本周日期、或直接看
  `mtgo.com/news` 最新几条，扫一眼有没有值得记的东西。不需要每天都有新发现——大多数周可能
  什么都不用写，平淡的客户端维护通知、纯娱乐性内容不需要记录。
- 判断标准：这条消息会不会让某类卡（或 Treasure Chest 本身）的供给或需求发生变化？例如新系列
  上线日期（供给面，跟前面"MTGO 新系列上线"那段是同一件事，两边可以共用一条事件）、赛制轮替、
  赛事奖励结构调整（直接影响 Treasure Chest 的发放频率，见下一节）、客户端重大功能变化影响交易
  便利性等。纯粹的活动宣传、UI 更新这类不影响供需的内容不用记。
- 判断结果用 `write_db`（`db_op: "set"`）写一条 `news_events` 集合的文档，doc_id 用
  `{date}-mtgo-{关键词slug}`（如 `2026-09-01-mtgo-reality-fracture`），内容：
  ```
  { date, type: "mtgo_news", format: "", summary: "一句话中文说明这件事以及可能的利空/利好方向" }
  ```
  `format` 留空字符串即可（这类消息通常不是针对单一赛制）。

## Treasure Chest 价格追踪（仪表盘首页专属板块，本技能自己负责，每天都跑）

Treasure Chest 是 MTGO 独有的可交易物品（不是印刷的 Magic 卡牌，GoatBots 把它记成
`cardset="O99"`、`rarity="Booster"` 的特殊条目），`watchlist_builder.py` 的
`NORMAL_RARITIES` 过滤规则天然把它排除在候选池外，所以它**永远不会出现在下跌/上涨观察
列表里**——仪表盘首页有专属的价格 + 走势图板块，点进去是它自己的详情页（跟单卡详情页布局
一致，复用同一套 `note-box`/`verdict-chip` 组件），但**不套用** `strategies/*.yaml`
里针对单卡赛制供需写的那套框架（它没有"赛制使用率""禁限赛制"这些概念）。

**Treasure Chest 自己的供需框架（写 note 前先想一遍）**：它的价格本质是"开一个箱子的期望
价值"的实时定价，供给端取决于玩家参与 Challenge 等赛事的频率（赢得的箱子数量），需求端取决于
想拆箱子换取内容物（Play Points/单卡/神器代币等）的玩家数量，两者通常都比较稳定，不会像单卡
那样出现"禁令"这类突发利空。官方会定期刷新箱内内容概率表（大约每3-4周一次，可以
`WebSearch` "MTGO Treasure Chest contents update" 查最近一次和下一次刷新日期），这通常是
它价格波动最主要的来源；除此之外，赛事奖励结构变化（比如 Challenge 改成用箱子发奖还是用
Play Points 发奖）也会直接影响它的供给。多数时候它的价格会在一个比较窄的区间里稳定波动，
这种"稳定"本身就是正常状态，不代表有交易信号，不要为了凑研判硬找一个方向性结论。

步骤：
1. 先做一次桥接（跟"每日赛事使用率"那一节的思路一样，避免每天重新下载两年历史）：
   `read_db`（`db_op: "get"`，collection `price_history`，doc_id 是 Treasure Chest 的
   mtgo_id 字符串——固定是 `"62245"`，脚本第一次跑时会打印出来，如果 GoatBots 哪天改了
   这个条目的 id，以脚本打印的为准）读回它现有的 `series` 字段，写到
   `data/treasure_chest_import.json`（结构 `{"series": [[date_str, price], ...]}`）。
   如果这个文档还不存在（第一次跑），跳过这一步，脚本会自动退回一次性下载最近两年归档来
   建立历史基线（比正常的每日更新慢很多，只会发生一次）。
2. `python fetch_treasure_chest.py`——输出 `data/treasure_chest.json`，结构：
   `{ mtgoId, name, cardset, asOf, price, chg_7d_pct, chg_30d_pct, chg_90d_pct, low_90d,
   high_90d, ma7, ma30, days_of_history, series }`。
3. 参考上面的框架给这次的价格走势定性成 `stabilizing`/`falling_knife`/`established`/
   `momentum`/`caution` 里的一个（复用单卡那几个 key，方便仪表盘统一渲染，含义按 Treasure
   Chest 自己的逻辑理解，不是按单卡赛制供需理解），写一两句 `note`（现价、7日/30日变动、
   是否有已知的内容刷新/赛事结构变化能解释走势，解释不了就如实写"正常区间内波动，无信号"），
   把这两个字段加进第2步的 JSON 里。
4. 把（加了 `verdict`/`note` 之后的）这份 JSON 拆成两份写：
   - 去掉 `series` 字段后的其余内容，用 `write_db`（**`db_op: "update"`，不是 `"set"`**——
     这个文档里还带着下面第5步维护的 `nextRefreshDate`/`refreshHistory` 字段，用 `set`
     会把它们整体覆盖清空，必须用 `update` 合并写入）写到 `special_items/treasure_chest`
     （首页价格面板和详情页都读这个文档，字段名保持跟 `data/treasure_chest.json` 一致，
     不用转驼峰）。
   - 只取 `series` 字段，包成 `{series: [...]}`，用 `write_db`（`db_op: "set"`，这份是独立
     文档，正常整份覆盖没问题）写到 `price_history/{mtgoId}`（跟普通单卡版本共用同一个集合，
     仪表盘的走势图直接复用现成的 `buildChart` 渲染逻辑）。
5. **维护刷新日历**（仪表盘详情页"下次内容刷新预计"+"历史刷新日期"板块的数据源，字段是
   `nextRefreshDate`（字符串日期）和 `refreshHistory`（`[{date, note}, ...]`数组），都存在
   `special_items/treasure_chest` 文档里，跟上一步同一份 `update` 一起写，不用分开调用）：
   - 先 `read_db`（`db_op: "get"`）读一下这个文档现有的 `nextRefreshDate`。
   - 如果**今天的日期还没到那个 `nextRefreshDate`**，通常不用改——但如果 WebSearch
     "MTGO Treasure Chest contents update" 查到官方把下次日期改了（比如推迟），就更新成
     新查到的日期。
   - 如果**今天的日期已经到了或过了**现有的 `nextRefreshDate`：说明那次刷新已经生效，把它
     追加进 `refreshHistory`（`note` 写这次刷新绑定的系列名，或者"常规轮换"，从 WebSearch
     或本次 `note` 里的判断取），再 WebSearch 查一下官方页面公布的**新一期**"下次更新"日期，
     更新 `nextRefreshDate`。查不到新日期就把 `nextRefreshDate` 留空，不要编。
   - 同时维护 `nextRefreshType`（取值 `"launch"`/`"routine"`/`"uncertain"`）和
     `nextRefreshNote`（一两句话说明判断依据）：WebSearch 查一下这个日期前后有没有新系列在
     MTGO 上线，日期精确对上就标 `"launch"`，明显对不上（比如像 2026-09-29 那次，真正的
     Reality Fracture 发售是 10/2，差了3天）就标 `"uncertain"`，注明"官方日期可能会改期贴合
     新系列，建议临近时再核实"；确认这次是纯常规轮换（没有系列上线）就标 `"routine"`。
6. **维护入场时机判断**（仪表盘"入场时机判断"板块，字段 `buyTiming`（取值
   `"favorable"`/`"neutral"`/`"unfavorable"`）和 `buyTimingNote`，同样存在这个文档里）——
   这是基于历史统计验证过的框架，不是每天重新分析一遍，只需要每天核对一次现价+刷新日历有没有
   变化：
   - 历史统计结论（已验证，直接引用即可，不用重新跑）：**发售绑定型**刷新生效后7天平均涨约
     +3.4%（8次样本，5次正收益，自举检验 p=0.001，站得住），**常规轮换型**刷新后基本没有
     可靠涨幅（均值接近0，跟随机噪音分不开）；而且发售绑定型刷新生效**前**一周价格通常已经
     开始温和上涨（均值+1.85%），不存在"等便宜"这个低点，越早确认越早入场越好。
   - 判断规则：
     - `nextRefreshType` 是 `"launch"` 且还没到期 → 标 `favorable`，note 里提醒"确认为发售
       绑定型，历史上这类刷新生效后7天平均有正收益，可以考虑提前布局，不用等更低价"。
     - `nextRefreshType` 是 `"routine"` 或者没有临近的刷新 → 标 `neutral`，note 里如实说
       "当前没有基于刷新日历的强烈入场理由"，可以结合现价在90日区间的位置（`low_90d`/
       `high_90d`，仪表盘前端自己会算百分位显示，不用在 note 里重复算）稍微加一句参考。
     - 价格明显处于90日区间高位（比如 >85%）同时又没有发售绑定型刷新临近 → 可以标
       `unfavorable`，提醒"现价接近90日高点，缺乏催化剂，不是理想的新增仓位置"。
     - `nextRefreshType` 是 `"uncertain"` → 参考对应的 `nextRefreshNote`，note 里如实写
       "下次刷新类型未确认，暂不基于刷新日历判断，建议临近日期核实后再决定"，通常标 `neutral`。
   - 这一套判断是给"买入-等刷新后卖出"这类短线操作用的参考，不是买卖建议，note 里可以保留
     "仅供参考，不构成交易建议"这类免责措辞。

## 买卖价差统计（仪表盘"买价记录"页签自己的板块，本技能自己负责，每天都跑）

GoatBots/Cardhoarder 的买价（bot 愿意收多少钱）故意做了防抓取处理（SVG 字形渲染，见
`docs/PRINCIPLES.md`），**不能也不应该**绕过去自动抓——这是网站运营方明确的反爬信号，"人眼能
看见"不等于"允许自动化抓取"。但仪表盘"买价记录"页签（`buy_observations` 集合）已经在积累一份
完全合规的买价数据：用户自己手动查看 bot 报价后录入的 `{name, set, bot, price, noBid, date}`
记录。这个步骤把这份数据换算成"典型买卖价差有多大"的统计量，回答"就算判断方向对了，扣掉真实
价差之后还剩多少"这个问题——是对第2步"操作建议"的补充，不是新的选卡标准。

步骤：
1. `read_db`（`db_op: "list"`，collection `buy_observations`）读回全部记录（这个集合目前
   数据量应该不大，`list` 一次拿完就够，不需要分页），包成 `{observations: [...]}`，写到
   `data/buy_observations_import.json`。这个集合是空的很正常（用户刚开始积累这份数据），
   脚本会自己处理成"样本不足"，不会报错。
2. `python compute_spread_stats.py`——输出 `data/spread_stats.json`，结构：
   `{ generatedAt, totalObservations, matched, skippedNoAskData, skippedNoBid,
   byRarity: {Rare: {medianSpreadPct, sampleSize, noBidRate, noBidSampleSize,
   insufficientSample}, ...}, overall: {同样的字段} }`。`insufficientSample=true`（样本 <5）
   的桶 `medianSpreadPct` 会是 `null`，前端会如实显示"样本不足"，不会瞎编一个数字。
3. 不需要额外定性判断，直接把整份 JSON 用 `write_db`（`db_op: "set"`）写到
   `site_meta/spread_stats`（字段名保持跟脚本输出一致，不用转驼峰）。

## 换手率代理指标（自动随每日流水线产生，本节不需要额外步骤）

`pipeline.py` 已经在 `data/latest_watchlist.json` 的每张卡（及每个版本）里带上
`price_change_rate_30d`（近30天卖价变化天数占比，0~1），随第4步一起写进 `watchlist` 文档，
不需要单独抓取或额外调用脚本。写研判 note 时参考前面"换手率低的卡，波动样本代表性要打折扣"
那条规则即可。

## 研判命中率回评（到期自动核对，本技能自己负责，每天都跑）

`watchlist`/`research_runs/latest` 都是"只保留最新一份"的文档，历史研判被覆盖或删除后就永久
丢失了，没办法回答"上周说会企稳的那些卡，后来真的没有继续跌吗"。第4步的 `verdict_log` 存档
（daily 每张入选卡 `horizonDays=30`）和每周研究 routine 自己的存档（`horizonDays` 按候选卡
所在窗口 7/14/30），到期后由本节自动回头核对，不是新的选卡标准，只是让"研判说的话有没有兑现"
这件事变得可追溯。

步骤：
1. `read_db`（`db_op: "query"`，collection `verdict_log`，`where: [["scored","==",false]]`，
   `limit: 500` 应该够用——每天新增的记录有限，到期前会一直保持 `scored=false`）读回全部未
   评分的记录，**保留每条记录的文档 id**（查询结果每条都带着自己的 doc id，回评时要用它做
   update 的目标），包成 `{records: [{id, date, name, mtgoId, source, verdict, direction,
   priceAtCall, horizonDays, rarity, scored}, ...]}`，写到 `data/verdict_log_import.json`。
   这个集合刚上线时会是空的，很正常，脚本会自己处理成"没有待评估记录"，不报错。
2. `python score_verdicts.py`——只会处理"已经到期"（`date + horizonDays <= 今天`）的记录，
   没到期的会原样跳过，留着下次再检查。如果同一次 routine 已经跑过"买卖价差统计"那一节产出了
   `data/spread_stats.json`，脚本会顺带给部分记录算一个"扣掉真实价差后大概还剩多少"的参考值，
   没有就跳过这部分，不影响主流程。输出 `data/verdict_scoring_output.json`，结构见脚本
   docstring（`updates` 数组 + `trackRecord` 汇总）。
3. 用 `write_db`（`db_op: "batch"`）把 `updates` 数组里每一条按 `{op: "update", collection:
   "verdict_log", doc_id: <该条的 id>, data: <该条的 fields>}` 落库（这一步是给`verdict_log`
   里已存在的文档打补丁，不是新建文档）。
4. `trackRecord` 只是这次桥接进来的记录算出来的**增量**参考（不是全局最终版本，因为第1步只
   查了 `scored=false` 的记录，不包含更早已经评过分的）。所以这一步之后，再做一次
   `read_db`（`db_op: "list"`，collection `verdict_log`）读回**全部**记录（这时已经含刚更新
   完的），按 `verdict` 分组统计 `hit=true`/`hit=false`（`hit=null` 的跳过，不计入分母），
   算出 `{byVerdict: {stabilizing: {hits, total, hitRate}, ...}}`，用 `write_db`
   （`db_op: "set"`）整份覆盖写到 `site_meta/verdict_track_record`（带上 `updatedAt`）——
   这是仪表盘"数据研究"页"历史研判命中率"板块的数据源。
5. 这一步不需要每天都产出内容（多数天可能没有新到期的记录），但每天都要执行，避免评分堆积。

## 运行步骤

### 0. 幂等检查——今天是不是已经跑成功过了（每次启动第一件事）

这个 routine 的云端沙盒偶尔会遇到网络出口策略把 GoatBots/Scryfall/mtgo.com 等域名全部拒绝
（CONNECT 403，policy denial）的情况——概率不算低（实测出现过连续两次触发都被拦的情况），
跟这次分配到的沙盒有关，遇到时应该如实报告、不要硬凑数据（历史上已经这样处理过，是对的）。
这个限制本身不是这个项目能修复的（是执行环境的基础设施策略，不是代码问题），能做的只是靠
**多试几次、换新沙盒**把"当天全部失败"的概率压低——cron 现在配置成**每天在同一小时内触发
四次**（目前是晚上8/9/10/11点），除第一次外，每一次都是"如果前面的尝试被网络策略挡住，
自动重试"：

1. `read_db`（`db_op: "get"`，collection `site_meta`，doc_id `daily_run_marker`）读一下今天
   的运行标记，结构 `{date, completedAt}`。
2. 如果 `date` 字段**等于今天**（routine 自己系统时钟的今天），说明今天已经有一次成功跑完
   全部步骤了——这次是重复触发，直接打印一句说明就结束，**不要**重新跑一遍第1-12步（没有
   意义，还会重复写入/重复推送通知）。
3. 如果 `date` 不是今天（不存在，或者是更早的日期——说明昨天/更早的某次也失败过，标记停留
   在更旧的日期），正常往下走完第1-12步。**只有全部步骤都跑完、没有在 `pipeline.py` 这类
   关键步骤卡死的情况下**，才在最后（原第12步之后）用 `write_db`（`db_op: "set"`）把
   `{date: 今天, completedAt: 当前时间戳}` 写回 `site_meta/daily_run_marker`——这样如果
   某次触发在 `pipeline.py` 就被网络策略挡死，标记不会被更新，一小时后下一次触发时
   步骤2的判断会正确识别"今天还没跑成功"，重新完整跑一遍（拿到的是另一个沙盒，网络策略
   可能不一样，这就是"自动重试"能生效的原因）。
4. **只要这次运行在 `pipeline.py`（或其他任何关键步骤）遇到网络出口策略拦截而提前退出，
   不管这是当天第几次触发，都必须立刻用 `PushNotification` 发一条通知**，如实说明
   "今天这次自动更新被网络策略挡住了，数据还是旧的"。**不要自己判断"这是不是最后一次触发
   所以先不打扰用户"**——之前出现过这个判断错误：一次运行错误地以为自己是"当天第一次"
   （实际上已经是当天最后一次重试），选择了保持沉默，结果当天全部触发都失败、又都没发通知，
   导致仪表盘数据整整停滞了一天多用户才发现。这个 routine 自己没有可靠的方式区分"这是第几次
   触发"，所以正确做法是**每次被拦截都发**，宁可同一天收到好几条重复通知，也不要因为误判
   "还有下一次机会"而漏发——数据能不能自动补上是重试机制的事，用户"知不知道今天没更新"
   不应该依赖那次判断对不对。

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
每个元素是一张卡，含 `versions` 数组。**同时也要读 `dollarDrops`/`dollarGains` 这两个数组**
（结构跟 `signals`/`risers` 一样，都是卡对象）——`signals`/`risers` 是按 `chg_30d_pct`
（百分比涨跌幅）排序筛出来的，这个排序方式天然偏向低价卡（同样几美分的波动在低价卡上就是
几百%，高价卡上只是零点几%），会把真正有分量的高价卡波动挤出候选视野；`dollarDrops`/
`dollarGains` 是按 `chg_30d_abs`（30日绝对美元变动）单独排的并列榜单，不预先要求
`near_90d_low`/`big_drop_7d` 这类百分比门槛，能补上高价卡的真实波动（2026-09-13 与用户
讨论后加入，见 `pipeline.py` 里的注释）。选候选时两份榜单都要看，同一张卡如果两边都出现
（常见，因为大波动往往百分比和美元变动都大），按一份处理即可，不要重复研判；只出现在
`dollarDrops`/`dollarGains`（百分比不够显著、但美元变动可观的高价卡）的卡，判断标准跟
下面一致，只是不要因为它没触发 `near_90d_low`/`big_drop_7d` 就当成"不够格"直接跳过——
这正是这两个并列榜单存在的意义。对每一张候选卡（基于其 primary version 的指标），参考
`strategies/*.yaml` 里的策略描述（dip_stabilizing 企稳 / falling_knife_caution 仍在下跌 /
new_set_decay 新卡衰减 / established_staple_dip 老卡折价 / momentum_up 上涨动量），判断这张卡
属于哪一类，并结合你自己对 Modern/Legacy 赛制环境的了解，给出简短研判。如果同一张卡不同版本
走势差异很大（比如某个促销版本单独暴跌，其他版本稳定），在 note 里说明是哪个版本驱动的信号，
不要笼统地说"这张卡"。不确定的信息（比如某张卡是否真的还在被广泛使用）要明确标注是"数据面"
还是"情报面"（后者是你的定性判断，不是抓取来的事实）。

**在给任何一张卡下"企稳/折价/追涨"这类结论之前，先过一遍 `supply_vs_demand_framework.yaml`
这个背景框架**（不是一个独立分类，是写每条 note 前都要先想一遍的判断前提）：MTGO 单卡不是
股票，供给几乎无限且由 WotC 单方面控制、bot 是机械清算库存不是公开竞价、买家几乎都是要立刻
上场的玩家而不是长期持有的投机资金——所以"跌深了、跌势放缓了"默认**不等于**"该反弹了"，
这跟股票市场的默认假设正好相反。写 note 前先判断这次涨跌能不能追溯到具体的供给事件（新系列
发售/重印/某版本补货）或需求事件（赛制合法性变化、解禁、新套牌采用），供给驱动的"企稳"要如实
写成"新低位企稳，不代表看多"，只有找到需求端证据支撑时才能写"值得关注的低吸候选"这类措辞；
涨势同理，需求驱动的涨势才值得强调"可能持续"，单纯供给收紧的涨势要提醒"追高前确认有没有需求
端理由"。

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

**换手率低的卡，波动样本代表性要打折扣**：`data/latest_watchlist.json` 里每张卡（及每个版本）
带一个 `price_change_rate_30d`（0~1，近30天里 GoatBots 卖价与前一天不同的天数占比，越低说明
这张卡越久没被重新定价，即 bot 几乎没有人跟它交易）。这不是新抓的数据，是从已有的每日卖价序列
里顺手算出来的换手率代理指标。如果一张卡同时满足"换手率很低（比如 <20%）"和"`big_drop_7d`/
`big_gain_7d` 触发了"，这次波动很可能只是一两笔孤立挂单造成的，不代表真实供需变化，note 里要
点破"该版本近期几乎无人交易，本次涨跌样本代表性存疑"，倾向标 `caution`，不要当成真实动能处理；
反过来，换手率高（比如 >60%）又出现明显涨跌，才是更值得信的信号。

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
  priceChangeRate30d,   // 30日换手率代理指标（0~1，近30天价格变化的天数占比），取自 primary version
  formats: ["modern","legacy"],   // 直接抄 latest_watchlist.json 里这张卡的 formats 数组
  direction: "rise",   // 上涨候选必须带这个字段；下跌候选不用带（省略即默认下跌）
  verdict: "stabilizing" | "falling_knife" | "established" | "momentum" | "caution" | "new_set",
  note: "一两句话的研判，中文，供仪表盘详情页展示",
  imageUrl, imageAssetId,   // 卡图，见下面"卡图"小节；抓不到图就都不要写这两个字段
  versions: [   // 该卡的全部已知版本，仪表盘详情页用这个渲染版本切换器
    { mtgoId, set, foil, collectorNumber, goatbotsPrice, cardhoarderPrice, chg7d, chg30d, low90, ma7, ma30, priceChangeRate30d },
    ...
  ]
}
```
verdict 的取值必须是上面枚举里的英文 key（仪表盘 CSS/文案按这几个 key 渲染），不要自己发明新词。
`versions` 数组直接从 `data/latest_watchlist.json` 里对应卡片的 `versions` 字段取，字段名要转成
驼峰（`mtgo_id`→`mtgoId`、`goatbotsPrice`/`cardhoarderPrice`/`chg_7d_pct`→`chg7d`、
`collector_number`→`collectorNumber`、`price_change_rate_30d`→`priceChangeRate30d` 等）。
**`collectorNumber` 这个字段必须带上，不要漏**——
同一个 `set` 代码下经常混着好几种实际印刷（普通版/无边框版/复古边框版等，比如 Exploration 的
DMR 版就有两种，`set` 都是 "DMR"），光看 set+foil 分不出来，仪表盘就是靠这个字段区分的。

**卡图（每张入选卡都要配）**：写入前先 `read_db`（`db_op: "get"`）看这张卡在 `watchlist`
集合里已有的文档：
- 如果已有文档的 `primaryMtgoId` 跟这次一样、且带着 `imageAssetId`（assets 路径）或者
  `card_images` 集合里已经有这个 mtgoId 的文档（data URI 路径，见下面兜底方案），说明图已经
  有了，不用重新下载上传。
- 否则（新卡，或者 `primaryMtgoId` 变了——比如更便宜的新版本上线）：
  1. `python fetch_card_image.py <mtgoId1> [<mtgoId2> ...] <输出路径>`（脚本支持传多个候选
     mtgo_id，按参数顺序依次尝试，第一个能拿到图的就用）：把这张卡的 `versions` 数组按价格
     从低到高排序，取排好序的 mtgo_id 列表依次传进去，第一个参数是 `primaryMtgoId`，后面是
     次便宜、再次便宜……以此类推。这样最低价版本 Scryfall 没收录图时会自动退而求其次用下一
     便宜版本的图，而不是直接放弃这张卡的卡图。全部版本都试过还是没有图（脚本退出码 1，打印
     `NO_IMAGE`）才真正跳过卡图字段，不要中断整个流水线。输出路径统一用
     `data/images/<primaryMtgoId>.jpg`（不管最后用的是哪个版本的图，本地文件名和数据库
     doc_id 都按 `primaryMtgoId` 走，方便仪表盘按卡片的 primaryMtgoId 统一查找）。
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

**c) `verdict_log` 集合**（只追加，不覆盖，不删除——这是历史研判的存档，供"研判命中率回评"
那一节到期后回头核对用）：对**这次入选**的每张卡（下跌+上涨候选都要），用 `write_db`
（`db_op: "batch"`）各写一条，doc_id 用 `{asOf}-{卡名slug}-daily`（跟 `watchlist` 用同一个
slug 规则，`asOf` 就是这次数据的日期），内容：
```
{
  date: asOf, name, mtgoId: primaryMtgoId, source: "daily",
  verdict, direction: "rise" | null,   // 跟这张卡写进 watchlist 文档的字段保持一致
  priceAtCall: bestPrice, horizonDays: 30, rarity,
  scored: false, scoredAt: null, priceAtHorizon: null, actualChgPct: null, hit: null
}
```
这一步只是"存一份档"，不影响 `watchlist` 集合本身的写法，两者并行写、互不覆盖。已经存在的
`verdict_log` 文档（doc_id 相同，同一天对同一张卡重复跑）会被覆盖成最新一次的研判，这是预期
行为，不是 bug。

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
