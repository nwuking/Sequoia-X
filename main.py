"""Sequoia-X V2 主程序入口。

两种运行模式：
  python main.py               # 日常模式：增量补数据 + 跑策略 + 飞书推送
  python main.py --force       # 增量同步失败时，使用本地陈旧数据继续推送
  python main.py --backfill    # 回填模式：按本地最后日期续传历史K线
  python main.py --backfill --full-history  # 强制从 START_DATE 全量补齐历史
"""

import argparse
import json
import sys
from dotenv import load_dotenv
load_dotenv()

from datetime import date
from pathlib import Path

import socket
socket.setdefaulttimeout(10.0)

import pandas as pd
from rich.console import Console
from rich.table import Table

from sequoia_x.core.config import get_settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine
from sequoia_x.notify.feishu import FeishuNotifier
from sequoia_x.monitor import IntradayMonitor
from sequoia_x.portfolio import PortfolioAdvisor, PortfolioManager
from sequoia_x.prediction import EnsemblePredictor
from sequoia_x.simulation import PaperTradingManager
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.high_tight_flag import HighTightFlagStrategy
from sequoia_x.strategy.limit_up_shakeout import LimitUpShakeoutStrategy
from sequoia_x.strategy.ma_volume import MaVolumeStrategy
from sequoia_x.strategy.low_price_multi_factor import LowPriceMultiFactorStrategy
from sequoia_x.strategy.turtle_trade import TurtleTradeStrategy
from sequoia_x.strategy.uptrend_limit_down import UptrendLimitDownStrategy
from sequoia_x.strategy.rps_breakout import RpsBreakoutStrategy
from sequoia_x.strategy.private_placement import PrivatePlacementStrategy
from sequoia_x.strategy.comprehensive_trend import ComprehensiveTrendStrategy


def _sync_latest(engine: DataEngine, force: bool, logger) -> None:
    """同步最新行情；force 模式下允许失败后继续使用本地数据。"""
    logger.info("开始拉取最新快照...")
    try:
        count = engine.sync_today_bulk()
    except Exception as exc:
        if not force:
            raise
        logger.warning(
            "⚠️ 增量同步失败，但已启用 --force，将使用本地陈旧数据继续执行策略和推送："
            f"{exc}"
        )
        return
    logger.info(f"快照同步完成，写入 {count} 只股票")


def _run_prediction(engine: DataEngine, settings, symbols: list[str], horizon: int) -> None:
    """训练集成模型并输出指定股票的涨跌概率。"""
    normalized = []
    for item in symbols:
        normalized.extend(part.strip() for part in item.split(",") if part.strip())

    predictor = EnsemblePredictor(engine)
    results, metrics = predictor.predict(normalized, horizon=horizon)

    console = Console()
    console.print(
        f"时间外验证：AUC={metrics['roc_auc']:.3f} | "
        f"准确率={metrics['accuracy']:.3f} | "
        f"平衡准确率={metrics['balanced_accuracy']:.3f} | "
        f"基准准确率={metrics['baseline_accuracy']:.3f} | "
        f"Brier={metrics['brier_score']:.3f}"
    )
    console.print(
        f"高置信度样本（≤40%或≥60%）：覆盖率={metrics['high_confidence_coverage']:.1%} | "
        f"准确率={metrics['high_confidence_accuracy']:.3f}"
    )
    console.print(
        f"验证起始日：{metrics['validation_start']} | "
        f"训练样本：{int(metrics['train_rows'])} | "
        f"校准样本：{int(metrics['calibration_rows'])} | "
        f"验证样本：{int(metrics['validation_rows'])}"
    )

    table = Table(title=f"未来 {horizon} 个交易日涨跌概率预测")
    table.add_column("代码")
    table.add_column("数据日期")
    table.add_column("方向")
    table.add_column("上涨概率", justify="right")
    table.add_column("同概率组历史均值", justify="right")
    for result in results:
        table.add_row(
            result.symbol,
            result.data_date,
            result.direction,
            f"{result.up_probability:.1%}",
            f"{result.expected_return:.2%}",
        )
    console.print(table)
    console.print("提示：这是统计概率而非收益保证；样本外指标低于基准时不应据此交易。")

    notifier = FeishuNotifier(settings)
    stock_names = engine.get_stock_names([result.symbol for result in results])
    notifier.send_prediction(results, metrics, stock_names=stock_names)


