"""飞书通知模块：将选股结果通过 Webhook 推送至飞书群。"""

import json
from datetime import date

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

    def send_prediction(
        self,
        results: list,
        metrics: dict[str, float],
        stock_names: dict[str, str] | None = None,
        webhook_key: str = "prediction",
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
