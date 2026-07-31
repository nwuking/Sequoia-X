# Sequoia-X: 王者回归 | The King Returns

> A 股量化选股系统 V2 | A-Share Quantitative Stock Selection System V2

---

## 简介 | Introduction

Sequoia-X V2 是面向 A 股市场的量化选股系统，基于现代 Python 工程化标准从零重构。
系统以 OOP 架构、向量化计算和增量数据更新为核心设计原则，每日收盘后自动选股并推送至飞书群。

数据层使用 [baostock](http://baostock.com)（免费、无需注册、无限流）拉取历史及增量日 K 数据（后复权），
存储于本地 SQLite，彻底规避东方财富反爬问题。

---

## 运行模式

```bash
.venv/bin/python main.py                # 日常模式：单连接增量补数据 + 跑策略 + 飞书推送
.venv/bin/python main.py --force        # 兼容参数；当前同步失败会自动降级继续
.venv/bin/python main.py --backfill     # 回填模式：按本地最后日期续传历史K线
.venv/bin/python main.py --backfill --full-history  # 强制从 START_DATE 全量补齐历史
.venv/bin/python main.py --sync-financials 20260331  # 同步某一期全市场财务因子
.venv/bin/python main.py --predict 600519 000001 --horizon 5  # 指定股票预测未来5个交易日
.venv/bin/python main.py --portfolio    # 刷新持仓收益并推送下一工作日操作观察
.venv/bin/python main.py --intraday     # 盘中监控持仓、自选和前一日综合趋势候选
```

baostock 登录或增量同步失败时，程序会通过 `system_operations` Webhook 推送红色系统告警，标明异常原因和
本地最新数据日期，然后自动使用本地数据继续执行持仓、全部策略和组合决策。未配置
`STRATEGY_WEBHOOK_SYSTEM_OPERATIONS` 时使用默认 Webhook；`--force` 参数仅为兼容旧调度保留。

为避免触发 `baostock` 在 `2026-07-29` 这类交易日的访问限制，系统默认启用本地软限流计数器：
`BAOSTOCK_DAILY_REQUEST_LIMIT=48000`。计数按自然日记录在本地数据库，达到阈值前会提前停止新的
`baostock` 请求，避免逼近官方 `5万次/日` 上限。

预测模式完全使用本地历史行情，不执行增量同步，并将结果推送至飞书。模型采用线性逻辑回归与
非线性梯度提升等权集成，并显示严格按时间切分的样本外 AUC、准确率、基准准确率和
Brier 分数。预测结果是统计概率，不构成收益保证或投资建议。

财务因子同步使用 `AKShare` 对接东方财富的业绩报表和 A 股实时估值数据，建议按季度执行一次
`--sync-financials`。低价多因子策略在本地存在财务因子时会自动叠加 `ROE / 营收同比 / 净利润同比 / 毛利率 /
经营现金流质量 / PE / PB` 等分数；若尚未同步，则自动回退为纯行情多因子版本。

概率预测与综合趋势、组合重点候选统一发送到 `STRATEGY_WEBHOOK_CORE_DECISION`；未配置时回退到
默认 `FEISHU_WEBHOOK_URL`。

### 自选与持仓

```bash
# 覆盖自选列表
.venv/bin/python main.py --portfolio \
  --set-watchlist 000425 600172 000783 600000 002491

# 新增或更新持仓：股票:股数:成本:买入均价
.venv/bin/python main.py --portfolio \
  --set-position 000783:6000:9.659:9.522 \
  --set-position 000425:1500:8.754:8.754

# 清空某只股票持仓，仍保留在自选中
.venv/bin/python main.py --portfolio --remove-position 000783

# 记录卖出：支持股票代码或中文名；全部卖出后自动清仓并保留自选及历史收益
.venv/bin/python main.py --portfolio --sell-position 000783:6000:10.20
.venv/bin/python main.py --portfolio --sell-position 长江证券:6000:10.20
```

数据保存在 `data/portfolio.csv`。日常模式和预测模式每次运行都会用本地最新收盘价刷新
收益率、市值和浮动盈亏，并向 `STRATEGY_WEBHOOK_PORTFOLIO_MANAGEMENT` 对应机器人推送持仓快照与
下一工作日规则化操作观察；未配置专属机器人时使用默认 Webhook。

### 日线决策与盘中监控

`ComprehensiveTrendStrategy` 只在收盘完整日K上生成正式趋势、评分、买点和止损价，并保存到
`data/comprehensive_trend_latest.json`。`--intraday` 只读取该快照、组合CSV和实时行情，不执行行情
同步，也不会把未收盘数据写入正式日K表。盘中模块监控硬止损、持仓亏损、预计放量下跌、突破候选
和14:45后的尾盘买点确认；同一股票同一类型的预警每天只推送一次。如果本轮没有新预警，盘中
机器人仍会推送绿色“监控正常、暂无新信号”状态卡片，并展示监控股票、成功取得报价和模拟账户数量。

盘中监控同时启用本地模拟交易。自选、实际持仓和所有策略最新选股中的每只股票都会建立一个相互
独立的 10 万元模拟账户：首次突破、盘中走强或尾盘买点使用约 30% 本金建仓，后续买点使用约 20% 本金
增持，放量下跌减持约一半，硬止损或持仓亏损信号清仓。买入和普通减持遵循 A 股 100 股一手，
且同一股票同一类信号每天最多成交一次。账户净值和逐笔成交持久化在
`data/paper_trading.db`，不会修改 `data/portfolio.csv` 中的真实持仓记录。可通过环境变量
`PAPER_INITIAL_CAPITAL` 和 `PAPER_TRADING_DB_PATH` 调整初始本金及存储位置。
模拟建仓、增持、减持或清仓发生时，会通过 `intraday_trading` Webhook 推送成交记录；未配置专属
机器人时使用默认 Webhook。

组合决策重点候选会推送每只股票的完整明细，包括策略来源、策略组、信号得分、低价多因子排名
与贡献、综合趋势评分与信号、风险状态、趋势确认和最终组合评分。发送前按照 `feishu` 配置段的
`combined_detail_max_chars` 检查卡片序列化长度；能容纳时合并推送，超长时自动拆分为多条，必要时
每只股票单独一条。

重点组合、实际持仓和自选股会自动进入多周期预测跟踪，同一活动周期不会重复建档。首次生成
1、3、5、7、10个交易日预测；到达相应交易日后，以该次预测签发日收盘价和目标日收盘价判断方向
是否准确，并刷新尚未到期目标的剩余期限预测。第10个交易日完成后归档并清空活动状态，下一交易日
仍在股票池时可开始新周期。活动数据保存在 `data/prediction_tracking.db`。组合重点候选卡片同时展示
连续入选次数，同日重复运行不会重复累加。
每日各策略的最新选股池保存在 `data/strategy_selection_latest.json`，供后续盘中任务读取。
各策略先独立扫描，再由组合决策层按趋势动量、形态整理、反转机会、事件驱动和横截面因子分组
加权。同组内多个相似策略不会重复获得完整权重，跨策略组共振会得到额外加分，最后再用综合趋势
评分、明确买点和退出风险进行二次确认。文件中的 `combined` 保存全部候选、多策略共振、跨组共振、
趋势确认、重点候选，以及每只股票的来源、策略组和组合评分；盘中任务会自动纳入 `combined.focus`。
低价多因子同时生成一套组合层专用排名，弱化与综合趋势重复的动量和趋势因子，侧重财务质量、
估值、低波动和流动性。前10名按排名向组合层贡献 10、9、8、6 或 3 分，原策略仍只推送综合排名
前3名。

原始策略默认使用最多 4 个线程并发计算，可通过 `STRATEGY_MAX_WORKERS` 调整。所有 baostock
入口共用进程级会话锁，完整的登录、查询和退出过程始终保持单连接串行。

策略、组合决策、盘中监控、组合建议、模拟交易和预测相关业务阈值统一记录在
`config/thresholds.ini`，每项均带有中文注释。可通过 `THRESHOLDS_CONFIG_PATH` 指定其他配置文件；
修改阈值后需重新启动程序。日常模式会先同步最新日K，再计算低价多因子候选和持仓操作观察，
确保报告与当天策略使用相同的数据日期。

建议交易日收盘后先运行一次 `main.py` 生成新快照，再通过定时任务在 09:40、10:30、11:20、
13:30、14:30、14:50 分别执行 `main.py --intraday`。

项目提供 `schedule_strategy.sh` 管理上述调度，并在每个工作日19:15执行完整日K策略：

```bash
chmod +x schedule_strategy.sh
./schedule_strategy.sh install  # 安装或更新项目专属crontab
./schedule_strategy.sh list     # 查看任务
./schedule_strategy.sh remove   # 只移除本项目任务
```

日志分别写入 `logs/intraday.log` 和 `logs/daily.log`。脚本使用 `Asia/Shanghai` 时区，并保留
用户crontab中的其他任务。周一至周五规则不自动识别A股法定休市日。

---

## 内置策略 | Strategies

| 策略 | 说明 |
|---|---|
| **ComprehensiveTrend** | 综合趋势系统：识别主升浪、阴跌、吸筹/洗盘/撤离候选、下跌反弹，并按市场环境、趋势、量价、MACD/RSI/OBV、相对强度和风险项评分；仅输出已触发平台突破、缩量回踩或趋势恢复买点的标的 |
| **TurtleTrade** | 海龟突破：20日新高 + 成交额过亿 + 阳线防诱多，按涨幅排序 |
| **MaVolume** | 均线+放量突破 |
| **HighTightFlag** | 高而窄的旗形整理突破 |
| **LimitUpShakeout** | 涨停洗盘回踩确认 |
| **UptrendLimitDown** | 上升趋势中的跌停反包 |
| **RpsBreakout** | 欧奈尔 RPS 相对强度突破 |
| **LowPriceMultiFactor** | 30元以下低价股多因子轮动：动量 + 低波动 + 趋势 + 流动性，固定前3名 |

---

## 快速开始 | Quick Start

### 环境要求

- Python >= 3.10

### 1. 安装依赖

```bash
# 推荐使用 uv（快速包管理器）
uv sync

# 或者 pip
pip install .
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填写飞书 Webhook URL
```

### 3. 首次回填历史数据

```bash
python main.py --backfill
```

约 12 分钟完成 ~5200 只 A 股历史后复权日 K 数据回填。

### 4. 日常运行

```bash
python main.py
```

建议配合 crontab 每个交易日收盘后自动执行：

```cron
15 19 * * 1-5 cd /root/Sequoia-X && .venv/bin/python main.py >> log.txt 2>&1
```

---

## 目录结构 | Project Structure

```
Sequoia-X/
├── main.py                      # 入口：argparse 分发日常/回填模式
├── pyproject.toml               # 依赖声明 + ruff/pytest 配置
├── .env.example                 # 环境变量模板
├── data/                        # SQLite 数据库（运行时生成，不入 git）
├── sequoia_x/
│   ├── core/
│   │   ├── config.py            # Pydantic-settings 配置管理
│   │   └── logger.py            # rich 结构化日志
│   ├── data/
│   │   └── engine.py            # 数据引擎（baostock 回填 + 增量同步 + SQLite）
│   ├── strategy/
│   │   ├── base.py              # 策略抽象基类
│   │   ├── turtle_trade.py      # 海龟交易策略
│   │   ├── ma_volume.py         # 均线放量策略
│   │   ├── high_tight_flag.py   # 高窄旗形策略
│   │   ├── limit_up_shakeout.py # 涨停洗盘策略
│   │   ├── uptrend_limit_down.py # 上升跌停策略
│   │   └── rps_breakout.py      # RPS 突破策略
│   │   └── low_price_multi_factor.py # 低价多因子轮动策略
│   └── notify/
│       └── feishu.py            # 飞书 Webhook 推送
└── tests/                       # 属性测试（hypothesis）
```

---

## 数据说明

- **数据源**：[baostock](http://baostock.com)（免费、无需注册、无限流）
- **复权方式**：后复权（hfq）— 历史价格不变，适合增量存储，避免除权导致数据错乱
- **存储**：本地 SQLite（`data/sequoia_v2.db`），可直接拷贝到其他机器使用
- **日常增量**：8 进程并行通过 baostock 拉取，2~3 分钟完成全市场更新

---

## 许可证 | License

MIT
