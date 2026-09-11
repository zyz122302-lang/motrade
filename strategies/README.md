# MOTrade 策略目录

参考 ZhuLinsen/daily_stock_analysis 的做法：策略用自然语言 YAML 描述，不写死成 if/else 代码，
由 Claude 在每日分析时读取这些描述，结合 `pipeline.py` 算出的裸指标做综合判断。

`indicators.py` 只产出客观数字（7日/30日/90日涨跌幅、90日高低点、历史天数），
**从数字到"值不值得关注"的判断交给这里的策略描述 + Claude 的综合推理**，而不是硬编码阈值。

## 字段参考（pipeline.py 输出里每张候选卡都有）

- `price`：今日卖价 (TIX)
- `chg_7d_pct` / `chg_30d_pct` / `chg_90d_pct`：涨跌幅
- `low_90d` / `high_90d`：90日区间
- `near_90d_low`：是否接近90日低点（<=低点*1.05）
- `big_drop_7d`：7日跌幅是否 <= -15%
- `days_of_history`：本地已积累的历史天数（新系列卡天数少，历史判断力弱）

## 策略文件

- `supply_vs_demand_framework.yaml` — **背景框架，写任何一条 note 之前都先读这个**：MTGO 单卡
  跟股票不一样，供给驱动的涨跌大多不可逆，只有需求端事件（赛制合法性变化、解禁、新套牌采用）
  才是真正可能持续的信号，跌深了/企稳了不代表会像股票一样均值回归上涨
- `dip_stabilizing.yaml` — 回调企稳，下跌已放缓，需要先判断是供给端稀释完了还是有需求端支撑
- `falling_knife_caution.yaml` — 仍在加速下跌，风险提示，暂不建议入场
- `new_set_decay.yaml` — 新系列发售后的正常供给衰减，需要观望而非当作信号
- `established_staple_dip.yaml` — 有较长历史的常年主力卡出现折价，历史参照本身也可能因供给
  增加被永久下移，需要结合当前使用率判断这是不是"新的正常水平"
- `momentum_up.yaml` — 上涨动量，需要区分是需求驱动（更可能持续）还是单纯供给收紧（持续性存疑）
