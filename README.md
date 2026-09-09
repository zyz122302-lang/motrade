# MOTrade

MTGO（Magic Online）单卡交易监控/信号系统。参考 [ZhuLinsen/daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis)
的架构思路（数据抓取 → 策略 → 分析 → 推送）移植到 MTGO 单卡交易场景。

## 核心原则（务必遵守，见 `docs/PRINCIPLES.md`）

1. 只用官方/合规开放的数据源，不做反爬绕过、不做 MTGO 客户端自动化
2. 密钥/Token 不硬编码、不入库
3. 用户个人信息（账号、持仓、交易记录）不进代码库、不外传
4. 分析层只处理公开市场数据，不夹带账号身份信息
5. 会话 token 余量降到 10% 时暂停并告知用户

## 为什么买价（bid）是手动录入的

GoatBots 和 Cardhoarder 都不公开批量的机器人买价（收购价）数据 —— 买价只有在 MTGO 客户端里
实际打开一次交易时才会被机器人报出来。系统的"低吸"信号完全基于官方公开的卖价（ask）历史数据，
"高抛"这一步需要你自己在客户端里查看实际买价后，用 `portfolio.py` 手动记录下来。
详见对话历史 / `docs/PRINCIPLES.md`。

## 目录结构

```
MOTrade/
  data/                  本地数据（gitignored）：SQLite 数据库、原始下载文件
  fetchers/
    goatbots_fetcher.py  抓取 GoatBots 官方每日卖价批量 JSON（card-definitions + price-history）
    scryfall_fetcher.py  抓取 Scryfall 官方 bulk data（oracle-cards），用于赛制合法性判断
  storage.py              SQLite 存储层（cards / daily_prices / watchlist / portfolio / buy_observations）
  watchlist_builder.py    按"价格阈值 + Modern/Legacy 合法性"生成常用单卡观察清单
  config.py               可调参数（价格阈值、赛制范围等）
  docs/
    PRINCIPLES.md         合规原则详细记录
```

## 当前进度

- [x] 项目骨架
- [x] GoatBots 数据抓取
- [x] Scryfall 赛制合法性抓取
- [x] Watchlist 构建（Modern + Legacy，价格阈值过滤）
- [ ] 历史价格入库 + 指标/信号引擎
- [ ] 持仓与买价手动记录工具
- [ ] `/schedule` 定时任务 + PushNotification 接入
