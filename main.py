"""Sequoia-X V2 主程序入口。

两种运行模式：
  python main.py               # 日常模式：增量补数据 + 跑策略 + 飞书推送
  python main.py --force       # 兼容参数；同步失败会自动告警并使用本地数据继续
  python main.py --backfill    # 回填模式：按本地最后日期续传历史K线
  python main.py --backfill --full-history  # 强制从 START_DATE 全量补齐历史
"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
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
from sequoia_x.core.logger import configure_file_logging, get_logger
from sequoia_x.data.engine import DataEngine
from sequoia_x.notify.feishu import FeishuNotifier
from sequoia_x.monitor import IntradayMonitor
from sequoia_x.portfolio import PortfolioAdvisor, PortfolioManager
from sequoia_x.prediction import EnsemblePredictor, PredictionTracker
from sequoia_x.simulation import PaperTradingManager
from sequoia_x.backtest import (
    BacktestConfig,
    EventDrivenBacktester,
    WalkForwardValidator,
    write_backtest_report,
)
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
from sequoia_x.strategy.combiner import StrategyCombiner
from sequoia_x.strategy.selection_state import update_consecutive_counts


def _run_strategies(
    strategies: list[BaseStrategy],
    max_workers: int,
    logger,
) -> dict[str, list[str]]:
    """并发执行独立策略，并按声明顺序收集结果。"""
    worker_count = max(1, min(max_workers, len(strategies)))
    logger.info(f"使用 {worker_count} 个线程并发执行 {len(strategies)} 个策略")
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="strategy",
    ) as executor:
        futures = []
        for strategy in strategies:
            strategy_name = type(strategy).__name__
            logger.info(f"提交策略：{strategy_name}")
            futures.append((strategy, executor.submit(strategy.run)))

        selections: dict[str, list[str]] = {}
        for strategy, future in futures:
            strategy_name = type(strategy).__name__
            try:
                selected = future.result()
            except Exception:
                logger.exception(f"{strategy_name} 执行失败，继续处理其他策略")
                selected = []
            selections[strategy_name] = selected
            logger.info(f"{strategy_name} 选出 {len(selected)} 只股票")
    return selections


def _run_backtest(engine: DataEngine, args) -> None:
    """运行事件驱动回测或滚动样本外验证并输出归因报表。"""
    start_date, end_date = args.backtest
    if end_date.lower() == "latest":
        end_date = engine.get_latest_date() or ""
        if not end_date:
            raise RuntimeError("本地数据库没有可用于回测的最新交易日")
    symbols = None
    if args.backtest_symbols:
        symbols = [
            item.strip().zfill(6)
            for raw in args.backtest_symbols
            for item in raw.split(",")
            if item.strip()
        ]
    config = BacktestConfig(initial_capital=args.backtest_capital)
    backtester = EventDrivenBacktester(engine.db_path, config, symbols=symbols)
    if args.walk_forward:
        result = WalkForwardValidator(backtester).run(
            start_date,
            end_date,
            train_days=args.train_days,
            test_days=args.test_days,
        )
        prefix = "walk_forward"
    else:
        result = backtester.run(start_date, end_date)
        prefix = "backtest"
    paths = write_backtest_report(result, args.backtest_output, prefix=prefix)
    console = Console()
    table = Table(title="滚动样本外验证" if args.walk_forward else "事件驱动回测")
    table.add_column("指标")
    table.add_column("结果")
    for key in (
        "total_return",
        "annual_return",
        "max_drawdown",
        "sharpe",
        "sortino",
        "calmar",
        "benchmark_return",
        "win_rate",
        "closed_trades",
    ):
        value = result.metrics.get(key, 0.0)
        is_percentage = key.endswith("return") or key in {"max_drawdown", "win_rate"}
        display = f"{value:.2%}" if is_percentage else f"{value:.3f}"
        table.add_row(key, display)
    console.print(table)
    console.print(f"报告已写入：{paths['summary']}")


def _sync_latest(engine: DataEngine, force: bool, logger, notifier=None) -> bool:
    """同步最新行情；失败时告警并继续使用本地数据。"""
    _ = force  # 保留命令行兼容性；当前无论是否指定 --force 都会降级继续。
    logger.info("开始拉取最新快照...")
    try:
        count = engine.sync_today_bulk()
    except Exception as exc:
        logger.warning(
            "⚠️ baostock 登录或增量同步失败，将使用本地数据继续执行策略和推送："
            f"{exc}"
        )
        if notifier is not None:
            notifier.set_local_data_fallback(True)
            notifier.send_system_alert(
                title="baostock 登录或同步失败",
                message=str(exc),
                data_date=engine.get_latest_date(),
            )
        return False
    if notifier is not None:
        notifier.set_local_data_fallback(False)
    logger.info(f"快照同步完成，写入 {count} 只股票")
    return True


def _run_prediction(engine: DataEngine, settings, symbols: list[str], horizon: int) -> None:
    """训练集成模型并输出指定股票的涨跌概率。"""
    logger = get_logger(__name__)
    normalized = []
    for item in symbols:
        normalized.extend(part.strip() for part in item.split(",") if part.strip())
    logger.info(f"开始独立概率预测：标的 {len(normalized)} 只，周期 {horizon} 个交易日")

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
    logger.info(f"独立概率预测完成：生成 {len(results)} 条结果")


def _run_portfolio(
    engine: DataEngine,
    settings,
    watchlist: list[str] | None = None,
    positions: list[str] | None = None,
    sales: list[str] | None = None,
    remove_positions: list[str] | None = None,
) -> None:
    """更新本地组合、刷新收益并推送持仓与下一工作日建议。"""
    logger = get_logger(__name__)
    logger.info("开始刷新自选、持仓和下一工作日操作观察")
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
    notifier.send_portfolio_report(portfolio, advice_report)
    logger.info(f"组合报告完成：共 {len(portfolio)} 只股票")


def _run_intraday_monitor(engine: DataEngine, settings) -> None:
    """执行独立盘中监控，不同步、写入或修改正式日K。"""
    logger = get_logger(__name__)
    logger.info("开始盘中监控和模拟交易")
    simulator = PaperTradingManager(
        settings.paper_trading_db_path,
        initial_capital=settings.paper_initial_capital,
        thresholds=engine.thresholds,
    )
    paper_positions = simulator.position_states()
    monitor = IntradayMonitor(engine, settings, paper_positions=paper_positions)
    alerts = monitor.run()
    simulator.sync_universe(
        monitor.latest_universe_sources,
        monitor.latest_names,
        monitor.latest_prices,
    )
    simulator.snapshot_nav()
    trades = simulator.apply_alerts(alerts)
    accounts = simulator.accounts()
    paper_portfolio = simulator.portfolio()
    logger.info(
        f"盘中监控完成：监控 {len(monitor.latest_universe_sources)} 只，"
        f"获取报价 {len(monitor.latest_prices)} 只，预警 {len(alerts)} 条，"
        f"模拟成交 {len(trades)} 笔"
    )
    notifier = FeishuNotifier(settings)
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
        notifier.send_intraday_alerts(alerts)
    else:
        console.print("盘中监控完成：暂无新预警")
        notifier.send_intraday_status(
            monitored_count=len(monitor.latest_universe_sources),
            quoted_count=len(monitor.latest_prices),
            account_count=len(accounts),
            portfolio=paper_portfolio,
        )

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
        notifier.send_paper_trades(
            trades,
            portfolio=paper_portfolio,
            accounts=accounts,
        )
    console.print(
        f"模拟组合：候选 {len(accounts)} 只，持仓 {paper_portfolio.position_count} 只，"
        f"总资产 {paper_portfolio.total_assets:,.0f} 元，仓位 {paper_portfolio.exposure_rate:.1%}；"
        f"数据：{settings.paper_trading_db_path}"
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
    mode_group.add_argument(
        "--backtest",
        nargs=2,
        metavar=("START_DATE", "END_DATE"),
        help="事件驱动历史回测，结束日期支持 latest",
    )
    parser.add_argument(
        "--walk-forward",
        action="store_true",
        help="配合 --backtest 执行滚动样本外验证",
    )
    parser.add_argument("--train-days", type=int, default=252, help="滚动训练窗口交易日数")
    parser.add_argument("--test-days", type=int, default=63, help="滚动测试窗口交易日数")
    parser.add_argument(
        "--backtest-capital", type=float, default=1_000_000, help="回测初始本金"
    )
    parser.add_argument(
        "--backtest-output", default="data/backtest", help="回测与归因报表输出目录"
    )
    parser.add_argument(
        "--backtest-symbols",
        nargs="+",
        help="可选股票范围，支持空格或逗号分隔；默认使用本地全部非ST股票",
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
        help="兼容参数；当前增量同步失败会自动告警并使用本地数据继续",
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
        log_path = configure_file_logging(
            settings.log_dir,
            retention=settings.log_retention_files,
        )
        logger = get_logger(__name__)
        logger.info(f"Sequoia-X V2 启动，日志文件：{log_path}")

        mode = (
            "历史回填" if args.backfill else
            "独立预测" if args.predict else
            "财务同步" if args.sync_financials is not None else
            "滚动样本外验证" if args.backtest and args.walk_forward else
            "事件驱动回测" if args.backtest else
            "盘中监控" if args.intraday else
            "组合管理" if (args.portfolio or args.set_watchlist or args.set_position
                           or args.sell_position or args.remove_position) else
            "日常策略"
        )
        logger.info(f"运行模式：{mode}")

        # 3. 初始化数据引擎
        engine = DataEngine(settings)

        if args.walk_forward and not args.backtest:
            parser.error("--walk-forward 必须配合 --backtest 使用")

        if args.backtest:
            _run_backtest(engine, args)
            return

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

        notifier = FeishuNotifier(settings)

        # ── 先同步最新日K；失败则告警并自动降级到本地数据 ──
        _sync_latest(engine, force=args.force, logger=logger, notifier=notifier)

        # ── 最新日K入库后再生成持仓、低价多因子候选与操作观察 ──
        _run_portfolio(engine, settings)

        # 4. 策略列表（新增策略在此追加即可）
        comprehensive_strategy = ComprehensiveTrendStrategy(engine=engine, settings=settings)
        low_price_strategy = LowPriceMultiFactorStrategy(engine=engine, settings=settings)
        strategies: list[BaseStrategy] = [
            comprehensive_strategy,
            MaVolumeStrategy(engine=engine, settings=settings),
            TurtleTradeStrategy(engine=engine, settings=settings),
            HighTightFlagStrategy(engine=engine, settings=settings),
            LimitUpShakeoutStrategy(engine=engine, settings=settings),
            UptrendLimitDownStrategy(engine=engine, settings=settings),
            RpsBreakoutStrategy(engine=engine, settings=settings),
            PrivatePlacementStrategy(engine=engine, settings=settings),
            low_price_strategy,
        ]

        data_date = engine.get_latest_date()
        logger.info(f"当前策略使用的数据日期：{data_date or '未知'}")

        # 5. 原始策略并发计算；按声明顺序收集并推送结果
        strategy_selections = _run_strategies(
            strategies,
            max_workers=settings.strategy_max_workers,
            logger=logger,
        )
        for strategy in strategies:
            strategy_name = type(strategy).__name__
            selected = strategy_selections[strategy_name]
            if not strategy.standalone_push_enabled:
                logger.info(
                    f"{strategy_name} 独立推送已关闭，结果继续用于组合报告和组合决策"
                )
                continue
            stock_names = engine.get_stock_names(selected) if selected else {}
            notifier.send(
                symbols=selected,
                strategy_name=strategy_name,
                webhook_key=strategy.webhook_key,
                data_date=data_date,
                stock_names=stock_names,
            )
            if not selected:
                logger.info(f"{strategy_name} 无选股结果，已推送空结果状态")

        combined = StrategyCombiner.combine(
            strategy_selections,
            comprehensive_strategy.last_assessments,
            factor_candidates=low_price_strategy.last_combination_ranking.to_dict("records"),
            thresholds=engine.thresholds,
        )
        logger.info(
            f"组合决策：总候选 {len(combined.all_candidates)} 只，"
            f"多策略共振 {len(combined.multi_strategy)} 只，"
            f"跨策略组共振 {len(combined.multi_family)} 只，"
            f"趋势确认 {len(combined.trend_confirmed)} 只，"
            f"重点候选 {len(combined.focus)} 只"
        )
        if combined.focus:
            consecutive_counts = update_consecutive_counts(
                settings.combined_streak_path,
                combined.focus,
                data_date or date.today().isoformat(),
            )
            focus_symbols = set(combined.focus)
            focus_details = [
                item for item in combined.details if item.symbol in focus_symbols
            ]
            notifier.send_combined_selection(
                details=focus_details,
                data_date=data_date,
                stock_names=engine.get_stock_names(list(combined.focus)),
                consecutive_counts=consecutive_counts,
                max_chars=engine.thresholds.integer("feishu", "combined_detail_max_chars"),
            )
            logger.info(
                "组合重点候选连续次数："
                + "，".join(f"{symbol}={count}" for symbol, count in consecutive_counts.items())
            )
        else:
            update_consecutive_counts(
                settings.combined_streak_path,
                (),
                data_date or date.today().isoformat(),
            )

        # 重点组合、实际持仓和自选股进入固定10交易日多周期预测跟踪。
        prediction_symbols = set(combined.focus)
        portfolio_path = Path(settings.portfolio_csv_path)
        if portfolio_path.exists():
            portfolio_frame = pd.read_csv(portfolio_path, dtype={"symbol": str})
            if not portfolio_frame.empty:
                portfolio_frame["symbol"] = portfolio_frame["symbol"].astype(str).str.zfill(6)
                shares_source = (
                    portfolio_frame["shares"]
                    if "shares" in portfolio_frame
                    else pd.Series(0, index=portfolio_frame.index)
                )
                shares = pd.to_numeric(shares_source, errors="coerce").fillna(0)
                watchlist_source = (
                    portfolio_frame["is_watchlist"]
                    if "is_watchlist" in portfolio_frame
                    else pd.Series(False, index=portfolio_frame.index)
                )
                watchlist = watchlist_source.astype(str).str.lower().isin({"true", "1"})
                prediction_symbols.update(portfolio_frame.loc[shares > 0, "symbol"])
                prediction_symbols.update(portfolio_frame.loc[watchlist, "symbol"])
        if prediction_symbols and data_date:
            try:
                logger.info(f"启动多周期预测跟踪：共 {len(prediction_symbols)} 只股票")
                prediction_names = engine.get_stock_names(sorted(prediction_symbols))
                tracking_report = PredictionTracker(
                    engine,
                    settings.prediction_tracking_db_path,
                ).run(sorted(prediction_symbols), prediction_names, data_date)
                notifier.send_prediction_tracking(
                    tracking_report,
                    max_chars=engine.thresholds.integer("feishu", "combined_detail_max_chars"),
                )
                logger.info(
                    f"多周期预测跟踪完成：新周期 {len(tracking_report.started)}，"
                    f"完成周期 {len(tracking_report.completed)}，"
                    f"评估 {len(tracking_report.evaluations)}，"
                    f"预测 {len(tracking_report.predictions)}，"
                    f"异常 {len(tracking_report.errors)}"
                )
            except Exception as exc:
                logger.exception(f"自动多周期预测跟踪失败：{exc}")
                notifier.send_system_alert(
                    title="自动多周期预测跟踪失败",
                    message=str(exc),
                    data_date=data_date,
                )

        selection_path = Path(settings.strategy_selection_path)
        selection_path.parent.mkdir(parents=True, exist_ok=True)
        selection_path.write_text(
            json.dumps(
                {
                    "data_date": data_date,
                    "strategies": strategy_selections,
                    "combined": combined.to_dict(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        logger.info(f"策略结果已写入：{selection_path}")

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
