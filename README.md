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
.venv/bin/python main.py                # 日常模式：4进程增量补数据 + 跑策略 + 飞书推送
.venv/bin/python main.py --force        # 同步失败时使用本地陈旧数据继续选股并推送
.venv/bin/python main.py --backfill     # 回填模式：全市场历史K线一次性灌入（约12分钟）
.venv/bin/python main.py --predict 600519 000001 --horizon 5  # 指定股票预测未来5个交易日
.venv/bin/python main.py --portfolio    # 刷新持仓收益并推送下一工作日操作观察
```

默认情况下，增量同步失败会终止程序，避免使用陈旧或不完整的数据推送。
只有明确指定 `--force` 时，程序才会在同步失败后继续使用本地已有数据。

预测模式完全使用本地历史行情，不执行增量同步，并将结果推送至飞书。模型采用线性逻辑回归与
非线性梯度提升等权集成，并显示严格按时间切分的样本外 AUC、准确率、基准准确率和
Brier 分数。预测结果是统计概率，不构成收益保证或投资建议。

可选配置 `STRATEGY_WEBHOOK_PREDICTION` 使用独立的预测机器人；未配置时回退到默认
`FEISHU_WEBHOOK_URL`。

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
```

数据保存在 `data/portfolio.csv`。日常模式和预测模式每次运行都会用本地最新收盘价刷新
收益率、市值和浮动盈亏，并向 `STRATEGY_WEBHOOK_PORTFOLIO` 对应机器人推送持仓快照与
下一工作日规则化操作观察；未配置专属机器人时使用默认 Webhook。

---

## 内置策略 | Strategies

| 策略 | 说明 |
|---|---|
| **TurtleTrade** | 海龟突破：20日新高 + 成交额过亿 + 阳线防诱多，按涨幅排序 |
| **MaVolume** | 均线+放量突破 |
| **HighTightFlag** | 高而窄的旗形整理突破 |
| **LimitUpShakeout** | 涨停洗盘回踩确认 |
| **UptrendLimitDown** | 上升趋势中的跌停反包 |
| **RpsBreakout** | 欧奈尔 RPS 相对强度突破 |

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
