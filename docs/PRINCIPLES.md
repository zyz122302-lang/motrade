# 合规与隐私原则

本项目搭建过程中约定的硬性规则，任何后续改动都必须遵守。

## 1. 数据合规

- 仅使用 GoatBots 官方主动开放的卖价批量下载
  (`https://www.goatbots.com/download/prices/card-definitions.zip`,
  `https://www.goatbots.com/download/prices/price-history.zip`)。
  该页面原文写明 "Here you can download our daily average sell prices for **your own project**"，
  每天 05:30 CET 更新一次，不需要高频抓取。
- 使用 Scryfall 官方 API / bulk data（`https://api.scryfall.com`），仅用于赛制合法性、卡牌基础信息查询。
- **不**抓取 GoatBots 单卡页面上的买价（该数据被刻意渲染成 SVG 字形以防止抓取，是官方明确的反爬信号）。
- **不**对 Cardhoarder 做任何形式的批量抓取（其买价同样只能通过官方 Collection Appraisal 工具交互式查询）。
- **不**对 MTGO 客户端做任何自动化（截图识别、内存读取、模拟交易），因为：
  1. 违反 Wizards of the Coast 的服务条款风险由用户账号承担；
  2. 这类工具的合法性完全依赖"多年默许"的灰色地带，不应在用户自己账号上新建风险。
- 买价数据的获取方式：用户手动查看后自行录入仪表盘，系统不做任何自动化读价。手动查看有两种方式：
  1. 在 MTGO 客户端里实际打开一次交易，看 bot 报价（最贴近真实成交）
  2. 直接在浏览器打开 `https://www.goatbots.com/card/<卡名>` 人眼查看——该页面买价数字是以 SVG
     路径渲染的（而非普通文本），人眼能正常看到，但程序抓取拿不到文本，这是 GoatBots 针对自动化
     抓取的反制措施。**人工打开浏览器查看不受此限制**，可以作为更方便的手动查价方式；但我们不会
     写代码去逆向这套 SVG 数字渲染来实现自动抓取——"人眼能看见"不等于"允许自动化抓取"，两者是
     不同的事。

## 2. 密钥与凭证

- 任何 API Key / Token（如未来接入的通知渠道）一律通过环境变量或本地 `.env` 文件管理。
- `.env`、`data/`、任何包含真实持仓/交易记录的文件必须在 `.gitignore` 中排除。
- 本项目当前的"LLM 分析层"直接复用当前 Claude Code 会话本身（通过 `/schedule` 定时唤醒），
  不接入任何第三方 LLM API，因此不存在额外密钥泄露面。

## 3. 个人信息

- 用户的 MTGO 账号名、真实持仓、交易记录、盈亏记录属于用户隐私数据，只保存在本地
  (`data/portfolio.db`)，不推送到任何远程仓库、不作为 prompt 内容发送给第三方服务。
- 若未来把本项目开源/分享，必须先剥离 `data/` 目录下的真实数据，只保留示例/空 schema。

## 4. 分析范围

- 自动化分析仅处理公开市场数据（卡名、价格、赛制合法性）。
- 不会把账号身份信息作为分析输入。

## 5. Token 预算

- 当前 Claude Code 会话剩余 token 降到总额（1500 万）的 10% 左右（约剩 150 万）时，
  必须先告知用户，暂停搭建工作，不擅自继续消耗预算。