def _run_portfolio(
    engine: DataEngine,
    settings,
    watchlist: list[str] | None = None,
    positions: list[str] | None = None,
    sales: list[str] | None = None,
    remove_positions: list[str] | None = None,
) -> None:
    """更新本地组合、刷新收益并推送持仓与下一工作日建议。"""
    manager = PortfolioManager(engine, settings.portfolio_csv_path)
    if watchlist:
        manager.set_watchlist(watchlist)
    if positions:
        manager.upsert_positions([manager.parse_position(value) for value in positions])
    if sales:
        manager.sell_positions([manager.parse_sale(value) for value in sales])
    if remove_positions:
        manager.remove_positions(remove_positions)

    portfolio, _ = manager.refresh()
    if portfolio.empty:
        get_logger(__name__).warning("组合为空，请先通过 --set-watchlist 或 --set-position 添加")
        return

    console = Console()
    table = Table(title="自选与持仓")
    table.add_column("名称")
    table.add_column("代码")
    for column in ("股数", "最新收盘", "整体收益率", "整体盈亏"):
        table.add_column(column, justify="right")
    for _, row in portfolio.iterrows():
        shares_value = row["shares"]
        shares = int(float(shares_value)) if str(shares_value) not in ("nan", "<NA>") else 0
        close_value = pd.to_numeric(pd.Series([row["latest_close"]]), errors="coerce").iloc[0]
        has_quote = pd.notna(close_value)
        table.add_row(
            str(row["name"]),
            row["symbol"],
            str(shares),
            f"{float(close_value):.3f}" if has_quote else "-",
            f"{float(row['total_return_rate']):+.2%}",
            f"{float(row['total_pnl']):+,.2f}",
        )
    console.print(table)
    console.print(f"CSV：{settings.portfolio_csv_path}")

    factor_strategy = LowPriceMultiFactorStrategy(engine=engine, settings=settings)
    advice_report = PortfolioAdvisor(engine, strategy=factor_strategy).advise(portfolio)
    notifier = FeishuNotifier(settings)
    notifier.send_portfolio(portfolio)
    notifier.send_portfolio_advice(advice_report)


