# MOTrade

MTGO（Magic Online）单卡交易监控/信号系统。参考 [ZhuLinsen/daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis)
的架构思路（数据抓取 → 自然语言策略 → LLM 综合研判 → 推送）移植到 MTGO 单卡交易场景。

## 仪表盘

**https://claude.ai/code/artifact/1e6623c1-9314-4778-bb5a-83f689947e9e**

私有 Artifact 页面：观察卡池信号、你的持仓（含未实现盈亏）、买价手动观察记录，都在这一个页面里。

## 核心原则（务必遵守，见 `docs/PRINCIPLES.md`）

1. 只用官方/合规开放的数据源（GoatBots 卖价批量下载 + Scryfall 官方 API），不做反爬绕过、不做
   MTGO 客户端自动化
2. 密钥/Token 不硬编码、不入库
3. 用户个人信息（账号、持仓、交易记录）不进代码库、不外传 —— 持仓/买价记录只存在仪表盘
   Artifact 自己的私有数据库里，本仓库里没有这些数据
4. 分析层只处理公开市场数据，不夹带账号身份信息
5. 会话 token 余量降到 10% 时暂停并告知用户

## 为什么买价（bid）是手动录入的

GoatBots 和 Cardhoarder 都不公开批量的机器人买价数据，且 GoatBots 网页上的买价数字是刻意用 SVG
渲染的（防程序抓取，人眼可正常看到）。系统的信号完全基于官方公开的卖价（ask）历史数据；买价需要
你自己查看后在仪表盘里手动记录——两种查看方式见 `docs/PRINCIPLES.md`。

## 目录结构

```
MOTrade/
  dashboard.html          仪表盘源文件（发布为上面那个 Artifact）
  data/                    本地数据（gitignored）：SQLite 数据库、原始下载文件、每日报告
  fetchers/
    goatbots_fetcher.py    GoatBots 官方每日卖价批量 JSON（card-definitions + price-history + 年度归档）
    scryfall_fetcher.py    Scryfall 官方 bulk data（oracle-cards），用于赛制合法性判断
  storage.py               SQLite 存储层（cards / daily_prices / legality_cache / meta）
  watchlist_builder.py     按"价格阈值 + Modern/Legacy 合法性"生成候选池
  indicators.py            7/30/90 日涨跌幅、90 日高低点等纯规则指标
  pipeline.py              编排以上步骤，输出 data/latest_watchlist.json
  strategies/*.yaml        自然语言策略（企稳 / 仍在下跌 / 新卡衰减 / 老卡折价）
  .claude/skills/motrade-analyze/SKILL.md   把"每日分析+同步仪表盘"包装成的技能
  daily_prompt.txt         每日分析的完整 prompt（云端 routine 和本地脚本共用同一份逻辑）
  run_daily.ps1            本地定时任务入口（可选，见下方"两种定时方式"）
  docs/PRINCIPLES.md        合规原则详细记录
```

## 每日自动化

**主路径：云端 routine**（`https://claude.ai/code/routines/trig_01FdtrFdtEk3NKdfYGVe1TSL`）
每天 12:00 UTC（本地 22:00，AEST/UTC+10；进入夏令时后本地时间会变成 23:00）自动跑一次：拉取
GitHub 上的 `zyz122302-lang/motrade` 仓库最新代码 → 跑 `pipeline.py` → 套用策略分类 → 用 Artifact
工具直接同步仪表盘 → 有高可信度信号时用 PushNotification 提醒。云端环境每次都是全新 checkout，
不依赖这台电脑开机。

**备用路径：本地 `run_daily.ps1`**（Windows 任务计划程序调用）复用本地已经积累的 SQLite 历史数据，
但用的是非交互 `claude -p` 模式，**没有 Artifact 工具权限**，只能生成本地报告
（`data/reports/YYYY-MM-DD.md`），没法自动同步仪表盘。适合当云端 routine 出问题时的排查/交叉验证，
不建议作为主力。

代码仓库（公开，用于云端 routine clone；只有代码，不含任何持仓/交易数据）：
**https://github.com/zyz122302-lang/motrade**

## 当前进度

- [x] 项目骨架
- [x] GoatBots 数据抓取（卖价快照 + 年度历史归档）
- [x] Scryfall 赛制合法性抓取
- [x] Watchlist 构建（Modern + Legacy，价格阈值过滤）
- [x] 历史价格入库 + 7/30/90 日趋势指标
- [x] 自然语言策略（`strategies/*.yaml`）+ `SKILL.md`
- [x] 仪表盘 Artifact（观察卡池 / 持仓 / 买价记录，含表单录入）
- [x] 云端 routine 定时任务，验证 Artifact 工具在云端会话可用
- [ ] 更长期跑几天，观察信号质量、调优策略描述
- [ ] 视需要再接入手机推送（Remote Control 配对之前没跑通，当前用桌面/仪表盘代替）
