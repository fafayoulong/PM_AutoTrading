# -*- coding: utf-8 -*-
"""Small-size live execution for Polymarket MLB end-game lock signals."""

import asyncio
import json
import logging
import math
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from dotenv import load_dotenv

from src.config import Config
from src.atomic_io import atomic_write_text
from src.entry_price import buffered_entry_price as _shared_buffered_entry_price
from src.entry_price import tick_size_of as _shared_tick_size_of
from src.trading_day import trading_day


logger = logging.getLogger(__name__)


ACTIVE_STATUSES = {"PENDING", "OPEN", "PARTIALLY_FILLED", "SUBMITTED"}
CLOSED_STATUSES = {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"}
DRY_RUN_STATUS = "DRY_RUN"


def apply_exchange_fill_price(position, info: dict) -> bool:
    """把仓位的入场价从「委托限价」改成「交易所真实成交均价」。

    背景（2026-10-26 用户："1.10 买 0.98，收益应该是 0.022，实盘只有 0.02"）
    -------------------------------------------------------------------------
    `_apply_remote_order` 里写的是 `average_price = price or limit_price`，
    但 `price` 是**委托限价字段**，不是成交明细。所以入场价、成本、盈亏全部按限价算：

        Team Nemesis Academy（10-26）：委托 0.98、真实成交 **0.9699**
        本地 pnl = 1.12×(1−0.98) = 0.0224；真实毛利 = 1.12−(1.12×0.9699) = **0.0337**
        —— 低估约 33%。交易所 `/positions` 与 `/closed-positions` 都带 `avgPrice`。

    规则：
      * `avg_price` 必须落在 (0, 1) 内、且与当前入场价不同，才采纳；
      * 采纳后重算 `cost = size × avg_price`；
      * 已结算且结算价是干净的 0/1 时，顺带重算 `pnl`（避免"限价×份额"的旧账）。
    返回 True 表示改动了。
    """
    try:
        avg = float(info.get("avg_price", info.get("avgPrice")) or 0)
    except (TypeError, ValueError):
        return False
    size = float(getattr(position, "size", 0) or 0)
    if not (0.0 < avg < 1.0) or size <= 0:
        return False
    current = float(getattr(position, "entry_price", 0) or 0)
    if current > 0 and abs(avg - current) < 1e-6:
        return False
    position.entry_price = round(avg, 6)
    position.cost = round(size * avg, 6)
    exit_price = getattr(position, "exit_price", None)
    status = str(getattr(position, "status", "") or "")
    if exit_price in (0.0, 1.0) and status.startswith("CLOSED"):
        position.pnl = round(size * float(exit_price) - position.cost, 6)
    return True


def _field(obj: object, *names: str, default=None):
    if isinstance(obj, dict):
        for name in names:
            if obj.get(name) is not None:
                return obj[name]
        return default
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return default


def _matching_maker_orders(trade, order_id: str):
    oid = str(order_id or "")
    if not oid:
        return []
    return [
        maker
        for maker in (_field(trade, "maker_orders", default=[]) or [])
        if str(_field(maker, "order_id", "orderID", default="")) == oid
    ]


def summarize_fill(trades, order_id: str = "") -> tuple[float, float] | None:
    """把一笔委托的多次成交折成 (总份额, 加权均价)；没有有效成交返回 None。

    为什么需要（2026-10-28 用户报错 `OpenOrder response did not match expected shape`）
    ---------------------------------------------------------------------------------
    `SecureClient.get_order(order_id=...)` 只能读**仍在挂单簿里**的订单；订单一旦
    全部成交/撤销，接口返回 `null`，SDK 的 `OpenOrder.parse_response(None)` 就抛
    `UnexpectedResponseError`。于是本地订单永远停在 PENDING/OPEN，还会在面板报错。

    真正权威的是 `list_account_trades()` —— 每行带 `taker_order_id`、`price`、
    `size` 和 `maker_orders`。如果我们是 maker，顶层 `size` 是整笔 taker 成交，
    不能整笔记到我们头上；只能累加 `maker_orders[].matched_amount`。

    2026-11-03 Maria Timofeeva：我们挂 5 份，顶层成交 50 份，
    其中我们只有 5 份；旧逻辑记成 50 份并虚增亏损 $20.15。
    """
    size = 0.0
    notional = 0.0
    for trade in trades or []:
        try:
            if order_id:
                if str(_field(trade, "taker_order_id", default="")) == order_id:
                    s = _field(trade, "size", default=0)
                    p = _field(trade, "price", default=0)
                else:
                    matches = _matching_maker_orders(trade, order_id)
                    if not matches:
                        continue
                    s = sum(
                        float(_field(maker, "matched_amount", default=0) or 0)
                        for maker in matches
                    )
                    p = (
                        sum(
                            float(_field(maker, "matched_amount", default=0) or 0)
                            * float(_field(maker, "price", default=0) or 0)
                            for maker in matches
                        )
                        / s
                        if s > 0
                        else 0
                    )
            else:
                s = _field(trade, "size", default=0)
                p = _field(trade, "price", default=0)
            s = float(s or 0)
            p = float(p or 0)
        except (TypeError, ValueError):
            continue
        if s <= 0 or p <= 0:
            continue
        size += s
        notional += s * p
    if size <= 0:
        return None
    return size, notional / size


def trade_belongs_to_order(trade, order_id: str) -> bool:
    """这笔成交是不是这张委托的（我们可能是 taker，也可能吃到 maker 单）。"""
    oid = str(order_id or "")
    if not oid:
        return False
    if str(getattr(trade, "taker_order_id", "") or "") == oid:
        return True
    return bool(_matching_maker_orders(trade, oid))


def _beijing_clock(moment: Optional[float] = None) -> str:
    """北京时间 HH:MM:SS，用于报错与日志（面板与文档统一用北京时间）。"""
    stamp = time.time() if moment is None else moment
    try:
        from zoneinfo import ZoneInfo

        return datetime.fromtimestamp(stamp, ZoneInfo("Asia/Shanghai")).strftime("%H:%M:%S")
    except Exception:  # noqa: BLE001 - 时区数据缺失时退回 UTC
        return time.strftime("%H:%M:%S", time.gmtime(stamp))


@dataclass
class LiveOrder:
    order_id: str
    game_id: str
    token_id: str
    team: str
    sport: str = ""
    side: str = "BUY"
    limit_price: float = 0.0
    size: float = 0.0
    amount: float = 0.0
    status: str = "PENDING"
    filled_size: float = 0.0
    average_price: float = 0.0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    raw: dict = field(default_factory=dict)
    intent: dict = field(default_factory=dict)
    request: dict = field(default_factory=dict)
    result: dict = field(default_factory=dict)
    drill: bool = False


@dataclass
class LivePosition:
    position_id: str
    order_id: str
    game_id: str
    token_id: str
    team: str
    size: float
    entry_price: float
    cost: float
    sport: str = ""
    status: str = "OPEN"
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    opened_at: float = field(default_factory=time.time)
    closed_at: Optional[float] = None
    strategy: str = ""


class LiveTrader:
    def __init__(self, config: Config):
        self.config = config
        load_dotenv(override=False)
        self.enabled = self._bool(os.getenv("LIVE_EXECUTION_ENABLED"), config.live_execution_enabled)
        self.global_enabled = self._bool(os.getenv("LIVE_GLOBAL_ENABLED"), config.live_global_enabled)
        self.unattended_enabled = self._bool(os.getenv("LIVE_UNATTENDED_ENABLED"), config.live_unattended_enabled)
        self.allowed_sports = {x.strip().upper() for x in os.getenv("LIVE_ALLOWED_SPORTS", ",".join(config.live_allowed_sports)).split(",") if x.strip()}
        self.max_order_size = self._float(os.getenv("LIVE_MAX_ORDER_SIZE_USDC"), config.live_max_order_size_usdc)
        self.max_daily_spend = self._float(os.getenv("LIVE_MAX_DAILY_SPEND_USDC"), config.live_max_daily_spend_usdc)
        self.max_daily_loss = self._float(os.getenv("LIVE_MAX_DAILY_LOSS_USDC"), config.live_max_daily_loss_usdc)
        self.order_timeout = self._int(os.getenv("LIVE_ORDER_TIMEOUT_SECONDS"), config.live_order_timeout_seconds)
        self.min_order_size = self._float(os.getenv("LIVE_MIN_ORDER_SIZE_USDC"), config.live_min_order_size_usdc)
        self.abnormal_pause_enabled = self._bool(os.getenv("LIVE_ABNORMAL_PAUSE_ENABLED"), config.live_abnormal_pause_enabled)
        self.max_book_age_seconds = self._float(os.getenv("LIVE_MAX_BOOK_AGE_SECONDS"), config.live_max_book_age_seconds)
        self.max_book_spread = self._float(os.getenv("LIVE_MAX_BOOK_SPREAD"), config.live_max_book_spread)
        self.drill_mode = self._bool(
            os.getenv("SIGNAL_DRILL_MODE"), getattr(config, "signal_drill_mode", False)
        )
        self.dry_run = self._bool(
            os.getenv("LIVE_DRY_RUN"), getattr(config, "live_dry_run", False)
        )
        if self.drill_mode and not self.dry_run:
            # 演练会产生大量信号，强制干跑，避免批量真下单
            self.dry_run = True
            logger.warning("SIGNAL_DRILL_MODE 已开启，自动强制 LIVE_DRY_RUN")
        self.orders: dict[str, LiveOrder] = {}
        self.positions: dict[str, LivePosition] = {}
        self.dedupe_keys: set[str] = set()
        self.armed = False
        self.globally_stopped = False
        self.abnormal_paused = False
        self.pause_reason = ""
        self.abnormal_paused_at = 0.0
        self.last_reject_reason = ""
        self.circuit_breaker = "" if self.global_enabled else "GLOBAL_DISABLED"
        self.sport_circuit_breakers: dict[str, str] = {}
        self.client = None
        self.client_ready = False
        # 客户端初始化失败的原因要单独留一份：`clear_stale_error` 会在面板改设置时
        # 清掉 last_error（2026-09-18 就是这样把"初始化失败"的原因抹掉了，
        # 面板只剩一句"实盘客户端未就绪"，看不出是网络问题）。
        self.client_error = ""
        self.client_error_at = 0.0
        self._client_rebuild_at = 0.0
        # Polymarket API 可达性（2026-09-18）：面板上的"一切正常"曾经骗了我们 ——
        # armed=true、无熔断、无 blockers，实际上下单通道整晚都是坏的。
        # 这里按"实盘客户端 / 公共 REST / 市场数据(Gamma)"三条链路分别探活。
        self._api_health: dict[str, dict] = {}
        self._api_health_checked_at = 0.0
        # 下单后的短轮询任务（状态一变就写回，不用等 5 秒的全局轮询）
        self._confirm_tasks: set = set()
        self._tie_event_recorder = None
        self.wallet_type = ""
        self.signer_type = ""
        # last_error 用 property 暴露（见下方），赋值时自动记录时间戳；
        # 这样 31 处失败点不用逐一改，也能在面板上标出「这是几点几分报的」。
        self._last_error = ""
        self._last_error_at: Optional[float] = None
        self._stop_event = asyncio.Event()
        self._monitor_task: Optional[asyncio.Task] = None
        self._state_path = Path(__file__).resolve().parent.parent / config.live_orders_path
        self._events_path = Path(__file__).resolve().parent.parent / config.live_order_events_path
        self._load_state()
        self._check_daily_loss()
        self._build_client()

    def _bool(self, value: object, default: bool = False) -> bool:
        if value is None or value == "":
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def set_tie_event_recorder(self, recorder) -> None:
        self._tie_event_recorder = recorder

    def _record_tie_order_update(self, order: LiveOrder, source: str, at: float) -> None:
        recorder = getattr(self, "_tie_event_recorder", None)
        if recorder is None:
            return
        if str((order.intent or {}).get("strategy") or "") != "tennis_deciding_tie_low":
            return
        try:
            recorder.record_order_update(order, source=source, at=at)
        except Exception as exc:  # noqa: BLE001 - telemetry must never block trading
            logger.debug("网球事件账本成交更新失败：%s", exc)

    # ---------- 错误信息（带时间戳） ----------

    @property
    def last_error(self) -> str:
        return getattr(self, "_last_error", "")

    @last_error.setter
    def last_error(self, value: object) -> None:
        text = str(value or "")
        self._last_error = text
        if text:
            self._last_error_at = time.time()

    @property
    def last_error_at(self) -> Optional[float]:
        return getattr(self, "_last_error_at", None)

    def clear_stale_error(self, reason: str = "风控设置已更新") -> None:
        """清掉上一次的失败原因。

        面板上的「最近错误」是snapshot，不是当前状态：提高上限之后如果还挂着旧报错，
        用户会以为限制没解除（实际发生过）。设置一变就清空，让下一次失败重新计时。
        """
        if self.last_error:
            logger.info("清除上一次实盘错误信息（%s）：%s", reason, self.last_error)
        self.last_error = ""
        self._last_error_at = None
        self.last_reject_reason = ""

    def _float(self, value: object, default: float) -> float:
        try:
            parsed = float(value)
            return parsed if parsed >= 0 else default
        except (TypeError, ValueError):
            return default

    def _int(self, value: object, default: int) -> int:
        try:
            parsed = int(value)
            return parsed if parsed > 0 else default
        except (TypeError, ValueError):
            return default

    def _build_client(self):
        private_key = os.getenv("PM_PRIVATE_KEY", "")
        signer = os.getenv("PM_SIGNER_ADDRESS") or os.getenv("PM_WALLET_ADDRESS", "")
        funder = os.getenv("PM_FUNDER_ADDRESS") or signer
        relayer_key = os.getenv("PM_RELAYER_API_KEY", "")
        relayer_address = os.getenv("PM_RELAYER_API_KEY_ADDRESS", "")

        if not private_key:
            self.last_error = "缺少 PM_PRIVATE_KEY"
            return
        if not funder:
            self.last_error = "缺少 PM_FUNDER_ADDRESS"
            return

        derived = self._account_address(private_key)
        if derived and signer and derived.lower() != signer.lower():
            self.last_error = "PM_SIGNER_ADDRESS 与私钥地址不一致"
            return

        try:
            from polymarket import RelayerApiKey, SecureClient

            if self.config.proxy_url:
                os.environ.setdefault("HTTP_PROXY", self.config.proxy_url)
                os.environ.setdefault("HTTPS_PROXY", self.config.proxy_url)

            api_key = None
            if relayer_key and relayer_address:
                api_key = RelayerApiKey(key=relayer_key, address=relayer_address)

            client = SecureClient.create(
                private_key=private_key,
                wallet=funder,
                api_key=api_key,
            )
            self._relax_sdk_transport_timeouts(client)
            self.client = client
            self.client_ready = True
            self.wallet_type = str(getattr(client._ctx, "wallet_type", ""))
            self.signer_type = str(getattr(client._ctx, "signer_type", ""))
            self.last_error = ""
            self.client_error = ""
            self.client_error_at = 0.0
        except Exception as exc:
            self.client_ready = False
            self.client_error = f"实盘客户端初始化失败：{type(exc).__name__}: {exc}"[:300]
            self.client_error_at = time.time()
            self.last_error = self.client_error
            logger.warning("%s", self.client_error)

    # 官方 SDK 的 httpx 默认超时：connect=5 / read=10 / write=10 / **pool=2**
    # pool=2 秒是 2026-09-19 那次"连续 43 分钟一单没成交"的直接原因（见 _describe_transport_error）。
    # 代理一抖动，连接池拿不到连接 2 秒就放弃；而下单是串在盘口读取之后的最紧一环，
    # 所以最先阵亡。放宽 pool 之后，下单有机会等连接释放，而不是被误判成"网络断了"。
    SDK_RELAXED_TIMEOUT = (10.0, 20.0, 20.0, 20.0)  # connect / read / write / pool

    def _relax_sdk_transport_timeouts(self, client) -> None:
        """把 SDK 内部 httpx 客户端的超时放宽（失败不影响客户端可用性）。

        只动超时，不动地址/认证/连接上限；SDK 升级后属性路径变了就静默跳过，
        退回 SDK 默认值（行为与改动前一致）。
        """
        try:
            import httpx
        except ImportError:  # pragma: no cover - httpx 是 SDK 依赖
            return
        connect, read, write, pool = self.SDK_RELAXED_TIMEOUT
        relaxed = httpx.Timeout(connect=connect, read=read, write=write, pool=pool)
        ctx = getattr(client, "_ctx_inner", None) or getattr(client, "_ctx", None)
        if ctx is None:
            return
        patched = []
        for name in ("secure_clob", "relayer"):
            transport = getattr(ctx, name, None)
            http_client = getattr(transport, "_client", None)
            if isinstance(http_client, httpx.Client):
                http_client.timeout = relaxed
                patched.append(name)
        if patched:
            logger.info(
                "实盘客户端超时已放宽（%s）：connect=%.0fs read=%.0fs write=%.0fs pool=%.0fs",
                "/".join(patched), connect, read, write, pool,
            )

    @staticmethod
    def _describe_transport_error(exc: BaseException) -> str:
        """把传输层异常翻成人能看懂的原因。

        为什么需要：SDK 抛的是 `TransportError(str(error) or "Request failed")`，
        而 httpx 的 **PoolTimeout 的 str() 就是空串** —— 于是真实原因（拿不到连接）
        在日志里只剩一句毫无信息量的 "Request failed"。
        2026-09-19 11:24~12:07 连续 5 次下单失败全部是这种情况。
        """
        text = str(exc).strip()
        if text:
            return text
        name = type(exc).__name__
        if name in {"PoolTimeout", "TransportError"}:
            return (
                "连接池超时（拿不到连接；通常是代理/网络拥塞，"
                "SDK 默认 pool=2s，本进程已放宽到 20s）"
            )
        return f"{name}（无错误详情）"

    def _account_address(self, private_key: str) -> str:
        try:
            from eth_account import Account
            return Account.from_key(private_key).address
        except Exception:
            return ""

    async def start(self):
        # 即使客户端没建起来也要跑监控循环：它负责 ① 定期重建客户端
        # ② 探活 Polymarket 三条链路（面板状态灯）——这两件事恰恰在最坏的时候最需要。
        if self._monitor_task:
            return
        self._stop_event.clear()
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def stop(self):
        self._stop_event.set()
        for task in list(self._confirm_tasks):
            task.cancel()
        self._confirm_tasks.clear()
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        if self.client is not None:
            try:
                await asyncio.to_thread(self.client.close)
            except Exception as exc:
                logger.warning("Failed to close live client: %s", exc)

    def disarm(self):
        self.armed = False

    def blockers(self) -> list[str]:
        """列出当前阻止实盘下单的条件，供日志与面板展示。"""
        items = []
        if not self.enabled:
            items.append("执行开关关闭（LIVE_EXECUTION_ENABLED）")
        if not self.global_enabled:
            items.append("全局总开关关闭（LIVE_GLOBAL_ENABLED）")
        if not self.unattended_enabled:
            items.append("无人值守关闭（LIVE_UNATTENDED_ENABLED）")
        if self.globally_stopped:
            items.append("已触发全局停止（需在面板切回实盘模式解除）")
        if not self.armed:
            items.append("面板未 arm（需在面板切换到实盘模式）")
        if self.circuit_breaker:
            items.append(f"风控熔断：{self.circuit_breaker}")
        if not self.client_ready:
            items.append("实盘客户端未就绪")
        return items

    def arm(self) -> None:
        """允许面板重新武装实盘，同时解除全局停止。"""
        self.globally_stopped = False
        self.armed = True
        if self.circuit_breaker == "GLOBAL_DISABLED" and self.global_enabled:
            self.circuit_breaker = ""
        # 上一次下单时可能因为尚未 arm 留下“自动实盘未就绪”。执行器现已恢复时
        # 这条错误已经不再代表当前状态；只有 blockers 为空才清除，真实故障仍保留。
        self.clear_stale_readiness_error("实盘已重新 arm")

    def clear_stale_readiness_error(self, reason: str = "执行器已恢复就绪") -> bool:
        """清除已经恢复就绪后的旧 readiness 错误。

        `last_error` 是最近一次失败的快照。用户重新 arm 或启动时恢复实盘模式后，
        如果当前没有任何 blockers，旧的“自动实盘未就绪”不能继续挂在首页；
        但客户端离线、熔断、开关关闭等真实故障必须继续显示。
        """
        if not self.last_error.startswith("自动实盘未就绪："):
            return False
        if self.blockers():
            return False
        self.clear_stale_error(reason)
        return True

    def _mark_abnormal(self, reason: str, message: str) -> None:
        """记录最近一次异常行情。仅作为状态展示，不再作为长期闸门。"""
        self.abnormal_paused = True
        self.pause_reason = reason
        self.abnormal_paused_at = time.time()
        self.last_error = message

    def apply_global_settings(self, data: dict) -> None:
        """应用面板下发的实盘全局开关与上限。"""
        if "execution_enabled" in data:
            self.enabled = bool(data["execution_enabled"])
        if "global_enabled" in data:
            self.global_enabled = bool(data["global_enabled"])
        if "unattended_enabled" in data:
            self.unattended_enabled = bool(data["unattended_enabled"])
        if "dry_run" in data:
            self.dry_run = bool(data["dry_run"])
        if "drill_mode" in data:
            self.drill_mode = bool(data["drill_mode"])
        if self.drill_mode and not self.dry_run:
            # 演练会产生大量信号，强制干跑，避免批量真下单
            self.dry_run = True
            logger.warning("演练模式已开启，自动强制干跑")
        for key, attr in (
            ("max_order_size_usdc", "max_order_size"),
            ("max_daily_spend_usdc", "max_daily_spend"),
            ("max_daily_loss_usdc", "max_daily_loss"),
        ):
            if key in data:
                setattr(self, attr, max(0.0, float(data[key])))

        if self.global_enabled:
            if self.circuit_breaker == "GLOBAL_DISABLED":
                self.circuit_breaker = ""
        else:
            self.circuit_breaker = "GLOBAL_DISABLED"

        if not (self.enabled and self.global_enabled and self.unattended_enabled):
            self.disarm()

        # 用户刚改过开关/上限，之前的「最近错误」已过时
        self.clear_stale_error("全局设置已更新")
        self._check_daily_loss()

    async def submit_limit_buy(
        self, signal, market, token_id: str, sport: str, book=None
    ) -> Optional[LiveOrder]:
        sport = sport.upper()
        self._append_event("LIVE_SUBMIT_ENTRY", {
            "sport": sport,
            "game_id": signal.game_id,
            "token_id": token_id,
        })
        if self.circuit_breaker:
            self.last_error = f"风控熔断：{self.circuit_breaker}"
            return None
        blockers = self.blockers()
        if blockers:
            # 逐条列出，避免只给一句聚合文案导致无法定位
            self.last_error = "自动实盘未就绪：" + "、".join(blockers)
            self._append_event("LIVE_GATE_BLOCKED", {
                "sport": sport,
                "game_id": signal.game_id,
                "token_id": token_id,
                "blockers": blockers,
            })
            return None
        if not self.client_ready or self.client is None:
            self.last_error = "实盘客户端未初始化"
            self.last_reject_reason = self.last_error
            return None
        if sport not in self.allowed_sports:
            self.last_error = f"市场 {sport} 未允许实盘"
            return None
        if token_id in self.dedupe_keys:
            self.last_error = "重复下单：同一市场已提交过订单"
            return None
        rules = self.config.sport_rules.get(sport)
        if not rules or not rules.enabled:
            self.last_error = f"市场 {sport} 风控未启用"
            return None
        if self.sport_circuit_breakers.get(sport):
            self.last_error = f"市场 {sport} 熔断：{self.sport_circuit_breakers[sport]}"
            return None

        # 全局单笔上限与运动级上限同时生效，取更严格的一个
        order_cap = rules.live_max_order_size_usdc
        if self.max_order_size > 0:
            order_cap = min(order_cap, self.max_order_size)

        sport_open_positions = [p for p in self._open_positions() if p.sport == sport]
        if len(sport_open_positions) >= rules.live_max_open_positions:
            self.last_error = f"市场 {sport} 持仓数量达到上限"
            return None

        active_game_ids = {
            order.game_id for order in self.orders.values() if order.status in ACTIVE_STATUSES
        }
        if signal.game_id in active_game_ids:
            self.last_error = "重复下单：同一比赛已有活动订单"
            return None

        # 同一**市场**不重复下注 —— 光按 game_id 去重不够：同一场比赛会以两个 id 出现
        # （PM 实时流 6 位 id vs ESPN 9 位 id），在系统眼里是"两场比赛"，于是一起买两次
        # （2026-09-16 实测 HOU 10:11 @0.94 与 10:22 @0.95 是同一个 token）。
        # 这里按 token_id 再兜一层：已有未结算持仓、或已有活动订单，就不再下单。
        token_key = str(token_id or "")
        if token_key:
            held = [p for p in self._open_positions()
                    if str(getattr(p, "token_id", "") or "") == token_key]
            if held:
                self.last_error = "重复下单：同一市场已有未结算持仓"
                return None
            active_tokens = {
                str(getattr(order, "token_id", "") or "")
                for order in self.orders.values() if order.status in ACTIVE_STATUSES
            }
            if token_key in active_tokens:
                self.last_error = "重复下单：同一市场已有活动订单"
                return None

        # 当日额度按 trading_day（默认北京时间）换日，不是 UTC
        today = trading_day()
        sport_orders = [
            order for order in self.orders.values()
            if order.sport == sport
            and order.status != DRY_RUN_STATUS
            and trading_day(order.created_at) == today
        ]
        spend_today = sum(
            order.amount for order in self.orders.values()
            if order.status != DRY_RUN_STATUS
            and trading_day(order.created_at) == today
        )
        sport_spend_today = sum(order.amount for order in sport_orders)
        # 这里只拦「明显没希望」的情况：剩余额度连一笔最小订单都放不下。
        # 真正的额度判定放到算出实际下单金额之后（下面用 amount 的两处检查）。
        # 原来这里按「预留一整笔上限」预判，会把「剩余 2.02、只需下 1.99」的单误杀
        # （实测 CS2 已投入 7.97、上限 10、单笔 2.1 → 10.07 > 10 被判死，
        #  而实际金额 1.99 完全放得下）。
        sport_remaining = rules.live_max_daily_spend_usdc - sport_spend_today
        global_remaining = self.max_daily_spend - spend_today
        floor = rules.live_min_order_size_usdc
        if min(sport_remaining, global_remaining) < floor:
            # 说清楚是哪一层限额卡住的，别让用户去猜
            if global_remaining <= sport_remaining:
                detail = (
                    f"全局当日额度不足：已投入 {spend_today:.2f} / "
                    f"限额 {self.max_daily_spend:.2f}，剩余 {max(0.0, global_remaining):.2f}"
                )
            else:
                detail = (
                    f"市场 {sport} 当日额度不足：已投入 {sport_spend_today:.2f} / "
                    f"限额 {rules.live_max_daily_spend_usdc:.2f}，剩余 {max(0.0, sport_remaining):.2f}"
                )
            self.last_error = (
                f"{detail} < 最小下单金额 {floor:.2f}（北京时间 {_beijing_clock()}）"
            )
            return None

        if book is None:
            try:
                book = await asyncio.to_thread(self.client.get_order_book, token_id=token_id)
            except Exception as exc:
                self.last_error = f"盘口读取失败：{exc}"
                return None

        asks = self._book_entries(book, "asks")
        bids = self._book_entries(book, "bids")
        if not asks or not bids:
            self._mark_abnormal("ORDER_BOOK_ONE_SIDED", "异常行情暂停：盘口只有单边")
            return None

        best_ask = min(float(ask.price) for ask in asks)
        best_bid = max(float(bid.price) for bid in bids)
        tick = self._book_tick_size(book)
        tolerance = max(0.0, float(getattr(rules, "live_entry_price_tolerance", 0.0) or 0.0))
        max_price = float(rules.max_entry_price) + tolerance
        min_profit = max(0.0, float(rules.min_expected_profit) - tolerance)
        if best_ask > max_price:
            self._mark_abnormal(
                "PRICE_ABOVE_LIMIT",
                f"异常行情暂停：价格过高 {best_ask:.3f} > {max_price:.3f}",
            )
            return None
        # 市场一致性闸门：市场价明显低于"已锁定"该有的水平时不下单。
        # 这条与我们的判定是否正确无关，是纯兜底 —— 只要市场不认为锁定就跳过。
        min_entry = max(0.0, float(getattr(rules, "live_min_entry_price", 0.0) or 0.0))
        if min_entry > 0 and not self.drill_mode and best_ask < min_entry:
            self._mark_abnormal(
                "PRICE_BELOW_LOCK_THRESHOLD",
                f"市场价低于锁定阈值：ask {best_ask:.3f} < 最低入场价 {min_entry:.2f}"
                f"（市场不认为已锁定，跳过）（北京时间 {_beijing_clock()}）",
            )
            self._append_event(
                "PRICE_BELOW_LOCK_THRESHOLD",
                {
                    "sport": sport,
                    "game_id": signal.game_id,
                    "leader_team": getattr(signal, "leader_team", None) or getattr(signal, "leader_name", ""),
                    "best_ask": best_ask,
                    "min_entry_price": min_entry,
                    "at": time.time(),
                },
            )
            return None
        if 1.0 - best_ask < min_profit:
            self._mark_abnormal(
                "EXPECTED_PROFIT_TOO_LOW",
                f"异常行情暂停：理论收益不足 {1.0 - best_ask:.3f} < {min_profit:.3f}",
            )
            return None
        # 入场价区间闸门（2026-09-23）：两个账本唯一同时为正的区间之外不下单。
        # 这不是"异常行情"，所以只记事件、不做熔断暂停。
        if bool(getattr(self.config, "entry_band_enabled", False)) and not self.drill_mode:
            band_min = float(getattr(self.config, "entry_band_min", 0.0) or 0.0)
            band_max = float(getattr(self.config, "entry_band_max", 1.0) or 1.0)
            if band_max > band_min and (best_ask < band_min or best_ask >= band_max):
                self.last_error = (
                    f"入场价 {best_ask:.3f} 不在盈利区间 "
                    f"[{band_min:.2f}, {band_max:.2f}) 内"
                )
                self._append_event(
                    "ENTRY_BAND",
                    {
                        "sport": sport,
                        "game_id": signal.game_id,
                        "leader_team": getattr(signal, "leader_team", None)
                        or getattr(signal, "leader_name", ""),
                        "best_ask": best_ask,
                        "band_min": band_min,
                        "band_max": band_max,
                        "at": time.time(),
                    },
                )
                return None
        if best_bid > best_ask:
            self._mark_abnormal(
                "BOOK_CROSSED", f"异常行情暂停：盘口交叉 {best_bid:.3f} > {best_ask:.3f}"
            )
            return None
        if self.abnormal_pause_enabled and rules.live_max_book_spread > 0 and best_ask - best_bid > rules.live_max_book_spread:
            self._mark_abnormal(
                "BOOK_SPREAD_TOO_WIDE",
                f"异常行情暂停：盘口价差过大 {best_ask - best_bid:.3f}",
            )
            return None

        book_age = self._book_age_seconds(book)
        if self.abnormal_pause_enabled and rules.live_max_book_age_seconds > 0 and (book_age is None or book_age > rules.live_max_book_age_seconds):
            self._mark_abnormal(
                "BOOK_TOO_STALE", f"异常行情暂停：盘口时间戳异常 {book_age}"
            )
            return None

        depth_size = sum(float(ask.size) for ask in asks if float(ask.price) <= best_ask)
        if depth_size <= 0:
            self._mark_abnormal("BOOK_DEPTH_EMPTY", "异常行情暂停：盘口深度不足")
            return None

        self.abnormal_paused = False
        self.pause_reason = ""
        self.abnormal_paused_at = 0.0

        buffer_ticks = max(0, int(getattr(rules, "live_entry_buffer_ticks", 0) or 0))
        order_price = self._buffered_entry_price(
            best_ask, tick, buffer_ticks, min_profit
        )
        fillable_depth = sum(
            float(ask.size) for ask in asks if float(ask.price) <= order_price
        )

        max_size = order_cap / order_price
        min_required_size = (
            math.ceil(rules.live_min_order_size_usdc / order_price * 100) / 100
        )
        if max_size + 1e-9 < min_required_size:
            self.last_error = (
                f"有效单笔上限不足：${order_cap:.2f} 在当前价格只能买 "
                f"{max_size:.2f} 份，最低需要 {min_required_size:.2f} 份"
            )
            self._append_event("ORDER_CAP_BELOW_MIN", {
                "sport": sport,
                "game_id": signal.game_id,
                "token_id": token_id,
                "price": order_price,
                "order_cap_usdc": order_cap,
                "min_order_size_usdc": rules.live_min_order_size_usdc,
                "max_size": max_size,
                "min_required_size": min_required_size,
            })
            return None
        if fillable_depth + 1e-9 < min_required_size:
            self.last_error = (
                f"盘口深度不足：卖一价 {order_price:.3f} 只有 "
                f"{fillable_depth:.2f} 份，约 ${fillable_depth * order_price:.2f}；"
                f"最低需要 {min_required_size:.2f} 份"
            )
            self._append_event("BOOK_DEPTH_BELOW_MIN", {
                "sport": sport,
                "game_id": signal.game_id,
                "token_id": token_id,
                "price": order_price,
                "fillable_depth": fillable_depth,
                "fillable_amount": fillable_depth * order_price,
                "min_required_size": min_required_size,
                "min_order_size_usdc": rules.live_min_order_size_usdc,
            })
            return None
        size = min(max_size, fillable_depth)
        size = math.floor(size * 100) / 100
        amount = size * order_price
        if amount < rules.live_min_order_size_usdc:
            size = min_required_size
            amount = size * order_price
        if size <= 0:
            self.last_error = "订单数量过小"
            return None

        if amount < rules.live_min_order_size_usdc:
            self.last_error = (
                f"下单金额不足：成交后金额 {amount:.2f} "
                f"< 最低 {rules.live_min_order_size_usdc:.2f}"
            )
            return None
        if spend_today + amount > self.max_daily_spend:
            self.last_error = (
                f"订单会超过全局当日投入上限：已投入 {spend_today:.2f} + 本单 {amount:.2f} "
                f"> 上限 {self.max_daily_spend:.2f}（北京时间 {_beijing_clock()}）"
            )
            return None
        if sport_spend_today + amount > rules.live_max_daily_spend_usdc:
            self.last_error = (
                f"订单会超过市场 {sport} 当日投入上限：已投入 {sport_spend_today:.2f} "
                f"+ 本单 {amount:.2f} > 上限 {rules.live_max_daily_spend_usdc:.2f}"
                f"（北京时间 {_beijing_clock()}）"
            )
            return None

        intent = {
            "strategy": "sports_end_game_lock",
            "sport": sport,
            "game_id": signal.game_id,
            "leader_team": getattr(signal, "leader_team", None) or getattr(signal, "leader_name", ""),
            "underdog_team": getattr(signal, "underdog_team", None) or getattr(signal, "underdog_name", ""),
            "score": (
                f"{getattr(signal, 'leader_score', getattr(signal, 'sets_leader', 0))}-"
                f"{getattr(signal, 'underdog_score', getattr(signal, 'sets_underdog', 0))}"
            ),
            "market_id": getattr(market, "market_id", ""),
            "condition_id": getattr(market, "condition_id", ""),
            "token_id": token_id,
            "side": "BUY",
            "price": order_price,
            "best_ask": best_ask,
            "tick_size": tick,
            "entry_buffer_ticks": buffer_ticks,
            "price_tolerance": tolerance,
            "effective_max_entry_price": round(max_price, 6),
            "fillable_depth": fillable_depth,
            "size": size,
            "amount": amount,
            "max_loss": amount,
            "max_profit": size - amount,
            "reason": "End-game lock; buy leader below settlement price",
            "risk": {
                "max_order_size_usdc": rules.live_max_order_size_usdc,
                "min_entry_price": rules.live_min_entry_price,
                "max_open_positions": rules.live_max_open_positions,
                "max_daily_spend_usdc": rules.live_max_daily_spend_usdc,
                "max_daily_loss_usdc": rules.live_max_daily_loss_usdc,
                "order_timeout_seconds": rules.live_order_timeout_seconds,
                "min_order_size_usdc": rules.live_min_order_size_usdc,
            },
        }
        request_payload = {
            "token_id": token_id,
            "price": order_price,
            "best_ask": best_ask,
            "entry_buffer_ticks": buffer_ticks,
            "size": size,
            "side": "BUY",
            "order_type": "GTC",
        }
        self._append_event("PRE_ORDER", intent)

        if self.dry_run:
            return await self._dry_run_order(
                signal=signal,
                intent=intent,
                request_payload=request_payload,
                token_id=token_id,
                sport=sport,
                order_price=order_price,
                size=size,
                amount=amount,
            )

        try:
            response = await self._place_with_retry(
                token_id=token_id, order_price=order_price, size=size, intent=intent
            )
        except Exception as exc:
            reason = self._describe_transport_error(exc)
            self.last_error = f"下单失败：{reason}"
            self._append_event("SUBMIT_FAILED", {**intent, "error": reason})
            return None

        if getattr(response, "ok", True) is False:
            detail = getattr(response, "message", "") or getattr(response, "code", "")
            self.last_error = f"下单被拒绝：{detail}"
            self._append_event("SUBMIT_REJECTED", {**intent, "result": self._response_to_dict(response)})
            return None

        order_id = self._extract_order_id(response)
        if not order_id:
            self.last_error = f"下单返回缺少订单号：{response}"
            self._append_event("UNKNOWN_SUBMIT_RESULT", {**intent, "result": response})
            return None

        response_payload = self._response_to_dict(response)
        order = LiveOrder(
            order_id=order_id,
            game_id=signal.game_id,
            token_id=token_id,
            team=getattr(signal, "leader_team", None) or getattr(signal, "leader_name", ""),
            sport=sport,
            limit_price=order_price,
            size=size,
            amount=amount,
            status="PENDING",
            raw=response_payload,
            intent=intent,
            request=request_payload,
            result=response_payload,
        )
        self.orders[order_id] = order
        self._persist_dedupe(token_id)
        self._save_state()
        self._append_event("ORDER_SUBMITTED", intent | {"order_id": order_id, "result": order.result})
        # 下单响应经常是 `delayed`（还没成交），而状态刷新原本只靠 5 秒的全局轮询 ——
        # 实测（#441/#442）"下单成功"到"确认成交"要 4.3~4.9 秒，占整条链路的 70%。
        # 这里立刻起一个短轮询，状态一变就写回（见 _confirm_order_soon）。
        self._start_order_confirmation(order_id)
        logger.info(
            "LIVE LIMIT BUY: %s %s %.3f (ask %.3f + %d tick) x %.2f",
            sport,
            getattr(signal, "leader_team", None) or getattr(signal, "leader_name", ""),
            order_price,
            best_ask,
            buffer_ticks,
            size,
        )
        return order

    async def submit_tie_low_buy(
        self,
        *,
        game_id: str,
        selected: str,
        opponent: str,
        token_id: str,
        book,
        order_usdc: float,
        tie_state: dict,
        shadow_id: str,
    ) -> Optional[LiveOrder]:
        """Attempt the configured notional at the current ask for a tie signal.

        This path intentionally bypasses the normal one-order-per-game/token
        dedupe: 1-1, 2-2, ... 6-6 are independent observations.  It still obeys
        the global live arm state, API health blockers, daily spend and daily
        loss circuit breakers. The exchange requires a marketable BUY of at
        least $1; convert the target USDC to shares at the current ask so a
        low-priced outcome is not rejected as an undersized order.
        """
        sport = "TENNIS"
        tie_event_id = str(tie_state.get("event_id") or "")
        self._append_event("LIVE_SUBMIT_ENTRY", {
            "sport": sport,
            "game_id": game_id,
            "token_id": token_id,
            "strategy": "tennis_deciding_tie_low",
            "game_score": tie_state.get("game_score"),
        })
        self._check_daily_loss()
        if self.circuit_breaker:
            self.last_error = f"风控熔断：{self.circuit_breaker}"
            return None
        blockers = self.blockers()
        if blockers:
            self.last_error = "自动实盘未就绪：" + "、".join(blockers)
            return None
        if not self.client_ready or self.client is None:
            self.last_error = "实盘客户端未初始化"
            return None
        if sport not in self.allowed_sports:
            self.last_error = f"市场 {sport} 未允许实盘"
            return None
        rules = self.config.sport_rules.get(sport)
        if not rules or not rules.enabled:
            self.last_error = "市场 TENNIS 风控未启用"
            return None
        if self.sport_circuit_breakers.get(sport):
            self.last_error = f"市场 {sport} 熔断：{self.sport_circuit_breakers[sport]}"
            return None
        if book is None:
            try:
                book = await asyncio.to_thread(self.client.get_order_book, token_id=token_id)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"盘口读取失败：{exc}"
                return None

        asks = self._book_entries(book, "asks")
        bids = self._book_entries(book, "bids")
        if not asks:
            self.last_error = "TIE_NO_ASK"
            return None
        best_ask = min(float(ask.price) for ask in asks)
        best_bid = max((float(bid.price) for bid in bids), default=0.0)
        if not (0.0 < best_ask < 1.0):
            self.last_error = f"TIE_INVALID_ASK：{best_ask}"
            return None
        if best_bid > best_ask:
            self.last_error = f"TIE_BOOK_CROSSED：{best_bid:.3f}>{best_ask:.3f}"
            return None
        max_spread = float(getattr(rules, "live_max_book_spread", 0.0) or 0.0)
        if getattr(self, "abnormal_pause_enabled", True) and max_spread > 0 and best_ask - best_bid > max_spread:
            self.last_error = f"TIE_SPREAD_TOO_WIDE：{best_ask - best_bid:.3f}"
            return None
        max_age = float(getattr(rules, "live_max_book_age_seconds", 0.0) or 0.0)
        book_age = self._book_age_seconds(book)
        if getattr(self, "abnormal_pause_enabled", True) and max_age > 0 and (
            book_age is None or book_age > max_age
        ):
            self.last_error = f"TIE_BOOK_TOO_STALE：{book_age}"
            return None
        depth = sum(float(ask.size) for ask in asks if float(ask.price) <= best_ask)
        book_min_size = self._book_min_order_size(book)
        min_notional = max(
            1.0,
            float(getattr(rules, "live_min_order_size_usdc", 0.0) or 0.0),
        )
        order_cap = float(
            getattr(rules, "live_max_order_size_usdc", 0.0) or 0.0
        )
        if self.max_order_size > 0:
            order_cap = (
                min(order_cap, self.max_order_size)
                if order_cap > 0
                else self.max_order_size
            )
        target_amount = max(min_notional, float(order_usdc or 0.0))
        if order_cap > 0:
            target_amount = min(target_amount, order_cap)
        if target_amount + 1e-9 < min_notional:
            self.last_error = (
                f"TIE_ORDER_MIN_NOTIONAL：有效单笔上限 {order_cap:.2f} "
                f"< 交易所最低金额 {min_notional:.2f}"
            )
            return None

        target_shares = math.floor(target_amount / best_ask * 100) / 100
        amount = target_shares * best_ask
        if amount + 1e-9 < min_notional:
            target_shares = math.ceil(min_notional / best_ask * 100) / 100
            amount = target_shares * best_ask
        if order_cap > 0 and amount > order_cap + 1e-9:
            self.last_error = (
                f"TIE_ORDER_CAP_CONFLICT：按最低金额 {min_notional:.2f} "
                f"需要 {target_shares:.2f} 份（${amount:.2f}），"
                f"超过单笔上限 ${order_cap:.2f}"
            )
            return None

        available = math.floor(depth * 100) / 100
        if target_shares + 1e-9 > available:
            self.last_error = (
                f"TIE_DEPTH_TOO_SMALL：目标金额 ${target_amount:.2f} "
                f"需要 {target_shares:.2f} 份，"
                f"可成交 {available:.2f} 份"
            )
            return None
        size = target_shares

        today = trading_day()
        spend_today = sum(
            order.amount
            for order in self.orders.values()
            if order.status != DRY_RUN_STATUS and trading_day(order.created_at) == today
        )
        if self.max_daily_spend > 0 and spend_today + amount > self.max_daily_spend:
            self.last_error = (
                f"TIE_DAILY_SPEND：已投入 {spend_today:.2f} + 本单 {amount:.2f} "
                f"> {self.max_daily_spend:.2f}"
            )
            return None

        tick = self._book_tick_size(book)
        intent = {
            "strategy": "tennis_deciding_tie_low",
            "sport": sport,
            "game_id": game_id,
            "leader_team": selected,
            "underdog_team": opponent,
            "score": tie_state.get("game_score"),
            "set_score": tie_state.get("set_score"),
            "set_index": tie_state.get("set_index"),
            "game_score": tie_state.get("game_score"),
            "tie_event_id": tie_event_id,
            "trigger_at": tie_state.get("trigger_at"),
            "score_change_at": tie_state.get("score_change_at"),
            "quote_started_at": tie_state.get("quote_started_at"),
            "quote_finished_at": tie_state.get("quote_finished_at"),
            "serving": tie_state.get("serving"),
            "shadow_id": shadow_id,
            "market_id": "",
            "condition_id": "",
            "token_id": token_id,
            "side": "BUY",
            "price": best_ask,
            "best_ask": best_ask,
            "best_bid": best_bid,
            "tick_size": tick,
            "fillable_depth": depth,
            "target_shares": target_shares,
            "target_amount_usdc": target_amount,
            "min_notional_usdc": min_notional,
            "book_min_order_size": book_min_size,
            "size": size,
            "amount": amount,
            "max_loss": amount,
            "max_profit": size - amount,
            "reason": "Deciding-set tied score; buy the cheaper side",
        }
        request_payload = {
            "token_id": token_id,
            "price": best_ask,
            "best_ask": best_ask,
            "size": size,
            "side": "BUY",
            "order_type": "GTC",
        }
        self._append_event("PRE_ORDER", intent)
        signal = SimpleNamespace(
            game_id=game_id,
            leader_team=selected,
            leader_name=selected,
            underdog_team=opponent,
            underdog_name=opponent,
        )
        if self.dry_run:
            requested_at = time.time()
            order = await self._dry_run_order(
                signal=signal,
                intent=intent,
                request_payload=request_payload,
                token_id=token_id,
                sport=sport,
                order_price=best_ask,
                size=size,
                amount=amount,
            )
            recorder = getattr(self, "_tie_event_recorder", None)
            if order is not None and recorder is not None:
                recorder.record_order_created(
                    order,
                    request_started_at=requested_at,
                    response_at=time.time(),
                    source="dry_run",
                )
            return order
        requested_at = time.time()
        try:
            response = await self._place_with_retry(
                token_id=token_id,
                order_price=best_ask,
                size=size,
                intent=intent,
            )
        except Exception as exc:  # noqa: BLE001
            reason = self._describe_transport_error(exc)
            self.last_error = f"下单失败：{reason}"
            self._append_event("SUBMIT_FAILED", {**intent, "error": reason})
            return None
        if getattr(response, "ok", True) is False:
            detail = getattr(response, "message", "") or getattr(response, "code", "")
            self.last_error = f"下单被拒绝：{detail}"
            self._append_event("SUBMIT_REJECTED", {
                **intent,
                "result": self._response_to_dict(response),
            })
            return None
        order_id = self._extract_order_id(response)
        if not order_id:
            self.last_error = f"下单返回缺少订单号：{response}"
            return None
        response_payload = self._response_to_dict(response)
        order = LiveOrder(
            order_id=order_id,
            game_id=game_id,
            token_id=token_id,
            team=selected,
            sport=sport,
            limit_price=best_ask,
            size=size,
            amount=amount,
            status="PENDING",
            raw=response_payload,
            intent=intent,
            request=request_payload,
            result=response_payload,
        )
        self.orders[order_id] = order
        recorder = getattr(self, "_tie_event_recorder", None)
        if recorder is not None:
            recorder.record_order_created(
                order,
                request_started_at=requested_at,
                response_at=time.time(),
                source="submit",
            )
        self._save_state()
        self._append_event(
            "ORDER_SUBMITTED",
            intent | {"order_id": order_id, "result": response_payload},
        )
        self._start_order_confirmation(order_id)
        logger.info(
            "TIE LIVE BUY: %s %s %s @ %.3f x %.2f ($%.2f)",
            game_id,
            tie_state.get("game_score"),
            selected,
            best_ask,
            size,
            amount,
        )
        return order

    async def _dry_run_order(
        self,
        *,
        signal,
        intent: dict,
        request_payload: dict,
        token_id: str,
        sport: str,
        order_price: float,
        size: float,
        amount: float,
    ) -> Optional[LiveOrder]:
        """只签名不提交，用于验证「信号 → 下单」链路。"""
        try:
            signed = await asyncio.to_thread(
                self.client.create_limit_order,
                token_id=token_id,
                price=order_price,
                size=size,
                side="BUY",
            )
        except Exception as exc:
            self.last_error = f"干跑签名失败：{exc}"
            self._append_event("DRY_RUN_FAILED", {**intent, "error": str(exc)})
            return None

        maker = getattr(signed, "maker", None)
        signer = getattr(signed, "signer", None)
        signature_type = getattr(signed, "signature_type", None)
        payload = {
            "dry_run": True,
            "maker": maker,
            "signer": signer,
            "signature_type": signature_type,
            "price": order_price,
            "size": size,
            "amount": amount,
        }
        order_id = f"DRYRUN_{int(time.time() * 1000)}"
        order = LiveOrder(
            order_id=order_id,
            game_id=getattr(signal, "game_id", ""),
            token_id=token_id,
            team=getattr(signal, "leader_team", None) or getattr(signal, "leader_name", ""),
            sport=sport,
            limit_price=order_price,
            size=size,
            amount=amount,
            status=DRY_RUN_STATUS,
            raw=payload,
            intent=intent,
            request=request_payload,
            result=payload,
            drill=True,
        )
        self.orders[order_id] = order
        self._append_event(
            "DRY_RUN_ORDER",
            intent | {
                "drill": True,
                "order_id": order_id,
                "maker": maker,
                "signer": signer,
                "signature_type": signature_type,
            },
        )
        logger.info(
            "LIVE DRY-RUN: %s %s price=%.3f size=%.2f maker=%s signer=%s sig_type=%s",
            sport, order.team, order_price, size, maker, signer, signature_type,
        )
        return order

    def _extract_order_id(self, response: object) -> str:
        if isinstance(response, str):
            try:
                response = json.loads(response)
            except Exception:
                return response
        if isinstance(response, dict):
            for key in ("orderID", "orderId", "order_id", "id"):
                if response.get(key):
                    return str(response[key])
            return ""
        for key in ("order_id", "orderID", "orderId", "id"):
            value = getattr(response, key, None)
            if value:
                return str(value)
        return ""

    @staticmethod
    def _json_safe(value: object) -> object:
        """把 SDK 返回的 Decimal / datetime 等类型转成可 JSON 序列化的值。"""
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, dict):
            return {str(key): LiveTrader._json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [LiveTrader._json_safe(item) for item in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    @staticmethod
    def _response_to_dict(response: object) -> dict:
        if isinstance(response, dict):
            return response
        for attr in ("model_dump", "dict"):
            method = getattr(response, attr, None)
            if callable(method):
                try:
                    data = method()
                except Exception:
                    continue
                if isinstance(data, dict):
                    return LiveTrader._json_safe(data)
        return {"response": str(response)}

    @staticmethod
    def _remote_field(row: object, *names: str):
        for name in names:
            if isinstance(row, dict):
                value = row.get(name)
            else:
                value = getattr(row, name, None)
            if value is not None:
                return value
        return None

    async def cancel_order(self, order_id: str) -> bool:
        if self.client is None or not self.client_ready:
            return False
        last_error: Optional[Exception] = None
        for attempt in range(2):
            try:
                await asyncio.to_thread(self.client.cancel_order, order_id=order_id)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    await asyncio.sleep(0.5)
        if last_error:
            self.last_error = f"撤单失败：{last_error}"
            return False

        if order_id in self.orders:
            order = self.orders[order_id]
            order.status = "CANCELLED"
            order.updated_at = time.time()
            self._record_tie_order_update(order, "cancel", order.updated_at)
            released = order.filled_size <= 0
            if released:
                self._release_dedupe(order.token_id)
            self._append_event("ORDER_CANCELLED", {
                "order_id": order_id,
                "reason": "manual_or_timeout",
                "filled_size": order.filled_size,
                "dedupe_released": released,
            })
        self._save_state()
        return True

    async def cancel_all(self) -> int:
        if self.client is None or not self.client_ready:
            return 0
        try:
            await asyncio.to_thread(self.client.cancel_all)
            count = 0
            for order in self.orders.values():
                if order.status in ACTIVE_STATUSES:
                    order.status = "CANCELLED"
                    order.updated_at = time.time()
                    self._record_tie_order_update(order, "cancel_all", order.updated_at)
                    released = order.filled_size <= 0
                    if released:
                        self._release_dedupe(order.token_id)
                    self._append_event("ORDER_CANCELLED", {
                        "order_id": order.order_id,
                        "reason": "global_stop",
                        "filled_size": order.filled_size,
                        "dedupe_released": released,
                    })
                    count += 1
            self._save_state()
            return count
        except Exception as exc:
            self.last_error = f"批量撤单失败：{exc}"
            return 0

    async def _monitor_loop(self):
        while not self._stop_event.is_set():
            if not self.client_ready:
                try:
                    await self._try_rebuild_client()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("重建实盘客户端异常：%s", exc)
            try:
                await self._refresh_orders()
            except Exception as exc:
                self.last_error = str(exc)
            try:
                await self._maybe_check_api_health()
            except Exception as exc:  # noqa: BLE001 - 探活本身不能影响主流程
                logger.debug("API 探活异常：%s", exc)
            await asyncio.sleep(5)

    async def _try_rebuild_client(self) -> None:
        """客户端没建起来时定期重建：网络抖动恢复后自动接上，不用手动重启 bot。

        2026-09-18 的情形：重启那一刻代理正好抖了 → 客户端初始化失败 → bot 带着
        "未 arm + 无实盘客户端"一直跑着，而网络几分钟后就恢复了，但没人再试过。
        """
        interval = max(15.0, float(getattr(self.config, "live_client_retry_seconds", 60) or 60))
        now = time.time()
        if now - self._client_rebuild_at < interval:
            return
        self._client_rebuild_at = now
        logger.info("实盘客户端未就绪，尝试重建（上次失败：%s）", self.client_error or "未知")
        await asyncio.to_thread(self._build_client)
        if self.client_ready:
            logger.warning("实盘客户端已重建成功")
            self._append_event("CLIENT_REBUILT", {
                "wallet_type": self.wallet_type, "signer_type": self.signer_type,
            })
            self.clear_stale_error("实盘客户端已重建")
            await self._maybe_auto_rearm()

    def _live_mode_requested(self) -> bool:
        """面板持久化的模式是不是"实盘"（risk_settings.json 里的 mode）。"""
        path = getattr(self.config, "risk_settings_path", "")
        if not path:
            return False
        try:
            with open(path, "r", encoding="utf-8") as file:
                return str((json.load(file) or {}).get("mode") or "").lower() == "live"
        except Exception:  # noqa: BLE001 - 读不到就当没要求实盘
            return False

    async def _maybe_auto_rearm(self) -> None:
        """客户端恢复 + 面板模式仍是实盘 → 自动重新 arm。

        只在"本来就该在交易"的情况下恢复：开关全开、没有熔断、没有全局停止、
        持久化模式是 live。用户手动切到观察模式（mode != live）时不会自动 arm。
        """
        if self.armed or self.globally_stopped or self.circuit_breaker:
            return
        if not (self.enabled and self.global_enabled and self.unattended_enabled):
            return
        if not self._live_mode_requested():
            return
        self.arm()
        logger.warning("客户端恢复且面板模式为实盘 → 自动重新 arm（恢复交易）")
        self._append_event("AUTO_REARM", {"reason": "client_recovered"})

    # ---------- API 可达性探活（2026-09-18）----------

    def api_health(self) -> dict:
        """三条链路的最近一次探活结果，供面板显示。"""
        return dict(self._api_health)

    async def _maybe_check_api_health(self, force: bool = False) -> None:
        interval = max(15.0, float(getattr(self.config, "api_health_interval_seconds", 60) or 60))
        now = time.time()
        if not force and now - self._api_health_checked_at < interval:
            return
        self._api_health_checked_at = now
        checks = {
            # 下单走的就是这条（官方 SDK / httpx）；失败时下单必失败
            "client": self._probe_client,
            # 读盘口的兜底通道（aiohttp + 跳过证书校验）
            "rest": self._probe_clob_rest,
            # 市场数据（预匹配 / 结算回填）
            "gamma": self._probe_gamma,
        }
        for name, probe in checks.items():
            started = time.perf_counter()
            ok = False
            error = ""
            try:
                ok = await probe()
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"[:160]
            latency_ms = round((time.perf_counter() - started) * 1000)
            previous = self._api_health.get(name) or {}
            failures = 0 if ok else int(previous.get("consecutive_failures") or 0) + 1
            self._api_health[name] = {
                "ok": ok,
                "latency_ms": latency_ms,
                "checked_at": now,
                "last_ok_at": now if ok else previous.get("last_ok_at"),
                "consecutive_failures": failures,
                "error": "" if ok else (error or previous.get("error") or "不可达"),
            }
            if not ok:
                logger.warning("Polymarket API 探活失败[%s]（连续 %d 次）：%s", name, failures, error)

    async def _probe_client(self) -> bool:
        """实盘客户端通道：查一次自己的挂单（认证 + 同一套传输层，最贴近下单）。

        2026-09-20：**加一次重试**。前一晚 01:12~01:29 连续报了 5 次
        `TransportError: The read operation timed out`，每次都是"连续 1 次"就恢复，
        而同期**一笔下单失败都没有**（实盘事件全是 ORDER_SUBMITTED / FILLED）。
        也就是说那是**探活自身的误报**：一次瞬时读超时就把面板的"实盘下单"灯打成 ❌，
        结果是真故障会被这种噪声淹没（假警报多了，下次就没人当真了）。
        重试一次能把绝大多数瞬时抖动吃掉，又不会明显拖慢真故障的发现。
        """
        if self.client is None or not self.client_ready:
            return False
        for attempt in (1, 2):
            try:
                orders = await asyncio.to_thread(
                    lambda: list(self.client.list_open_orders().iter_items())
                )
                return isinstance(orders, list)
            except Exception:  # noqa: BLE001
                if attempt >= 2:
                    raise
                logger.info("实盘下单通道探活失败，1 秒后重试一次（通常是一次瞬时超时）")
                await asyncio.sleep(1.0)
        return False

    async def _probe_clob_rest(self) -> bool:
        """公共 REST 兜底通道：能拿到任何 HTTP 响应（哪怕是 404）就算通。"""
        import aiohttp

        url = f"{self.config.polymarket_clob_url}/book"
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
            async with session.get(
                url, params={"token_id": "1"},
                proxy=getattr(self.config, "proxy_url", None), timeout=timeout,
            ) as resp:
                return resp.status < 500

    async def _probe_gamma(self) -> bool:
        import aiohttp

        url = f"{self.config.polymarket_gamma_url}/markets"
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
            async with session.get(
                url, params={"limit": "1"},
                proxy=getattr(self.config, "proxy_url", None), timeout=timeout,
            ) as resp:
                return resp.status == 200

    async def _read_with_retry(self, func, *args):
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                return await asyncio.to_thread(func, *args)
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
        raise last_error

    # ---------- 下单重试：传输层失败不再直接放弃（2026-09-18）----------
    #
    # 背景：2026-09-18 00:16 起代理到 *.polymarket.com 的 TLS 通道抖了 12 小时，
    # 19 次下单尝试全部报 `Request failed`（官方 SDK 把 httpx 传输错误统一包成
    # `TransportError`，见 polymarket/clients/_transport.py）。当时是"失败即放弃"，
    # 于是整段行情一分钱没赚到（那 19 笔在模拟盘里是 18 胜 1 负）。
    # 现在：传输层失败自动重试（默认 3 次，1.5s / 4s 退避），
    # **且每次重试前先查一次交易所**——万一上一笔其实已经成交、只是响应丢在半路，
    # 就按成功处理，绝不补第二单。

    @staticmethod
    def _is_transport_error(exc: BaseException) -> bool:
        """是不是"请求根本没送到服务器"那一类错误（这类重试是安全的）。

        业务性错误（余额不足、价格不接受、签名问题）不会被 SDK 包成 TransportError，
        也就不会被重试。
        """
        name = type(exc).__name__
        if name in {
            "TransportError", "ConnectError", "ConnectTimeout", "ReadTimeout",
            "RemoteProtocolError", "ReadError", "WriteError", "ProxyError", "SSLError",
            "PoolTimeout", "TimeoutException",
        }:
            return True
        try:
            import httpx

            if isinstance(exc, httpx.HTTPError):
                return True
        except ImportError:  # pragma: no cover - httpx 一定有（SDK 依赖）
            pass
        text = str(exc).lower()
        return any(token in text for token in ("request failed", "ssl", "timed out", "connection"))

    def _existing_order_for_token(self, token_id: str) -> str:
        """交易所那边这张盘现在有没有我们的挂单/成交（同步实现，用 to_thread 调用）。"""
        if self.client is None:
            return ""
        try:
            for row in self.client.list_open_orders(token_id=token_id).iter_items():
                order_id = str(self._remote_field(row, "id", "orderID", "order_id") or "")
                if order_id:
                    return order_id
        except Exception:  # noqa: BLE001 - 查不到就当没有，不能因此阻断重试
            pass
        try:
            trades = self.client.list_account_trades(token_id=token_id)
            iterator = trades.iter_items() if hasattr(trades, "iter_items") else iter(trades)
            first = next(iter(iterator), None)
            if first is not None:
                order_id = str(
                    self._remote_field(first, "taker_order_id", "order_id", "id") or ""
                )
                if order_id:
                    return order_id
        except Exception:  # noqa: BLE001
            pass
        return ""

    async def _place_with_retry(self, *, token_id: str, order_price, size, intent: dict):
        """提交限价单；传输层失败自动重试，重试前先确认上一笔是否其实已经成功。"""
        attempts = max(1, int(getattr(self.config, "live_submit_retries", 3) or 3))
        delays = list(getattr(self.config, "live_submit_retry_delays", (1.5, 4.0)) or (1.5, 4.0))
        for attempt in range(1, attempts + 1):
            try:
                return await asyncio.to_thread(
                    self.client.place_limit_order,
                    token_id=token_id,
                    price=order_price,
                    size=size,
                    side="BUY",
                )
            except Exception as exc:  # noqa: BLE001
                if not self._is_transport_error(exc) or attempt >= attempts:
                    raise
                existing = await asyncio.to_thread(self._existing_order_for_token, token_id)
                if existing:
                    reason = self._describe_transport_error(exc)
                    logger.warning(
                        "下单报传输错误，但交易所其实已有挂单 %s —— 按成功处理，不再补单", existing
                    )
                    self._append_event("SUBMIT_RECOVERED", {
                        **intent, "attempt": attempt, "order_id": existing, "error": reason,
                    })
                    return {"orderID": existing, "recovered": True}
                delay = float(delays[min(attempt - 1, len(delays) - 1)])
                reason = self._describe_transport_error(exc)
                logger.warning(
                    "下单传输失败（第 %d/%d 次），%.1fs 后重试：%s",
                    attempt, attempts, delay, reason,
                )
                self._append_event("SUBMIT_RETRY", {
                    **intent, "attempt": attempt, "error": reason, "delay_s": delay,
                })
                await asyncio.sleep(delay)
        raise RuntimeError("unreachable")  # pragma: no cover

    async def _refresh_orders(self):
        active = [order for order in self.orders.values() if order.status in ACTIVE_STATUSES]
        if not active or self.client is None:
            return
        try:
            remote_orders = await self._read_with_retry(
                lambda: list(self.client.list_open_orders().iter_items())
            )
        except Exception as exc:
            self.last_error = f"订单状态读取失败：{exc}"
            return
        active_ids = {order.order_id for order in active}
        by_id = {}
        for row in remote_orders:
            remote_id = str(self._remote_field(row, "id", "orderID", "order_id") or "")
            if remote_id in active_ids:
                by_id[remote_id] = row
        changed = False
        now = time.time()
        for order in active:
            order_rules = self.config.sport_rules.get(order.sport)
            order_timeout = order_rules.live_order_timeout_seconds if order_rules else self.order_timeout
            if order_timeout > 0 and now - order.created_at >= order_timeout:
                await self.cancel_order(order.order_id)
                changed = True
                continue
            remote = by_id.get(order.order_id)
            if not remote:
                # 不在挂单簿里 = 已成交 / 已撤。**先按成交明细对账** ——
                # `get_order` 对已平订单返回 null，SDK 会抛
                # "OpenOrder response did not match expected shape"（2026-10-28 用户报错）。
                if await asyncio.to_thread(self._reconcile_from_trades, order, now):
                    changed = True
                    continue
                try:
                    remote = await self._read_with_retry(
                        lambda: self.client.get_order(order_id=order.order_id)
                    )
                except Exception:
                    # 挂单簿没有、成交明细也没有 → 视为已撤销/过期。给成交明细 20 秒的落库时间。
                    if now - order.created_at >= 20:
                        order.status = "CANCELLED"
                        order.updated_at = now
                        self._append_event("ORDER_GONE", {
                            "order_id": order.order_id, "at": now,
                        })
                        changed = True
                    continue
            if self._apply_remote_order(order, remote, now):
                changed = True
        if changed:
            self._save_state()

    def _apply_remote_order(self, order: LiveOrder, remote: object, now: float) -> bool:
        """把交易所返回的订单状态写回本地订单；有变化返回 True。"""
        status = str(self._remote_field(remote, "status") or "").upper()
        if status in {"LIVE", "OPEN"}:
            new_status = "OPEN"
        elif status in {"MATCHED", "FILLED"}:
            new_status = "FILLED"
        elif status in {"CANCELLED", "CANCELED"}:
            new_status = "CANCELLED"
        elif status in {"REJECTED"}:
            new_status = "REJECTED"
        else:
            new_status = status
        filled = self._float_value(self._remote_field(remote, "size_matched", "sizeMatched"))
        price = self._float_value(self._remote_field(remote, "price"))
        if filled == order.filled_size and new_status == order.status:
            return False
        order.status = new_status
        order.filled_size = filled
        order.average_price = price or order.limit_price
        order.updated_at = now
        self._sync_position(order)
        self._append_event("ORDER_UPDATE", {
            "order_id": order.order_id,
            "status": order.status,
            "filled_size": order.filled_size,
            "average_price": order.average_price,
        })
        self._record_tie_order_update(order, "remote_order", now)
        return True

    # ---------- 成交明细对账（2026-10-28：get_order 对已成交订单会返回 null）----------

    def _trades_for_order(self, order: LiveOrder) -> list:
        """按 token 拉取成交明细，再挑出属于这张委托的那些。"""
        if self.client is None:
            return []
        try:
            page = self.client.list_account_trades(token_id=order.token_id)
            rows = list(page.iter_items()) if hasattr(page, "iter_items") else list(page)
        except Exception as exc:  # noqa: BLE001 - 读不到就交给下一轮，不当真错误
            logger.debug("成交明细读取失败 %s: %s", str(order.order_id)[:12], exc)
            return []
        return [row for row in rows if trade_belongs_to_order(row, order.order_id)]

    def _reconcile_from_trades(self, order: LiveOrder, now: float) -> bool:
        """用成交明细把订单标成 FILLED 并写入**真实成交均价**；无成交返回 False。"""
        summary = summarize_fill(self._trades_for_order(order), order.order_id)
        if summary is None:
            return False
        size, avg = summary
        if (abs(size - order.filled_size) < 1e-9
                and order.status == "FILLED"
                and abs(avg - float(order.average_price or 0)) < 1e-9):
            return False
        order.status = "FILLED"
        order.filled_size = round(size, 6)
        order.average_price = round(avg, 6)
        order.updated_at = now
        self._sync_position(order)
        self._append_event("ORDER_RECONCILED_FROM_TRADES", {
            "order_id": order.order_id,
            "filled_size": order.filled_size,
            "average_price": order.average_price,
            "at": now,
        })
        self._record_tie_order_update(order, "account_trades", now)
        logger.info(
            "订单 %s 已按成交明细对账：%s 股 @ %.4f（get_order 对已成交订单返回 null）",
            str(order.order_id)[:12], order.filled_size, order.average_price,
        )
        return True

    # ---------- 成交确认：下单后短轮询（2026-09-18）----------

    def _start_order_confirmation(self, order_id: str) -> None:
        """下单成功后尽快确认状态变化（成交/挂上/被撤），不再等 5 秒的全局轮询。"""
        if not order_id or self.client is None or not self.client_ready:
            return
        attempts = int(getattr(self.config, "live_confirm_poll_attempts", 10) or 0)
        if attempts <= 0:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:      # 没有事件循环（同步上下文）→ 交给 5 秒全局轮询
            return
        task = loop.create_task(self._confirm_order_soon(order_id, attempts))
        self._confirm_tasks.add(task)
        task.add_done_callback(self._confirm_tasks.discard)

    async def _confirm_order_soon(self, order_id: str, attempts: int) -> None:
        interval = max(0.1, float(getattr(self.config, "live_confirm_poll_interval_seconds", 0.5) or 0.5))
        for _ in range(max(1, attempts)):
            await asyncio.sleep(interval)
            order = self.orders.get(order_id)
            if order is None or order.status not in ACTIVE_STATUSES:
                return
            try:
                remote = await asyncio.to_thread(self.client.get_order, order_id=order_id)
            except Exception:  # noqa: BLE001 - 已成交订单读不到 → 改用成交明细对账
                if await asyncio.to_thread(self._reconcile_from_trades, order, time.time()):
                    self._save_state()
                    return
                continue
            if self._apply_remote_order(order, remote, time.time()):
                self._save_state()
            if order.status not in ACTIVE_STATUSES:
                logger.info(
                    "订单 %s 已确认：%s（下单后 %.1fs，短轮询）",
                    order_id[:12], order.status, time.time() - order.created_at,
                )
                return

    def _sync_position(self, order: LiveOrder):
        if order.filled_size <= 0:
            return
        position_id = f"LP_{order.order_id}"
        if position_id not in self.positions:
            self.positions[position_id] = LivePosition(
                position_id=position_id,
                order_id=order.order_id,
                game_id=order.game_id,
                token_id=order.token_id,
                team=order.team,
                size=order.filled_size,
                entry_price=order.average_price or order.limit_price,
                cost=order.filled_size * (order.average_price or order.limit_price),
                # 原来漏了这行 → 每一笔实盘仓位的「运动」都是空的，
                # 面板「实盘持仓与结算」那一列全是 '-'（2026-09-16 用户发现）。
                # 订单上一直有 sport，只是没传进仓位。
                sport=order.sport,
                strategy=str((order.intent or {}).get("strategy") or ""),
            )
        else:
            position = self.positions[position_id]
            position.size = order.filled_size
            position.entry_price = order.average_price or order.limit_price
            position.cost = position.size * position.entry_price

    def _open_positions(self) -> list[LivePosition]:
        return [p for p in self.positions.values() if p.status == "OPEN"]

    def settle_game(
        self,
        game_id: str,
        home_team: str,
        away_team: str,
        home_score: int,
        away_score: int,
        *,
        allow_draw: bool = False,
    ):
        """按最终比分结算；allow_draw=True 时平局两边都算输（足球用）。"""
        draw = bool(allow_draw) and home_score == away_score
        winner = "" if draw else (home_team if home_score > away_score else away_team)
        for position in self._open_positions():
            if position.game_id != game_id:
                continue
            position.status = "CLOSED_WIN" if position.team == winner else "CLOSED_LOSS"
            position.exit_price = 1.0 if position.team == winner else 0.0
            position.pnl = position.size * position.exit_price - position.cost
            position.closed_at = time.time()
            self._append_event("POSITION_SETTLED", asdict(position))
        self._save_state()
        self._check_daily_loss(cancel_open_orders=True)

    # ---------- 交易所对账 ----------

    def _adopt_exchange_fill(self, position, info: dict) -> bool:
        """用交易所的真实成交均价回填入场价（PROJECT_DESIGN #138 / #120）。"""
        avg = self._float_value(info.get("avg_price", info.get("avgPrice")))
        if apply_exchange_fill_price(position, {"avg_price": avg}):
            self._append_event("FILL_PRICE_FIXED", {
                "position_id": position.position_id,
                "entry_price": position.entry_price,
                "cost": position.cost,
                "pnl": position.pnl,
            })
            return True
        return False

    def _backfill_fill_prices(self, open_positions: list[dict],
                              closed_positions: list[dict]) -> int:
        """把「委托限价」换成交易所真实成交均价（每 60 秒随账户快照跑一次）。

        为什么需要（2026-10-26 用户："1.10 买 0.98，收益应该是 0.022，实盘只有 0.02"）
        -------------------------------------------------------------------------
        `_apply_remote_order` 里 `average_price = price or limit_price`，而 `price`
        是**委托限价**，不是成交明细。于是每笔的 `entry_price`/`cost`/`pnl` 都按限价算：

            Team Nemesis Academy（10-26）：委托 0.98、真实成交 **0.9699**
            本地 pnl 0.0224（=1.12×(1−0.98)），真实毛利 **0.0337**，低估 33%。

        交易所的 `/positions` 与 `/closed-positions` 都带 `avgPrice`（真实成交均价），
        这里按 token 匹配后回填。已结算且结算价是干净的 0/1 时，顺带重算 pnl。
        """
        by_asset: dict[str, dict] = {}
        for item in closed_positions or []:
            asset = str(item.get("asset_id") or "")
            if asset:
                by_asset.setdefault(asset, item)
        for item in open_positions or []:
            asset = str(item.get("asset_id") or "")
            if asset:
                by_asset[asset] = item
        if not by_asset:
            return 0
        fixed = 0
        for position in self.positions.values():
            # Repeated trigger positions can share one token. The exchange
            # position is aggregated, so its avgPrice cannot be copied to every
            # local slice; these orders already reconcile their own fills by
            # taker_order_id in _reconcile_from_trades().
            if str(getattr(position, "strategy", "") or "") == "tennis_deciding_tie_low":
                continue
            info = by_asset.get(str(position.token_id or ""))
            if info is None:
                continue
            if self._adopt_exchange_fill(position, info):
                fixed += 1
        if fixed:
            self._save_state()
            logger.info("成交价回填：%d 笔按交易所均价修正（限价≠成交价）", fixed)
        return fixed

    def reconcile_exchange(
        self,
        open_positions: list[dict],
        closed_positions: list[dict],
    ) -> dict:
        """用交易所只读数据校准本地持仓记录。

        本地持仓靠比赛结算驱动；一旦某场比赛的消息被漏掉（换源、抓取失败、
        比赛不在订阅范围），本地就会留下一条永远不会关闭的 OPEN 记录，
        而持仓上限是按本地 OPEN 数计算的 —— 结果就是「信号一直触发、实盘
        一直不下单」。这里用交易所的持仓/已结算数据把这类记录修正掉。

        只在能拿到可信数据时才动作：
          * 出现在交易所「已结算」里 → 按交易所的实际盈亏在本地结算；
          * 两边都查不到、且开仓已超过 live_reconcile_orphan_hours
            → 标记为 CLOSED_UNKNOWN 释放额度（不臆造盈亏）；
          * 交易所返回空数据 → 直接跳过，避免误杀。
        """
        # 对账只用只读数据做账务修正，paper 模式下也照常生效
        if not getattr(self.config, "live_reconcile_enabled", True):
            return {"skipped": "功能已关闭"}
        # 先回填真实成交价（与是否还有 OPEN 仓位无关 —— 已结算的也要修正）
        self._backfill_fill_prices(open_positions, closed_positions)
        local_open = self._open_positions()
        if not local_open:
            return {"settled": [], "released": [], "checked": 0}
        local_cost_by_asset: dict[str, float] = {}
        for position in local_open:
            asset = str(position.token_id)
            local_cost_by_asset[asset] = (
                local_cost_by_asset.get(asset, 0.0) + float(position.cost or 0.0)
            )

        open_by_asset: dict[str, dict] = {}
        for item in open_positions or []:
            asset = str(item.get("asset_id") or "")
            if asset:
                open_by_asset[asset] = item
        closed_by_asset: dict[str, dict] = {}
        for item in closed_positions or []:
            asset = str(item.get("asset_id") or "")
            if asset:
                closed_by_asset.setdefault(asset, item)

        if not open_by_asset and not closed_by_asset:
            # 交易所一条记录都没有，但本地有未结算仓位：极可能是查询异常，
            # 宁可不动作，也不要把真实持仓释放掉。
            logger.warning(
                "交易所返回空持仓，跳过对账（本地未结算 %d 笔）", len(local_open)
            )
            return {"skipped": "交易所返回空数据", "checked": 0}

        orphan_seconds = max(
            60.0,
            float(getattr(self.config, "live_reconcile_orphan_hours", 12.0) or 12.0) * 3600.0,
        )
        now = time.time()
        settled: list[dict] = []
        released: list[dict] = []
        for position in local_open:
            asset = str(position.token_id)
            matched = closed_by_asset.get(asset)
            if matched is not None:
                exchange_pnl = self._float_value(
                    matched.get("pnl", matched.get("realized_pnl"))
                )
                total_cost = local_cost_by_asset.get(asset, 0.0)
                pnl = (
                    exchange_pnl * float(position.cost or 0.0) / total_cost
                    if total_cost > 0
                    else exchange_pnl
                )
                size = position.size if position.size else 0.0
                position.pnl = round(pnl, 6)
                position.status = (
                    "CLOSED_WIN" if pnl > 0 else ("CLOSED_LOSS" if pnl < 0 else "CLOSED_SETTLED")
                )
                position.exit_price = (
                    max(0.0, min(1.0, (position.cost + pnl) / size)) if size > 0 else None
                )
                position.closed_at = now
                self._append_event(
                    "POSITION_RECONCILED",
                    {
                        "position_id": position.position_id,
                        "reason": "交易所已结算",
                        "exchange_title": matched.get("title"),
                        "pnl": position.pnl,
                        "at": now,
                    },
                )
                settled.append(
                    {"position_id": position.position_id, "team": position.team, "pnl": position.pnl}
                )
                logger.info(
                    "对账结算 %s %s：交易所已结算，盈亏 %.4f",
                    position.position_id, position.team, position.pnl,
                )
                continue
            if asset in open_by_asset:
                info = open_by_asset[asset]
                # 交易所把「已结算但没人赎回」的 token 一直留在 /positions 里
                # （输的仓位现价 0、redeemable=True，赢的也可能等赎回）。
                # 这种记录如果本地还标着 OPEN，会一直占着持仓额度 ——
                # 所以这里把"已结算"的两种信号也算作可结算：
                #   1) redeemable=True（市场已可赎回 = 已结算）
                #   2) 现价≈0 且市值≈0（该 outcome 已归零）
                # 注意必须两个价格字段都存在才判定，避免把缺字段的返回值误当成 0。
                has_price = info.get("current_price") is not None and info.get("value") is not None
                resolved = bool(info.get("redeemable")) or (
                    has_price
                    and float(info.get("current_price") or 0) <= 0.0001
                    and float(info.get("value") or 0) <= 0.01
                )
                if not resolved:
                    continue
                exchange_pnl = self._float_value(info.get("pnl", info.get("cash_pnl")))
                total_cost = local_cost_by_asset.get(asset, 0.0)
                pnl = (
                    exchange_pnl * float(position.cost or 0.0) / total_cost
                    if total_cost > 0
                    else exchange_pnl
                )
                size = position.size if position.size else 0.0
                position.pnl = round(pnl, 6)
                position.status = (
                    "CLOSED_WIN" if pnl > 0 else ("CLOSED_LOSS" if pnl < 0 else "CLOSED_SETTLED")
                )
                position.exit_price = (
                    max(0.0, min(1.0, (position.cost + pnl) / size)) if size > 0 else None
                )
                position.closed_at = now
                self._append_event(
                    "POSITION_RECONCILED",
                    {
                        "position_id": position.position_id,
                        "reason": "交易所已结算（token 已归零或可赎回）",
                        "exchange_value": info.get("value"),
                        "exchange_price": info.get("current_price"),
                        "redeemable": bool(info.get("redeemable")),
                        "pnl": position.pnl,
                        "at": now,
                    },
                )
                settled.append(
                    {"position_id": position.position_id, "team": position.team, "pnl": position.pnl}
                )
                logger.info(
                    "对账结算 %s %s：交易所 token 已归零/可赎回，本地补结算 盈亏 %.4f",
                    position.position_id, position.team, position.pnl,
                )
                continue
            if now - float(position.opened_at or 0) < orphan_seconds:
                continue
            position.status = "CLOSED_UNKNOWN"
            position.pnl = None
            position.closed_at = now
            self._append_event(
                "POSITION_RECONCILED",
                {
                    "position_id": position.position_id,
                    "reason": "交易所无该持仓且无结算记录，释放持仓额度",
                    "opened_at": position.opened_at,
                    "at": now,
                },
            )
            released.append(
                {"position_id": position.position_id, "team": position.team}
            )
            logger.warning(
                "对账释放 %s %s：交易所持仓与结算记录里都找不到，标记为 CLOSED_UNKNOWN",
                position.position_id, position.team,
            )

        if settled or released:
            self._save_state()
            self._check_daily_loss(cancel_open_orders=True)
        return {
            "settled": settled,
            "released": released,
            "checked": len(local_open),
            "at": now,
        }

    def _closed_positions(self) -> list[LivePosition]:
        return [p for p in self.positions.values() if p.status.startswith("CLOSED")]

    def _settled_positions(self) -> list[LivePosition]:
        """真正有结算结果的仓位；CLOSED_UNKNOWN（交易所查不到）不计入盈亏与战绩。"""
        return [
            p for p in self.positions.values()
            if p.status in {"CLOSED_WIN", "CLOSED_LOSS", "CLOSED_SETTLED"}
        ]

    def _realized_pnl(self) -> float:
        return sum(p.pnl or 0.0 for p in self._settled_positions())

    def stats_by_sport(self) -> dict[str, dict]:
        """按运动统计**已结算**的实盘仓位（全量）：笔数 / 胜场 / 盈亏 / 成本 / ROI。

        2026-09-20：面板的"分运动统计"要加上实盘。这个方法必须在服务端算，
        因为下面 summary() 里的 `positions` 字段为了控制 payload 只给最后 30 条 ——
        面板若自己聚合那 30 条，会显示成"最近 30 笔"，数字会骗人。

        2026-09-20 追加 ROI（已实现盈亏 / 已投入成本，与 summary 的 `realized_roi`
        同口径）。成本取每笔实际的 `cost`，因为实单单笔金额一路在涨（$1 → $2 → $10），
        "笔数 × 当前单笔"算出来的分母会让早期交易失真。没有成本的运动返回 roi=None。
        """
        out: dict[str, dict] = {}
        for p in self._settled_positions():
            key = str(getattr(p, "sport", "") or "") or "Unknown"
            item = out.setdefault(key, {"trades": 0, "wins": 0, "pnl": 0.0, "cost": 0.0})
            pnl = float(p.pnl or 0.0)
            item["trades"] += 1
            if pnl > 0:
                item["wins"] += 1
            item["pnl"] = round(item["pnl"] + pnl, 4)
            item["cost"] = round(item["cost"] + float(p.cost or 0.0), 4)
        for item in out.values():
            item["roi"] = round(item["pnl"] / item["cost"], 4) if item["cost"] > 0 else None
        return out

    def stats_by_strategy(self, strategy: str) -> dict:
        """已结算实盘统计，按 intent.strategy 归集。"""
        key = str(strategy or "")
        rows = [
            p for p in self._settled_positions()
            if str(getattr(p, "strategy", "") or "") == key
        ]
        wins = sum(1 for p in rows if (p.pnl or 0.0) > 0)
        pnl = sum(float(p.pnl or 0.0) for p in rows)
        cost = sum(float(p.cost or 0.0) for p in rows)
        return {
            "trades": len(rows),
            "wins": wins,
            "losses": len(rows) - wins,
            "win_rate": round(wins / len(rows), 6) if rows else None,
            "pnl": round(pnl, 6),
            "cost": round(cost, 6),
            "roi": round(pnl / cost, 6) if cost > 0 else None,
            "open": sum(
                1 for p in self._open_positions()
                if str(getattr(p, "strategy", "") or "") == key
            ),
        }

    def summary(self) -> dict:
        closed = self._settled_positions()
        unknown = [p for p in self.positions.values() if p.status == "CLOSED_UNKNOWN"]
        wins = sum(1 for p in closed if (p.pnl or 0.0) > 0)
        losses = sum(1 for p in closed if (p.pnl or 0.0) < 0)
        settled = wins + losses
        realized = sum(p.pnl or 0.0 for p in closed)
        cost = sum(p.cost for p in closed)
        spent_today = self.daily_spent_usdc()
        return {
            "audit_version": 10,
            "enabled": self.enabled,
            "client_ready": self.client_ready,
            "client_error": self.client_error,
            "client_error_at": self.client_error_at or None,
            "client_retry_seconds": float(getattr(self.config, "live_client_retry_seconds", 60) or 60),
            "wallet_type": self.wallet_type,
            "signer_type": self.signer_type,
            "dry_run": self.dry_run,
            "drill_mode": self.drill_mode,
            "armed": self.armed,
            "globally_stopped": self.globally_stopped,
            "global_enabled": self.global_enabled,
            "unattended_enabled": self.unattended_enabled,
            "abnormal_paused": self.abnormal_paused,
            "pause_reason": self.pause_reason,
            "abnormal_paused_at": self.abnormal_paused_at,
            "blockers": self.blockers(),
            "api_health": self.api_health(),
            "circuit_breaker": self.circuit_breaker,
            "daily_pnl_usdc": round(self._daily_realized_pnl(), 2),
            "realized_pnl_usdc": round(realized, 2),
            "realized_cost_usdc": round(cost, 2),
            "realized_roi": round(realized / cost, 4) if cost > 0 else 0.0,
            "closed_positions": len(closed),
            "unknown_positions": len(unknown),
            "live_wins": wins,
            "live_losses": losses,
            "live_win_rate": round(wins / settled, 4) if settled else 0.0,
            "max_daily_loss_usdc": self.max_daily_loss,
            "daily_spent_usdc": round(spent_today, 2),
            "daily_spend_remaining_usdc": round(max(0.0, self.max_daily_spend - spent_today), 2),
            "daily_loss_remaining_usdc": round(
                max(0.0, self.max_daily_loss - abs(min(0.0, self._daily_realized_pnl()))), 2
            ),
            "sport_rules": {name: rules.to_dict() for name, rules in self.config.sport_rules.items()},
            "sport_circuit_breakers": dict(self.sport_circuit_breakers),
            "allowed_sports": sorted(self.allowed_sports),
            "max_order_size_usdc": self.max_order_size,
            "max_daily_spend_usdc": self.max_daily_spend,
            "open_orders": len([x for x in self.orders.values() if x.status in ACTIVE_STATUSES]),
            "open_positions": len(self._open_positions()),
            "total_orders": len(self.orders),
            "total_positions": len(self.positions),
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "orders": [self._json_safe(asdict(x)) for x in self.orders.values()][-30:],
            "positions": [self._json_safe(asdict(x)) for x in self.positions.values()][-30:],
            # 全量分运动统计（不受上面 30 条截断影响），给面板的"分运动统计"用
            "sport_stats": self.stats_by_sport(),
            "strategy_stats": {
                "tennis_deciding_tie_low": self.stats_by_strategy(
                    "tennis_deciding_tie_low"
                ),
            },
        }

    def _load_state(self):
        if not self._state_path.exists():
            return
        try:
            with self._state_path.open("r", encoding="utf-8") as file:
                for line in file:
                    row = json.loads(line)
                    if row.get("kind") == "ORDER":
                        order = LiveOrder(**row["data"])
                        self.orders[order.order_id] = order
                        if order.status in ACTIVE_STATUSES or order.filled_size > 0:
                            self.dedupe_keys.add(order.token_id)
                    elif row.get("kind") == "POSITION":
                        position = LivePosition(**row["data"])
                        self.positions[position.position_id] = position
                    elif row.get("kind") == "DEDUPE":
                        token_id = row.get("token_id", "")
                        if token_id:
                            self.dedupe_keys.add(token_id)
                    elif row.get("kind") == "DEDUPE_RELEASE":
                        token_id = row.get("token_id", "")
                        if token_id:
                            self.dedupe_keys.discard(token_id)
        except Exception as exc:
            logger.warning("Failed to load live trader state: %s", exc)
        else:
            self._backfill_position_sports()
            # 启动时压缩一次：这个文件是「每次保存把全部记录再追加一遍」的
            # 快照日志，不压缩会随重启次数无界增长（曾到 433 行 / 455 KB）。
            self.compact_state_file()

    def _backfill_position_sports(self) -> int:
        """给缺 sport 的老仓位回填（从同一个 order_id 的订单上取）。

        历史遗留：`_sync_position_from_order` 原来没把订单的 sport 传进仓位，
        导致 2026-09-16 之前的所有实盘仓位「运动」都是空的（面板显示 '-'）。
        订单侧一直有正确的 sport，所以这里能无损补齐。
        """
        filled = 0
        for position in self.positions.values():
            if position.sport:
                continue
            order = self.orders.get(position.order_id)
            if order is not None and order.sport:
                position.sport = order.sport
                filled += 1
        if filled:
            logger.info("已从订单回填 %d 笔实盘仓位的运动信息", filled)
        return filled

    def _save_state(self):
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            with self._state_path.open("a", encoding="utf-8") as file:
                for order in self.orders.values():
                    file.write(json.dumps(
                        {"kind": "ORDER", "data": self._json_safe(asdict(order))},
                        ensure_ascii=False,
                    ) + "\n")
                for position in self.positions.values():
                    file.write(json.dumps(
                        {"kind": "POSITION", "data": self._json_safe(asdict(position))},
                        ensure_ascii=False,
                    ) + "\n")
        except Exception as exc:
            logger.warning("Failed to save live trader state: %s", exc)
        else:
            self._compact_state_if_needed()

    # ---------- 状态文件压缩 ----------

    def compact_state_file(self) -> int:
        """把追加式快照日志压缩成「每个 id 只保留最后一行」。

        语义等价：ORDER / POSITION 按 id 取最后一行；DEDUPE 与 DEDUPE_RELEASE
        按 token 取最后一次出现的事件（保留原始相对顺序），加载结果不变。
        返回压缩后的行数。
        """
        if not self._state_path.exists():
            return 0
        try:
            last_row: dict[tuple[str, str], dict] = {}
            order_keys: list[tuple[str, str]] = []
            total = 0
            for line in self._state_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                total += 1
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = str(row.get("kind") or "")
                if kind in {"ORDER", "POSITION"}:
                    key = (kind, str((row.get("data") or {}).get(
                        "order_id" if kind == "ORDER" else "position_id", "")))
                elif kind in {"DEDUPE", "DEDUPE_RELEASE"}:
                    key = ("DEDUPE", str(row.get("token_id") or ""))
                else:
                    continue
                if key not in last_row:
                    order_keys.append(key)
                last_row[key] = row
            # DEDUPE 系列只保留每个 token 的最后一次事件，且排在订单/持仓之前
            dedupe_keys = [k for k in order_keys if k[0] == "DEDUPE"]
            rows = [last_row[k] for k in dedupe_keys]
            rows += [last_row[k] for k in order_keys if k[0] != "DEDUPE"]
            if not rows:
                return 0
            if total <= len(rows):
                return len(rows)  # 已经是压缩状态
            text = "".join(
                json.dumps(row, ensure_ascii=False) + "\n" for row in rows
            )
            atomic_write_text(self._state_path, text)
            logger.info("实盘状态文件已压缩：%d 行 → %d 行", total, len(rows))
            return len(rows)
        except Exception as exc:  # noqa: BLE001
            logger.warning("压缩实盘状态文件失败：%s", exc)
            return 0

    def _compact_state_if_needed(self) -> None:
        limit = max(100, int(getattr(self.config, "live_state_compact_lines", 2000) or 2000))
        try:
            if not self._state_path.exists():
                return
            with self._state_path.open("r", encoding="utf-8") as file:
                lines = sum(1 for _ in file)
        except OSError:
            return
        if lines > limit:
            self.compact_state_file()

    @staticmethod
    def _float_value(value: object) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _daily_realized_pnl(self) -> float:
        today = trading_day()
        return sum(
            position.pnl or 0.0
            for position in self.positions.values()
            if position.closed_at and trading_day(position.closed_at) == today
        )

    def daily_spent_usdc(self) -> float:
        """当日已投入（与下单闸门同一口径：非干跑订单金额，按 trading_day 换日）。"""
        today = trading_day()
        return sum(
            order.amount
            for order in self.orders.values()
            if order.status != DRY_RUN_STATUS
            and trading_day(order.created_at) == today
        )

    def _daily_realized_pnl_by_sport(self) -> dict[str, float]:
        today = trading_day()
        daily: dict[str, float] = {}
        for position in self.positions.values():
            if not position.closed_at or trading_day(position.closed_at) != today:
                continue
            sport = (position.sport or "UNKNOWN").upper()
            daily[sport] = daily.get(sport, 0.0) + (position.pnl or 0.0)
        return daily

    def _check_daily_loss(self, cancel_open_orders: bool = False):
        daily_pnl = self._daily_realized_pnl()
        breached = self.max_daily_loss > 0 and daily_pnl <= -self.max_daily_loss
        if breached:
            if self.circuit_breaker != "GLOBAL_DISABLED":
                self.circuit_breaker = "DAILY_LOSS"
            self.disarm()
            if cancel_open_orders:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop:
                    loop.create_task(self.cancel_all())
        elif self.circuit_breaker == "DAILY_LOSS":
            self.circuit_breaker = ""
        for sport, rules in self.config.sport_rules.items():
            daily_pnl = self._daily_realized_pnl_by_sport().get(sport, 0.0)
            breached = rules.live_max_daily_loss_usdc > 0 and daily_pnl <= -rules.live_max_daily_loss_usdc
            if breached:
                self.sport_circuit_breakers[sport] = "DAILY_LOSS"
                if cancel_open_orders:
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        loop = None
                    if loop:
                        loop.create_task(self.cancel_all())
            elif self.sport_circuit_breakers.get(sport) == "DAILY_LOSS":
                self.sport_circuit_breakers[sport] = ""

    def _persist_dedupe(self, token_id: str):
        self.dedupe_keys.add(token_id)
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            with self._state_path.open("a", encoding="utf-8") as file:
                file.write(json.dumps({"kind": "DEDUPE", "token_id": token_id}, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning("Failed to persist dedupe key: %s", exc)

    def _release_dedupe(self, token_id: str):
        """撤单后释放去重键，允许同一市场重新报价。"""
        if not token_id:
            return
        self.dedupe_keys.discard(token_id)
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            with self._state_path.open("a", encoding="utf-8") as file:
                file.write(
                    json.dumps({"kind": "DEDUPE_RELEASE", "token_id": token_id}, ensure_ascii=False) + "\n"
                )
        except Exception as exc:
            logger.warning("Failed to persist dedupe release: %s", exc)

    def log_rejection(self, signal, token_id: str, sport: str, reason: str):
        self.last_reject_reason = reason or self.last_error or "UNKNOWN"
        self._append_event(
            "ORDER_REJECTED",
            {
                "sport": sport,
                "game_id": signal.game_id,
                "token_id": token_id,
                "reason": self.last_reject_reason,
            },
        )

    @staticmethod
    def _book_age_seconds(book: object) -> Optional[float]:
        raw = book.get("timestamp") if isinstance(book, dict) else getattr(book, "timestamp", None)
        if not raw:
            return None
        raw = str(raw)
        try:
            if raw.isdigit():
                timestamp = float(raw)
                if timestamp > 1e12:
                    timestamp /= 1000.0
                return max(0.0, time.time() - timestamp)
        except Exception:
            pass
        try:
            timestamp = datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
            return max(0.0, time.time() - timestamp)
        except Exception:
            return None

    @staticmethod
    def _book_entries(book: object, side: str) -> list:
        raw = book.get(side) if isinstance(book, dict) else getattr(book, side, None)
        entries = []
        for row in raw or []:
            if isinstance(row, dict):
                price = row.get("price")
                size = row.get("size")
            else:
                price = getattr(row, "price", None)
                size = getattr(row, "size", None)
            try:
                price = float(price)
                size = float(size)
            except (TypeError, ValueError):
                continue
            if price > 0 and size > 0:
                entries.append(SimpleNamespace(price=price, size=size))
        return entries

    @staticmethod
    def _book_tick_size(book: object) -> float:
        """该市场的真实 tick（0.01 / 0.001）。实现收拢到 src/entry_price.py。"""
        return _shared_tick_size_of(book)

    @staticmethod
    def _book_min_order_size(book: object) -> float:
        """读取市场最小下单份额；读不到时由调用方使用 1 份。"""
        if isinstance(book, dict):
            value = book.get("min_order_size", book.get("minimum_order_size"))
        else:
            value = getattr(
                book,
                "min_order_size",
                getattr(book, "minimum_order_size", None),
            )
        try:
            parsed = float(value)
            return parsed if parsed > 0 else 0.0
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _buffered_entry_price(
        best_ask: float, tick: float, buffer_ticks: int, min_expected_profit: float
    ) -> float:
        """在卖一之上加若干 tick 缓冲，既提高成交率，又不突破最低理论收益。

        2026-09-28：实盘与模拟盘必须用**同一份**实现（同一份口径），
        否则又会出现"模拟盘记卖一价、实盘吃卖一+1tick"的 1 分钱系统性偏差。
        """
        return _shared_buffered_entry_price(
            best_ask, tick, buffer_ticks, min_expected_profit
        )

    def _append_event(self, kind: str, data: dict):
        try:
            self._events_path.parent.mkdir(parents=True, exist_ok=True)
            event = {"kind": kind, "at": time.time(), "data": data}
            with self._events_path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning("Failed to append live order event: %s", exc)
