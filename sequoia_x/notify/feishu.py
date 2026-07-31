"""飞书通知模块：将选股结果通过 Webhook 推送至飞书群。"""

import json
from datetime import date
from types import SimpleNamespace

import pandas as pd
import requests

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)


class FeishuNotifier:
    """飞书 Webhook 推送器。

    根据策略的 webhook_key 路由到对应的飞书机器人。
    若 webhook_key 未在 Settings.strategy_webhooks 中配置，
    则 fallback 到 Settings.feishu_webhook_url。
    """

    def __init__(self, settings: Settings) -> None:
        """
        初始化 FeishuNotifier。

        Args:
            settings: Settings 实例，提供 Webhook URL 配置。
        """
        self.settings = settings

    @staticmethod
    def _to_xueqiu_code(code: str) -> str:
        """将纯数字代码转为雪球格式：6开头→SH，4/8开头→BJ，其余→SZ。"""
        if code.startswith("6"):
            return f"SH{code}"
        elif code.startswith(("4", "8")):
            return f"BJ{code}"
        return f"SZ{code}"

    def _build_card(
        self,
        symbols: list[str],
        strategy_name: str,
        data_date: str | None = None,
        stock_names: dict[str, str] | None = None,
    ) -> dict:
        today = date.today().strftime("%Y-%m-%d")
        data_date_text = data_date or "未知"
        names = stock_names or {}

        links: list[str] = []
        for code in symbols:
            xq_code = self._to_xueqiu_code(code)
            name = names.get(code, xq_code)
            links.append(f"[{name}](https://xueqiu.com/S/{xq_code})")

        symbol_text = " ".join(links) if links else "（无选股结果）"

        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"📈 Sequoia-X 选股播报 | {strategy_name}",
                    },
                    "template": "blue",
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": (
                                f"**推送日期：** {today}\n"
                                f"**数据日期：** {data_date_text}\n"
                                f"**策略：** {strategy_name}\n"
                                f"**选股数量：** {len(symbols)}"
                            ),
                        },
                    },
                    {"tag": "hr"},
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**选股列表：**\n{symbol_text}",
                        },
                    },
                ],
            },
        }

    def _build_prediction_card(
        self,
        results: list,
        metrics: dict[str, float],
        stock_names: dict[str, str] | None = None,
    ) -> dict:
        """构建指定股票涨跌概率预测卡片。"""
        names = stock_names or {}
        horizon = results[0].horizon if results else 0
        data_date = results[0].data_date if results else "未知"

        rows = []
        for result in results[:50]:
            xq_code = self._to_xueqiu_code(result.symbol)
            name = names.get(result.symbol, xq_code)
            rows.append(
                f"[{name} {result.symbol}](https://xueqiu.com/S/{xq_code})｜"
                f"**{result.direction}**｜上涨概率 {result.up_probability:.1%}｜"
                f"同概率组历史均值 {result.expected_return:.2%}"
            )
        if len(results) > 50:
            rows.append(f"其余 {len(results) - 50} 只股票因消息长度限制未展示")

        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"🔮 Sequoia-X 股票预测 | 未来 {horizon} 个交易日",
                    },
                    "template": "purple",
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": (
                                f"**数据日期：** {data_date}\n"
                                f"**预测数量：** {len(results)}\n"
                                f"**时间外 AUC：** {metrics['roc_auc']:.3f}\n"
                                f"**准确率：** {metrics['accuracy']:.3f} "
                                f"（基准 {metrics['baseline_accuracy']:.3f}）\n"
                                f"**高置信度准确率：** "
                                f"{metrics['high_confidence_accuracy']:.3f}\n"
                                f"**Brier：** {metrics['brier_score']:.3f}"
                            ),
                        },
                    },
                    {"tag": "hr"},
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": "\n".join(rows),
                        },
                    },
                    {
                        "tag": "note",
                        "elements": [
                            {
                                "tag": "plain_text",
                                "content": "统计概率不代表收益保证，不构成投资建议。",
                            }
                        ],
                    },
                ],
            },
        }

    def _build_intraday_alert_card(self, alerts: list) -> dict:
        """构建盘中风险与尾盘确认卡片。"""
        rows = []
        for item in alerts[:50]:
            xq_code = self._to_xueqiu_code(item.symbol)
            rows.append(
                f"**[{item.level}]** "
                f"[{item.name} {item.symbol}](https://xueqiu.com/S/{xq_code})｜"
                f"{item.alert_type}｜实时价 {item.price:.3f}\n{item.message}"
            )
        if len(alerts) > 50:
            rows.append(f"其余 {len(alerts) - 50} 条预警因消息长度限制未展示")
        quote_time = max((item.quote_time for item in alerts), default="未知")
        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": "⚠️ Sequoia-X 盘中实时监控"},
                    "template": "red",
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**行情时间：** {quote_time}\n**新预警：** {len(alerts)} 条",
                        },
                    },
                    {"tag": "hr"},
                    {"tag": "div", "text": {"tag": "lark_md", "content": "\n\n".join(rows)}},
                    {
                        "tag": "note",
                        "elements": [
                            {
                                "tag": "plain_text",
                                "content": "盘中信号仅用于风险预警和尾盘确认；正式趋势以收盘日K为准。",
                            }
                        ],
                    },
                ],
            },
        }

    def _build_portfolio_card(self, portfolio: pd.DataFrame) -> dict:
        """构建持仓收益与自选股行情卡片。"""
        def numeric_column(name: str) -> pd.Series:
            if name not in portfolio.columns:
                return pd.Series(0.0, index=portfolio.index)
            return pd.to_numeric(portfolio[name], errors="coerce").fillna(0)

        holdings = portfolio[pd.to_numeric(portfolio["shares"], errors="coerce").fillna(0) > 0]
        total_market_value = float(pd.to_numeric(holdings["market_value"], errors="coerce").fillna(0).sum())
        total_pnl = float(pd.to_numeric(holdings["unrealized_pnl"], errors="coerce").fillna(0).sum())
        realized_pnl = float(numeric_column("realized_pnl").sum())
        total_pnl += realized_pnl
        if "cost_price" in holdings.columns:
            current_cost = float(
                (pd.to_numeric(holdings["cost_price"], errors="coerce").fillna(0)
                 * pd.to_numeric(holdings["shares"], errors="coerce").fillna(0)).sum()
            )
        else:
            current_cost = total_market_value - float(
                pd.to_numeric(holdings["unrealized_pnl"], errors="coerce").fillna(0).sum()
            )
        sold_cost = float(numeric_column("sold_cost").sum())
        total_cost = current_cost + sold_cost
        total_return = total_pnl / total_cost if total_cost else 0.0
        data_dates = portfolio["data_date"].dropna().astype(str)
        data_date = data_dates.max() if not data_dates.empty else "未知"

        rows = []
        for _, row in portfolio.iterrows():
            shares = float(pd.to_numeric(pd.Series([row["shares"]]), errors="coerce").fillna(0).iloc[0])
            close = pd.to_numeric(pd.Series([row["latest_close"]]), errors="coerce").iloc[0]
            if pd.isna(close):
                continue
            xq_code = self._to_xueqiu_code(row["symbol"])
            link = f"[{row['name']} {row['symbol']}](https://xueqiu.com/S/{xq_code})"
            if shares > 0:
                return_rate = float(row.get("total_return_rate", row["return_rate"]))
                pnl = float(row.get("total_pnl", row["unrealized_pnl"]))
                rows.append(
                    f"{link}｜{int(shares)}股｜收盘 {float(close):.3f}｜"
                    f"整体收益 {return_rate:+.2%}｜整体盈亏 {pnl:+,.2f}元"
                )
            else:
                history_rate_value = pd.to_numeric(
                    pd.Series([row.get("historical_return_rate", 0)]), errors="coerce"
                ).fillna(0).iloc[0]
                history_pnl_value = pd.to_numeric(
                    pd.Series([row.get("realized_pnl", 0)]), errors="coerce"
                ).fillna(0).iloc[0]
                history_rate = float(history_rate_value)
                history_pnl = float(history_pnl_value)
                rows.append(
                    f"{link}｜自选｜收盘 {float(close):.3f}｜"
                    f"历史收益 {history_rate:+.2%}｜历史盈亏 {history_pnl:+,.2f}元"
                )

        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": "💼 Sequoia-X 自选与持仓"},
                    "template": "wathet",
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": (
                                f"**数据日期：** {data_date}\n"
                                f"**持仓市值：** {total_market_value:,.2f} 元\n"
                                f"**整体盈亏：** {total_pnl:+,.2f} 元\n"
                                f"**组合收益率：** {total_return:+.2%}"
                            ),
                        },
                    },
                    {"tag": "hr"},
                    {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(rows)}},
                ],
            },
        }

    def _build_portfolio_advice_card(self, report) -> dict:
        """构建下一工作日持仓与自选操作建议卡片。"""
        advice = report.advice
        candidates = report.candidates
        replacements = report.replacements
        next_workday = advice[0].next_workday if advice else "未知"
        rows = []
        for item in advice:
            xq_code = self._to_xueqiu_code(item.symbol)
            rows.append(
                f"[{item.name} {item.symbol}](https://xueqiu.com/S/{xq_code})｜"
                f"**{item.action}**｜风险 {item.risk}\n"
                f"参考价 {item.reference_price:.3f}｜{item.reason}"
            )

        replacement_rows = []
        for item in replacements:
            sell_code = self._to_xueqiu_code(item.sell_symbol)
            buy_code = self._to_xueqiu_code(item.buy_symbol)
            replacement_rows.append(
                f"卖出 [{item.sell_name} {item.sell_symbol}](https://xueqiu.com/S/{sell_code}) "
                f"→ 买入 **[{item.buy_name} {item.buy_symbol}](https://xueqiu.com/S/{buy_code})** "
                f"(候选第{item.buy_rank}，分数 {item.buy_score:.2f})\n"
                f"当前持仓收益 {item.current_return:+.2%}"
            )

        candidate_rows = []
        for item in candidates:
            xq_code = self._to_xueqiu_code(item.symbol)
            marker = "⭐" if item.is_focus else "•"
            candidate_rows.append(
                f"{marker} [{item.name} {item.symbol}](https://xueqiu.com/S/{xq_code})｜"
                f"第{item.rank}名｜分数 {item.score:.2f}｜收盘 {item.close:.2f}"
            )
        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"🧭 下一工作日操作观察 | {next_workday}",
                    },
                    "template": "orange",
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": "\n\n".join(rows) if rows else "暂无明确操作建议",
                        },
                    },
                    {"tag": "hr"},
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": (
                                "**重点替换建议（前3候选优先）：**\n"
                                + ("\n\n".join(replacement_rows) if replacement_rows else "暂无明确替换建议")
                            ),
                        },
                    },
                    {"tag": "hr"},
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": (
                                "**低价多因子前10候选（⭐为前3重点）：**\n"
                                + ("\n".join(candidate_rows) if candidate_rows else "暂无候选")
                            ),
                        },
                    },
                    {
                        "tag": "note",
                        "elements": [
                            {
                                "tag": "plain_text",
                                "content": "规则化风险提示，仅供研究参考；节假日顺延，不构成个性化投资承诺。",
                            }
                        ],
                    },
                ],
            },
        }

    def _build_portfolio_report_card(self, portfolio: pd.DataFrame, report) -> dict:
        """将持仓概览和下一工作日操作观察合并为一张卡片。"""
        if isinstance(report, list):
            report = SimpleNamespace(advice=report, candidates=[], replacements=[])
        if report is None:
            report = SimpleNamespace(advice=[], candidates=[], replacements=[])

        portfolio_card = self._build_portfolio_card(portfolio)
        advice_card = self._build_portfolio_advice_card(report)
        advice = report.advice
        next_workday = advice[0].next_workday if advice else "待确认"
        elements = list(portfolio_card["card"]["elements"])
        elements.extend(
            [
                {"tag": "hr"},
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**下一工作日操作观察：** {next_workday}",
                    },
                },
                *advice_card["card"]["elements"],
            ]
        )
        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": "💼 Sequoia-X 持仓与下一工作日操作观察",
                    },
                    "template": "wathet",
                },
                "elements": elements,
            },
        }

    def _post_payload(self, payload: dict, webhook_key: str, success_message: str) -> None:
        """发送飞书卡片并统一处理响应。"""
        url = self.settings.get_webhook_url(webhook_key)
        try:
            resp = requests.post(
                url,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            resp_json = resp.json()
            if resp.status_code != 200 or resp_json.get("code") != 0:
                logger.error(
                    f"飞书推送失败 [{webhook_key}] "
                    f"HTTP状态={resp.status_code} 飞书响应={resp.text}"
                )
            else:
                logger.info(success_message)
        except requests.RequestException as exc:
            logger.error(f"飞书推送请求异常 [{webhook_key}]：{exc}")

    def send(
        self,
        symbols: list[str],
        strategy_name: str,
        webhook_key: str = "default",
        data_date: str | None = None,
        stock_names: dict[str, str] | None = None,
    ) -> None:
        """
        将选股结果格式化为飞书卡片消息并 POST 至对应 Webhook。

        根据 webhook_key 从 Settings 中查找专属 URL；
        若未配置，则 fallback 到 feishu_webhook_url。

        Args:
            symbols: 选股结果代码列表。
            strategy_name: 策略名称，用于卡片标题。
            webhook_key: 策略标识，用于路由到对应飞书机器人。
            data_date: 本地行情库中的最新交易日期。
            stock_names: 从本地数据库读取的股票代码到中文名映射。

        Raises:
            不抛出异常，HTTP 失败时记录 ERROR 日志。
        """
        payload = self._build_card(
            symbols,
            strategy_name,
            data_date=data_date,
            stock_names=stock_names,
        )

        self._post_payload(
            payload,
            webhook_key,
            f"飞书推送成功 [{webhook_key}]，共 {len(symbols)} 只股票",
        )

    def send_system_alert(
        self,
        title: str,
        message: str,
        data_date: str | None = None,
        webhook_key: str = "system_operations",
    ) -> None:
        """推送数据同步等系统级异常告警。"""
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": f"🚨 Sequoia-X | {title}"},
                    "template": "red",
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": (
                                f"**告警日期：** {date.today().isoformat()}\n"
                                f"**本地数据日期：** {data_date or '未知'}\n"
                                f"**处理方式：** 继续使用本地数据执行持仓、策略和组合决策\n\n"
                                f"**异常信息：**\n{message}"
                            ),
                        },
                    }
                ],
            },
        }
        self._post_payload(payload, webhook_key, f"系统告警飞书推送成功 [{webhook_key}]")

    def _build_combined_detail_card(
        self,
        details: list,
        data_date: str | None,
        stock_names: dict[str, str],
        consecutive_counts: dict[str, int] | None = None,
        batch_index: int = 1,
        batch_count: int = 1,
    ) -> dict:
        """构建包含完整组合评分明细的候选卡片。"""
        rows = []
        counts = consecutive_counts or {}
        for item in details:
            xq_code = self._to_xueqiu_code(item.symbol)
            name = stock_names.get(item.symbol, item.symbol)
            factor_rank = str(item.factor_rank) if item.factor_rank is not None else "未进入前10"
            factor_score = f"{item.factor_score:.3f}" if item.factor_score is not None else "-"
            reason = (
                "综合趋势明确买点确认"
                if item.trend_confirmed
                else "跨策略组共振并通过趋势风险过滤"
            )
            rows.append(
                f"### [{name} {item.symbol}](https://xueqiu.com/S/{xq_code}) "
                f"｜连续入选 {counts.get(item.symbol, 1)} 次\n"
                f"**组合评分：** {item.combined_score:.1f}｜"
                f"**趋势评分：** {item.trend_score:.1f}｜"
                f"**趋势信号：** {item.trend_signal}\n"
                f"**入选原因：** {reason}｜"
                f"**风险通过：** {'是' if item.risk_passed else '否'}｜"
                f"**趋势确认：** {'是' if item.trend_confirmed else '否'}\n"
                f"**策略来源（{item.vote_count}）：** "
                f"{', '.join(item.sources) if item.sources else '-'}\n"
                f"**策略组（{item.family_count}）：** "
                f"{', '.join(item.families) if item.families else '-'}｜"
                f"信号得分 {item.signal_score:.1f}\n"
                f"**低价多因子：** 排名 {factor_rank}｜原始分 {factor_score}｜"
                f"组合贡献 {item.factor_contribution:.1f}"
            )
        suffix = f"（{batch_index}/{batch_count}）" if batch_count > 1 else ""
        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"🎯 Sequoia-X 组合决策重点候选{suffix}",
                    },
                    "template": "blue",
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": (
                                f"**数据日期：** {data_date or '未知'}\n"
                                f"**本卡候选：** {len(details)} 只"
                            ),
                        },
                    },
                    {"tag": "hr"},
                    {"tag": "div", "text": {"tag": "lark_md", "content": "\n\n".join(rows)}},
                    {
                        "tag": "note",
                        "elements": [
                            {
                                "tag": "plain_text",
                                "content": "组合评分仅用于策略研究与候选排序，不构成投资建议。",
                            }
                        ],
                    },
                ],
            },
        }

    def send_combined_selection(
        self,
        details: list,
        data_date: str | None,
        stock_names: dict[str, str] | None = None,
        consecutive_counts: dict[str, int] | None = None,
        max_chars: int = 12000,
        webhook_key: str = "core_decision",
    ) -> None:
        """按飞书消息长度自动合并或拆分组合候选完整明细。"""
        if not details:
            return
        names = stock_names or {}
        batches: list[list] = []
        current: list = []
        for item in details:
            candidate = current + [item]
            payload = self._build_combined_detail_card(
                candidate,
                data_date,
                names,
                consecutive_counts,
                batch_index=99,
                batch_count=99,
            )
            payload_size = len(json.dumps(payload, ensure_ascii=False))
            if current and payload_size > max_chars:
                batches.append(current)
                current = [item]
            else:
                current = candidate
        if current:
            batches.append(current)

        for index, batch in enumerate(batches, start=1):
            payload = self._build_combined_detail_card(
                batch,
                data_date,
                names,
                consecutive_counts,
                batch_index=index,
                batch_count=len(batches),
            )
            self._post_payload(
                payload,
                webhook_key,
                f"组合决策明细推送成功 [{webhook_key}] {index}/{len(batches)}，"
                f"共 {len(batch)} 只",
            )

    def send_prediction(
        self,
        results: list,
        metrics: dict[str, float],
        stock_names: dict[str, str] | None = None,
        webhook_key: str = "core_decision",
    ) -> None:
        """将指定股票的概率预测结果推送至飞书。"""
        if not results:
            logger.info("无有效预测结果，跳过飞书推送")
            return
        payload = self._build_prediction_card(results, metrics, stock_names=stock_names)
        self._post_payload(
            payload,
            webhook_key,
            f"预测结果飞书推送成功 [{webhook_key}]，共 {len(results)} 只股票",
        )

    def send_prediction_tracking(
        self,
        report,
        max_chars: int = 12000,
        webhook_key: str = "core_decision",
    ) -> None:
        """推送自动多周期预测、到期准确性和剩余周期刷新结果。"""
        blocks: list[str] = []
        stock_sections: dict[tuple[str, str], list[str]] = {}
        seen_evaluations: set[tuple[str, int]] = set()
        seen_predictions: set[tuple[str, int, int, bool]] = set()

        for item in report.evaluations:
            key = (item.symbol, item.horizon)
            if key in seen_evaluations:
                continue
            seen_evaluations.add(key)
            stock_sections.setdefault((item.symbol, item.name), []).append(
                f"✅ 第{item.horizon}个交易日验证｜"
                f"预测方向：**{item.predicted_direction}**｜实际收益：{item.actual_return:+.2%}｜"
                f"结果：**{'准确' if item.accurate else '不准确'}**"
            )
        for item in report.predictions:
            key = (
                item.symbol,
                item.target_horizon,
                item.remaining_horizon,
                item.refreshed,
            )
            if key in seen_predictions:
                continue
            seen_predictions.add(key)
            label = "刷新预测" if item.refreshed else "初始预测"
            stock_sections.setdefault((item.symbol, item.name), []).append(
                f"🔮 {label}｜周期第{item.target_horizon}个交易日｜"
                f"距当前剩余：{item.remaining_horizon}个交易日｜"
                f"方向：**{item.direction}**｜上涨概率：{item.probability:.1%}｜"
                f"历史同概率组收益：{item.expected_return:+.2%}"
            )
        for (symbol, name), lines in stock_sections.items():
            blocks.append(f"### {name} {symbol}\n" + "\n".join(lines))
        for error in report.errors:
            blocks.append(f"### ⚠️ 预测处理异常\n{error}")
        if not blocks and not report.completed:
            return

        batches: list[list[str]] = []
        current: list[str] = []

        def build_payload(items: list[str], index: int, count: int) -> dict:
            suffix = f"（{index}/{count}）" if count > 1 else ""
            return {
                "msg_type": "interactive",
                "card": {
                    "header": {
                        "title": {
                            "tag": "plain_text",
                            "content": f"📊 Sequoia-X 多周期预测跟踪{suffix}",
                        },
                        "template": "purple",
                    },
                    "elements": [
                        {
                            "tag": "div",
                            "text": {
                                "tag": "lark_md",
                                "content": (
                                    f"**数据日期：** {report.data_date}\n"
                                    f"**新建周期：** {len(report.started)}只｜"
                                    f"**完成并重置：** {len(report.completed)}只"
                                ),
                            },
                        },
                        {"tag": "hr"},
                        {"tag": "div", "text": {"tag": "lark_md", "content": "\n\n".join(items)}},
                    ],
                },
            }

        for block in blocks:
            candidate = current + [block]
            if current and len(json.dumps(build_payload(candidate, 99, 99), ensure_ascii=False)) > max_chars:
                batches.append(current)
                current = [block]
            else:
                current = candidate
        if current:
            batches.append(current)
        if not batches:
            batches = [["本轮没有新增预测或到期验证。"]]
        for index, batch in enumerate(batches, start=1):
            self._post_payload(
                build_payload(batch, index, len(batches)),
                webhook_key,
                f"多周期预测跟踪推送成功 [{webhook_key}] {index}/{len(batches)}",
            )

    def send_intraday_alerts(self, alerts: list, webhook_key: str = "intraday_trading") -> None:
        """推送盘中新预警；同日去重由 IntradayMonitor 负责。"""
        if not alerts:
            logger.info("无盘中新预警，跳过飞书推送")
            return
        self._post_payload(
            self._build_intraday_alert_card(alerts),
            webhook_key,
            f"盘中预警飞书推送成功 [{webhook_key}]，共 {len(alerts)} 条",
        )

    def send_intraday_status(
        self,
        monitored_count: int,
        quoted_count: int,
        account_count: int,
        webhook_key: str = "intraday_trading",
    ) -> None:
        """盘中无新信号时推送监控正常状态。"""
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": "✅ Sequoia-X 盘中监控正常"},
                    "template": "green",
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": (
                                f"**监控日期：** {date.today().isoformat()}\n"
                                f"**监控股票：** {monitored_count} 只\n"
                                f"**取得实时报价：** {quoted_count} 只\n"
                                f"**模拟账户：** {account_count} 个\n\n"
                                "本轮检查完成，**暂无新的盘中预警信号**。"
                            ),
                        },
                    }
                ],
            },
        }
        self._post_payload(payload, webhook_key, f"盘中无信号状态推送成功 [{webhook_key}]")

    def send_paper_trades(self, trades: list, webhook_key: str = "intraday_trading") -> None:
        """推送本轮模拟交易成交记录。"""
        if not trades:
            return
        rows = []
        for trade in trades[:50]:
            xq_code = self._to_xueqiu_code(trade.symbol)
            rows.append(
                f"[{trade.name} {trade.symbol}](https://xueqiu.com/S/{xq_code})｜"
                f"**{trade.action}** {trade.shares}股｜价格 {trade.price:.3f}｜"
                f"金额 {trade.amount:,.2f}元\n{trade.reason}｜{trade.traded_at}"
            )
        if len(trades) > 50:
            rows.append(f"其余 {len(trades) - 50} 笔成交因消息长度限制未展示")
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": "🧪 Sequoia-X 模拟交易成交"},
                    "template": "orange",
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**本轮成交：** {len(trades)} 笔",
                        },
                    },
                    {"tag": "hr"},
                    {"tag": "div", "text": {"tag": "lark_md", "content": "\n\n".join(rows)}},
                    {
                        "tag": "note",
                        "elements": [
                            {
                                "tag": "plain_text",
                                "content": "仅为本地模拟交易，不会修改真实持仓或发出真实委托。",
                            }
                        ],
                    },
                ],
            },
        }
        self._post_payload(
            payload,
            webhook_key,
            f"模拟交易成交推送成功 [{webhook_key}]，共 {len(trades)} 笔",
        )

    def send_portfolio(
        self,
        portfolio: pd.DataFrame,
        webhook_key: str = "portfolio_management",
    ) -> None:
        """推送当前自选、持仓市值和收益。"""
        if portfolio.empty:
            logger.info("组合为空，跳过飞书推送")
            return
        self._post_payload(
            self._build_portfolio_card(portfolio),
            webhook_key,
            f"持仓信息飞书推送成功 [{webhook_key}]",
        )

    def send_portfolio_report(
        self,
        portfolio: pd.DataFrame,
        report,
        webhook_key: str = "portfolio_management",
    ) -> None:
        """一次推送持仓概览和下一工作日操作观察。"""
        if portfolio.empty:
            logger.info("组合为空，跳过飞书推送")
            return
        self._post_payload(
            self._build_portfolio_report_card(portfolio, report),
            webhook_key,
            f"持仓与操作观察飞书推送成功 [{webhook_key}]",
        )

    def send_portfolio_advice(
        self,
        report,
        webhook_key: str = "portfolio_management",
    ) -> None:
        """推送下一工作日规则化操作建议。"""
        if isinstance(report, list):
            report = SimpleNamespace(advice=report, candidates=[], replacements=[])
        if not report or not report.advice:
            logger.info("无组合操作建议，跳过飞书推送")
            return
        self._post_payload(
            self._build_portfolio_advice_card(report),
            webhook_key,
            f"组合操作建议飞书推送成功 [{webhook_key}]",
        )