def _run_intraday_monitor(engine: DataEngine, settings) -> None:
    """执行独立盘中监控，不同步、写入或修改正式日K。"""
    monitor = IntradayMonitor(engine, settings)
    alerts = monitor.run()
    simulator = PaperTradingManager(
        settings.paper_trading_db_path,
        initial_capital=settings.paper_initial_capital,
    )
    simulator.sync_universe(
        monitor.latest_universe_sources,
        monitor.latest_names,
        monitor.latest_prices,
    )
    trades = simulator.apply_alerts(alerts)
    console = Console()
    if alerts:
        table = Table(title="盘中实时预警")
        for column in ("级别", "股票", "类型", "实时价", "说明"):
            table.add_column(column)
        for item in alerts:
            table.add_row(
                item.level, f"{item.name} {item.symbol}", item.alert_type,
                f"{item.price:.3f}", item.message,
            )
        console.print(table)
        FeishuNotifier(settings).send_intraday_alerts(alerts)
    else:
        console.print("盘中监控完成：暂无新预警")

    if trades:
        trade_table = Table(title="本次模拟交易")
        for column in ("股票", "操作", "股数", "价格", "金额", "原因"):
            trade_table.add_column(column)
        for trade in trades:
            trade_table.add_row(
                f"{trade.name} {trade.symbol}", trade.action, str(trade.shares),
                f"{trade.price:.3f}", f"{trade.amount:,.2f}", trade.reason,
            )
        console.print(trade_table)
    console.print(
        f"模拟账户：{len(simulator.accounts())} 只股票，每只初始本金 "
        f"{settings.paper_initial_capital:,.0f} 元；数据：{settings.paper_trading_db_path}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Sequoia-X V2 选股系统")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--backfill",
        action="store_true",
        help="回填模式：通过 baostock 拉取全市场历史 K 线（约12分钟）",
    )
    mode_group.add_argument(
        "--predict",
        nargs="+",
        metavar="SYMBOL",
        help="预测指定股票，可用空格或逗号分隔，例如 --predict 600519 000001",
    )
    mode_group.add_argument(
        "--portfolio",
        action="store_true",
        help="刷新本地自选与持仓，计算收益并推送飞书操作建议",
    )
    mode_group.add_argument(
        "--intraday",
        action="store_true",
        help="盘中监控：读取前一日综合趋势快照，检查持仓、自选和高分候选",
    )
    mode_group.add_argument(
        "--sync-financials",
        nargs="?",
        const="latest",
        metavar="REPORT_DATE",
        help="同步某一期财务因子，例如 --sync-financials 20260331；省略日期则按当前时间推断最近完整报告期",
    )
    parser.add_argument(
        "--sell-position",
        action="append",
        metavar="代码或中文名:股数:卖出价格",
        help="按股票代码或中文名记录卖出交易并更新历史收益，可重复指定",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="增量同步失败时，使用本地陈旧数据继续执行策略并推送",
    )
    parser.add_argument(
        "--horizon",
        type=int,
        default=5,
        help="预测未来交易日数量，范围 1-60，默认 5",
    )
    parser.add_argument(
        "--set-watchlist",
        nargs="+",
        metavar="STOCK",
        help="覆盖自选列表，支持股票代码或中文名",
    )
    parser.add_argument(
        "--set-position",
        action="append",
        metavar="股票:股数:成本[:买入均价]",
        help="新增或更新持仓，可重复指定",
    )
    parser.add_argument(
        "--remove-position",
        action="append",
        metavar="STOCK",
        help="清空指定股票持仓但保留在自选中，可重复指定",
    )
    parser.add_argument(
        "--full-history",
        action="store_true",
        help="配合 --backfill 使用：忽略本地最后日期，强制从 START_DATE 全量补齐历史",
    )
    args = parser.parse_args()

    try:
        # 1. 初始化配置
        settings = get_settings()

        # 2. 初始化日志
        logger = get_logger(__name__)
        logger.info("Sequoia-X V2 启动")

        # 3. 初始化数据引擎
        engine = DataEngine(settings)

        if args.backfill:
            # ── 回填模式：单线程保守拉历史 K 线，自动多轮重跑 ──
            logger.info("进入回填模式...")
            all_symbols = engine.get_all_symbols()
            engine.backfill(all_symbols, full_history=args.full_history)
            logger.info("Sequoia-X V2 回填模式运行完成")
            return

        if args.predict:
            _run_prediction(engine, settings, symbols=args.predict, horizon=args.horizon)
            _run_portfolio(engine, settings)
            return

        if args.sync_financials is not None:
            report_date = None if args.sync_financials == "latest" else args.sync_financials
            count = engine.sync_financial_factors(report_date=report_date)
            logger.info(f"财务因子同步完成：{count} 条")
            return

        if args.intraday:
            _run_intraday_monitor(engine, settings)
            return

        if (args.portfolio or args.set_watchlist or args.set_position or args.sell_position
                or args.remove_position):
            _run_portfolio(
                engine,
                settings,
                watchlist=args.set_watchlist,
                positions=args.set_position,
                sales=args.sell_position,
                remove_positions=args.remove_position,
            )
            return

        # ── 日常模式：持仓使用独立不复权报价，先更新以免行情同步失败时漏推 ──
        _run_portfolio(engine, settings)

        # ── 单次 API 补今天 + 策略 + 推送 ──
        _sync_latest(engine, force=args.force, logger=logger)

        # 4. 策略列表（新增策略在此追加即可）
        strategies: list[BaseStrategy] = [
            ComprehensiveTrendStrategy(engine=engine, settings=settings),
            MaVolumeStrategy(engine=engine, settings=settings),
            TurtleTradeStrategy(engine=engine, settings=settings),
            HighTightFlagStrategy(engine=engine, settings=settings),
            LimitUpShakeoutStrategy(engine=engine, settings=settings),
            UptrendLimitDownStrategy(engine=engine, settings=settings),
            RpsBreakoutStrategy(engine=engine, settings=settings),
            PrivatePlacementStrategy(engine=engine, settings=settings),
            LowPriceMultiFactorStrategy(engine=engine, settings=settings),
        ]

        notifier = FeishuNotifier(settings)
        data_date = engine.get_latest_date()
        logger.info(f"当前策略使用的数据日期：{data_date or '未知'}")

        # 5. 遍历策略，有结果则推送至对应机器人
        strategy_selections: dict[str, list[str]] = {}
        for strategy in strategies:
            strategy_name = type(strategy).__name__
            logger.info(f"执行策略：{strategy_name}")

            selected: list[str] = strategy.run()
            strategy_selections[strategy_name] = selected
            logger.info(f"{strategy_name} 选出 {len(selected)} 只股票")

            if selected:
                stock_names = engine.get_stock_names(selected)
                notifier.send(
                    symbols=selected,
                    strategy_name=strategy_name,
                    webhook_key=strategy.webhook_key,
                    data_date=data_date,
                    stock_names=stock_names,
                )
            else:
                logger.info(f"{strategy_name} 无选股结果，跳过推送")

        selection_path = Path(settings.strategy_selection_path)
        selection_path.parent.mkdir(parents=True, exist_ok=True)
        selection_path.write_text(
            json.dumps(
                {"data_date": data_date, "strategies": strategy_selections},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    except Exception:
        try:
            _logger = get_logger(__name__)
            _logger.exception("主流程发生未捕获异常，程序终止")
        except Exception:
            import traceback
            traceback.print_exc()
        sys.exit(1)

    logger.info("Sequoia-X V2 运行完成")


if __name__ == "__main__":
    main()
