# -*- coding: utf-8 -*-
"""Read-only Polymarket account monitoring for the live panel."""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import aiohttp
from dotenv import load_dotenv

from src.config import Config
from src.atomic_io import rotate_by_size, tail_lines


logger = logging.getLogger(__name__)


def _num(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def pending_settlement(positions: list[dict], closed_positions: list[dict]) -> dict:
    """「已定局但还没进已结算列表」的仓位 → 待入账盈亏。

    为什么需要它（2026-10-16 用户："面板上的已实现盈亏一直在骗人"）
    -----------------------------------------------------------------
    `data-api/closed-positions` 对**亏损**仓位有约一天的滞后：10-07 那批 5 笔亏光、
    10-16 上午批量赎回的 8 笔亏光，在这段时间里**只出现在 `/positions`（curPrice=0）**，
    一笔都不在 `closed-positions` 里。于是面板的「交易所已实现盈亏」只统计赢的那些 ——
    实测 10-09~10-16 面板给 +$17.49（+6.68%），真实只有 +$3.35（+1.21%），
    差出来的 $14.14 全是"还没入账的亏损"。

    口径（只记**已经定局**的，绝不预支浮盈）：
      * `curPrice <= 0.02`            → 市场已定局且我们归零，按 **−cost** 入账；
      * `redeemable` 且 `curPrice >= 0.98` → 已可赎回的赢单，按 `size − cost` 入账；
      * 其余（还在打的）一律不算 —— 它们已经体现在「持仓市值」里，重复计入会虚增。
      * 已经出现在 `closed_positions` 里的 asset **排除**，避免同一笔算两次。

    返回 dict：count / pnl / cost / wins / losses / items（面板只用前四个）。
    """
    closed_assets = {
        str(p.get("asset_id") or p.get("id") or "")
        for p in (closed_positions or [])
    }
    items: list[dict] = []
    for p in positions or []:
        asset = str(p.get("asset_id") or p.get("id") or "")
        if asset and asset in closed_assets:
            continue
        size = _num(p.get("size"))
        avg = _num(p.get("avg_price"))
        price = _num(p.get("current_price"))
        if size <= 0 or avg <= 0:
            continue
        cost = size * avg
        if price <= 0.02:
            pnl = -cost
        elif p.get("redeemable") and price >= 0.98:
            pnl = size * 1.0 - cost
        else:
            continue
        items.append({
            "asset_id": asset,
            "title": str(p.get("title") or ""),
            "outcome": str(p.get("outcome") or ""),
            "size": round(size, 4),
            "avg_price": round(avg, 4),
            "cost": round(cost, 4),
            "pnl": round(pnl, 4),
        })
    return {
        "count": len(items),
        "pnl": round(sum(i["pnl"] for i in items), 4),
        "cost": round(sum(i["cost"] for i in items), 4),
        "wins": sum(1 for i in items if i["pnl"] > 0),
        "losses": sum(1 for i in items if i["pnl"] <= 0),
        "items": items,
    }


class HttpStatusError(RuntimeError):
    """带状态码的 HTTP 错误。

    为什么要单独一个类型（2026-09-20）：`_get_json` 原来用 `except RuntimeError: raise`
    把**所有** HTTP 状态错误都当成"确定性错误"直接抛出、不重试 —— 结果是
    Polymarket 偶发一个 **HTTP 500**（服务端瞬时故障）就直接弹到面板上
    （用户实测告警："成交: HTTP 500: {"error":"Internal server error"}"）。
    正确的区分是：**4xx = 我们请求错了（重试无用）；5xx = 对方服务端抖动（该重试）**。
    带上状态码，重试逻辑才能分开对待。
    """

    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {int(status)}: {body[:180]}")
        self.status = int(status)


@dataclass
class LiveAccountState:
    mode: str = "readonly"
    connected: bool = False
    wallet_address: str = ""
    api_configured: bool = False
    balance_usdc: Optional[float] = None
    balance_raw: Optional[float] = None
    portfolio_value: float = 0.0
    positions: list[dict] = field(default_factory=list)
    closed_positions: list[dict] = field(default_factory=list)
    orders: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # 连接类错误单独记：它们是网络抖动，不该和「凭据失效 / 域名被封」同等对待。
    # 2026-09-14 凌晨断网期间，面板每隔 60 秒弹一次「余额: Cannot connect ...」，
    # 实际只是出口抖动，余额数字本身一直是好的（上一次成功快照仍然有效）。
    transient_errors: list[str] = field(default_factory=list)
    updated_at: Optional[float] = None
    last_success_at: Optional[float] = None
    consecutive_failures: int = 0


def normalize_balance(raw, decimals: Optional[int] = None) -> dict:
    """把 CLOB `/balance-allowance` 的 `balance` 折成 USDC。

    ⚠️ 这个接口返回的是**基础单位**（USDC 6 位小数），必须**无条件**除以 10^decimals。

    2026-10-08 修一个"只在余额 < $1 时才现形"的 bug：原来写的是
        `normalized = raw / 10**decimals if raw >= 10**decimals else raw`
    —— 那个 `else raw` 本意是"万一接口哪天返回已经归一化的数就别再除"，
    但它**恰好在余额低于 1 USDC 时误判**：真实 0.93111 USDC 的原始值是 931110，
    小于 10**6 → 原样返回 → 首页把**$0.93 显示成 $931,110**。
    同样的误判在 09-22 也出现过（显示 175,610 = 真实的 $0.1756），
    并且把 `live_account_history.jsonl` 里的收益曲线也带歪了（2,716 行脏点）。
    实测该接口一直返回整数基础单位（16764870↔16.76487、931110↔0.93111），
    所以正确做法就是**一直除**；万一以后接口改成返回小数，那也应该是小数本身，
    用"是不是整数"来判别即可（见下）。
    """
    if decimals is None:
        decimals = int(os.getenv("PM_BALANCE_DECIMALS", "6"))
    value = float(raw)
    # 基础单位一定是整数（链上 uint）；万一是小数说明接口已经归一化过 → 原样用
    if float(value).is_integer():
        return {"balance_raw": value, "balance_usdc": value / (10 ** decimals)}
    return {"balance_raw": value, "balance_usdc": value}


class LiveAccountMonitor:
    def __init__(self, config: Config):
        self.config = config
        self.state = LiveAccountState()
        self._session: Optional[aiohttp.ClientSession] = None
        self._refresh_task: Optional[asyncio.Task] = None
        # 每次成功刷新后回调，供实盘执行器做对账（只读数据，不涉及下单）
        self.on_snapshot = None
        self._stopping = asyncio.Event()
        self._history_path = Path("trades/live_account_history.jsonl")
        self.history: list[dict] = []
        self._load_history()

    @property
    def credentials_configured(self) -> bool:
        return all(
            [
                os.getenv("PM_API_KEY"),
                os.getenv("PM_API_SECRET"),
                os.getenv("PM_API_PASSPHRASE"),
                os.getenv("PM_SIGNER_ADDRESS") or os.getenv("PM_WALLET_ADDRESS"),
            ]
        )

    async def start(self):
        load_dotenv(override=False)
        self.state.wallet_address = os.getenv("PM_SIGNER_ADDRESS") or os.getenv("PM_WALLET_ADDRESS", "")
        self.state.api_configured = self.credentials_configured
        self._session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=False),
            timeout=aiohttp.ClientTimeout(total=15),
        )
        self._stopping.clear()
        self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def stop(self):
        self._stopping.set()
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()

    def status(self) -> dict:
        # 交易所的 /positions 会把"已结算但未赎回"的 token 一直挂着（价值 0）。
        # 这类记录不是持仓，必须和"真实持仓"分开统计，否则面板的「持仓数」会骗人。
        active = [
            p for p in self.state.positions
            if self._float(p.get("value", 0)) > 0.01
        ]
        unredeemed = [
            p for p in self.state.positions
            if self._float(p.get("value", 0)) <= 0.01
        ]
        redeemable_value = sum(
            self._float(p.get("value", 0))
            for p in self.state.positions
            if p.get("redeemable")
        )
        # 已定局但还没进 closed_positions 的仓位（多为亏光的单子，见 pending_settlement 的说明）。
        # 面板的「已实现盈亏」必须把它算进去，否则每天看到的都是"只赚不亏"。
        pending = pending_settlement(self.state.positions, self.state.closed_positions)
        return {
            "mode": "readonly",
            "order_execution_available": False,
            "connected": self.state.connected,
            "wallet_address": self.state.wallet_address,
            "api_configured": self.state.api_configured,
            "balance_usdc": self.state.balance_usdc,
            "balance_raw": self.state.balance_raw,
            "portfolio_value": round(self.state.portfolio_value, 2),
            "positions": self.state.positions,
            "positions_active": len(active),
            "positions_unredeemed": len(unredeemed),
            "redeemable_value": round(redeemable_value, 4),
            "pending_settlement_count": pending["count"],
            "pending_settlement_pnl_usdc": pending["pnl"],
            "pending_settlement_cost_usdc": pending["cost"],
            "pending_settlement_wins": pending["wins"],
            "pending_settlement_losses": pending["losses"],
            "closed_positions": self.state.closed_positions,
            "orders": self.state.orders,
            "trades": self.state.trades,
            "errors": self.state.errors,
            "transient_errors": self.state.transient_errors,
            # degraded = 只有抖动、没有真错误：面板该显示「数据是 X 分钟前的快照」，
            # 而不是把余额位置换成一行红色报错。
            "degraded": bool(self.state.transient_errors) and not self.state.errors,
            "consecutive_failures": self.state.consecutive_failures,
            "last_success_at": self.state.last_success_at,
            "stale_seconds": (
                round(time.time() - self.state.last_success_at, 1)
                if self.state.last_success_at
                else None
            ),
            "updated_at": self.state.updated_at,
            "equity_history": self._compressed_history(),
        }

    def _compressed_history(self, limit: int = 1000) -> list[dict]:
        """只保留权益真正发生变化的采样点，避免曲线被重复点拉平。"""
        points = []
        previous = None
        for point in self.history:
            value = point.get("total_value")
            if previous is None or value != previous:
                points.append(point)
                previous = value
        return points[-limit:]

    def _load_history(self):
        if not self._history_path.exists():
            return
        # 只读末尾若干行：每 60 秒一行，跑一个月就有几万行，
        # 启动时整份读既慢又没必要（曲线只用最近的变化点）。
        limit = max(100, int(getattr(self.config, "live_history_load_lines", 20000) or 20000))
        try:
            for line in tail_lines(self._history_path, limit):
                try:
                    self.history.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except Exception as exc:
            logger.warning("Failed to load live account history: %s", exc)

    def _append_history(self):
        point = {
            "t": time.time(),
            "balance_usdc": self.state.balance_usdc,
            "portfolio_value": round(self.state.portfolio_value, 2),
            "total_value": round(
                (self.state.balance_usdc or 0) + self.state.portfolio_value, 2
            ),
        }
        self.history.append(point)
        self.history = self.history[-5000:]
        try:
            self._history_path.parent.mkdir(parents=True, exist_ok=True)
            rotate_by_size(
                self._history_path,
                max_bytes=int(getattr(self.config, "live_history_max_bytes", 0) or 0),
                backup_count=int(getattr(self.config, "live_history_backup_count", 3) or 0),
            )
            with self._history_path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(point, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning("Failed to save live account history: %s", exc)

    async def _refresh_loop(self):
        while not self._stopping.is_set():
            await self.refresh()
            try:
                await asyncio.sleep(self.config.live_refresh_interval)
            except asyncio.CancelledError:
                raise

    async def refresh(self):
        errors: list[str] = []
        transient: list[str] = []
        self.state.wallet_address = os.getenv("PM_SIGNER_ADDRESS") or os.getenv("PM_WALLET_ADDRESS", "")
        self.state.api_configured = self.credentials_configured
        self.state.connected = False

        if not self._session:
            errors.append("账户监控未启动")
            self._finish_refresh(errors, transient)
            return

        results = await asyncio.gather(
            self._fetch_balance(),
            self._fetch_positions(),
            self._fetch_closed_positions(),
            self._fetch_orders(),
            self._fetch_trades(),
            return_exceptions=True,
        )

        balance, positions, closed_positions, orders, trades = results
        for name, result in zip(["余额", "持仓", "已结算持仓", "订单", "成交"], results):
            if isinstance(result, Exception):
                # 连接/超时 → 抖动；HTTP 4xx、缺配置、域名被屏蔽 → 真错误
                target = transient if self._is_transient(result) else errors
                target.append(f"{name}: {result}")

        if isinstance(positions, list):
            self.state.positions = positions
            self.state.portfolio_value = sum(
                self._float(item.get("value", item.get("current_value", 0)))
                for item in positions
            )
        if isinstance(orders, list):
            self.state.orders = orders
        if isinstance(closed_positions, list):
            self.state.closed_positions = closed_positions
        if isinstance(trades, list):
            self.state.trades = trades
        if isinstance(balance, dict):
            self.state.balance_raw = self._float(balance.get("balance_raw"))
            self.state.balance_usdc = self._optional_float(balance.get("balance_usdc"))

        self._finish_refresh(errors, transient)

    @staticmethod
    def _is_transient(exc: BaseException) -> bool:
        """连接/超时类错误是网络抖动，重试一下通常就好，不该污染错误区。"""
        return isinstance(
            exc,
            (
                aiohttp.ClientConnectionError,  # 连不上 / 被断开
                aiohttp.ClientPayloadError,     # 响应没收全
                asyncio.TimeoutError,
            ),
        )

    def _finish_refresh(self, errors: list[str], transient: Optional[list[str]] = None):
        transient = list(transient or [])
        self.state.errors = errors[:20]
        self.state.transient_errors = transient[:20]
        self.state.updated_at = time.time()
        self.state.connected = not errors and not transient
        if self.state.connected:
            self.state.consecutive_failures = 0
            self.state.last_success_at = time.time()
            self._append_history()
            self._notify_snapshot()
            return
        # 抖动期间日志按「连续失败次数」收敛：断网一整夜曾刷出上万行 WARNING，
        # 既没有新信息，又把日志轮转吃光（历史日志因此被冲掉）。
        self.state.consecutive_failures += 1
        detail = "; ".join(errors + transient)
        if self.state.consecutive_failures == 1 or self.state.consecutive_failures % 10 == 0:
            logger.warning(
                "Live account readonly refresh 失败（连续 %d 次）：%s",
                self.state.consecutive_failures, detail,
            )
        else:
            logger.debug("Live account readonly refresh 失败：%s", detail)

    def _notify_snapshot(self):
        """数据完整时才通知订阅者（对账依赖完整数据，不能拿半份数据做判断）。"""
        callback = self.on_snapshot
        if callback is None:
            return
        try:
            result = callback(self.state)
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("账户快照回调失败：%s: %s", type(exc).__name__, exc)

    async def _get_json(self, url: str, params: Optional[dict] = None, headers: Optional[dict] = None) -> object:
        """带重试的只读 GET。

        出口抖动时（实测：新建连接失败、响应被截断）同一次刷新里重试几乎总能成功，
        而每次刷新的结果直接决定面板显示什么 —— 一次抖动就让「余额」位置整分钟
        显示报错，代价远大于多打两个请求。HTTP 4xx / 缺凭据 / 域名被屏蔽不重试。
        """
        attempts = max(1, int(getattr(self.config, "live_fetch_attempts", 3) or 1))
        delay = max(0.0, float(getattr(self.config, "live_fetch_retry_delay_seconds", 0.4) or 0.0))
        last_exc: Optional[BaseException] = None
        for attempt in range(attempts):
            try:
                return await self._get_json_once(url, params, headers)
            except HttpStatusError as exc:
                # 2026-09-20：4xx 是确定性错误（重试没用），**5xx 是服务端抖动（该重试）**。
                if exc.status < 500 or attempt + 1 >= attempts:
                    raise
                last_exc = exc
                await asyncio.sleep(delay * (2 ** attempt))
            except RuntimeError:
                raise  # 确定性错误（缺配置 / 域名被封 / JSON 坏了），重试没有意义
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt + 1 >= attempts:
                    break
                await asyncio.sleep(delay * (2 ** attempt))
        raise last_exc if last_exc else RuntimeError("请求失败")

    async def _get_json_once(self, url: str, params: Optional[dict] = None, headers: Optional[dict] = None) -> object:
        if not self._session:
            raise RuntimeError("HTTP session is not initialized")
        async with self._session.get(
            url,
            params=params,
            headers=headers,
            proxy=self.config.proxy_url,
        ) as response:
            text = await response.text()
            if response.status >= 400:
                if "This Domain Name Has Been Blocked" in text or "此網域已經遭到封鎖" in text:
                    raise RuntimeError(
                        "当前网络出口屏蔽了 Polymarket 域名，请切换 VPN 节点后重启服务"
                    )
                raise HttpStatusError(response.status, text)
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Invalid JSON response: {text[:180]}") from exc

    def _level_2_headers(self, request_path: str) -> dict:
        api_key = os.getenv("PM_API_KEY", "")
        api_secret = os.getenv("PM_API_SECRET", "")
        passphrase = os.getenv("PM_API_PASSPHRASE", "")
        wallet = os.getenv("PM_SIGNER_ADDRESS") or os.getenv("PM_WALLET_ADDRESS", "")
        if not all([api_key, api_secret, passphrase, wallet]):
            raise RuntimeError("缺少 PM_API_KEY / PM_API_SECRET / PM_API_PASSPHRASE / PM_WALLET_ADDRESS")

        timestamp = str(int(time.time()))
        message = f"{timestamp}GET{request_path}"
        try:
            secret = base64.urlsafe_b64decode(api_secret + "=" * (-len(api_secret) % 4))
        except Exception as exc:
            raise RuntimeError("PM_API_SECRET 不是有效的 Base64 字符串") from exc
        signature = base64.urlsafe_b64encode(
            hmac.new(secret, message.encode("utf-8"), hashlib.sha256).digest()
        ).decode("utf-8")
        return {
            "POLY_ADDRESS": wallet,
            "POLY_SIGNATURE": signature,
            "POLY_TIMESTAMP": timestamp,
            "POLY_API_KEY": api_key,
            "POLY_PASSPHRASE": passphrase,
        }

    async def _fetch_balance(self) -> dict:
        path = "/balance-allowance"
        params = {
            "asset_type": "COLLATERAL",
            "signature_type": int(os.getenv("PM_SIGNATURE_TYPE", "1")),
        }
        data = await self._get_json(
            f"{self.config.polymarket_clob_url}{path}",
            params=params,
            headers=self._level_2_headers(path),
        )
        if not isinstance(data, dict):
            raise RuntimeError("余额接口返回格式不是对象")
        raw = self._optional_float(data.get("balance", data.get("balance_amount")))
        if raw is None:
            raise RuntimeError("余额接口没有返回 balance")
        return normalize_balance(raw)

    async def _fetch_positions(self) -> list[dict]:
        wallet = os.getenv("PM_FUNDER_ADDRESS") or self.state.wallet_address
        if not wallet:
            return []
        rows = await self._fetch_v2_position_pages(
            wallet,
            status="OPEN",
            page_size=500,
            max_pages=5,
            sort_by="CURRENT_VALUE",
            size_threshold=0.01,
        )
        return [self._normalize_position(item) for item in rows]

    async def _fetch_closed_positions(self) -> list[dict]:
        """已结算的实盘仓位，用来给出每笔的输赢与收益。

        Data API v2 的 `status=CLOSED` 使用 cursor 分页。v1 offset 接口已于
        2026-11-02 下线。

        为什么这件事很显眼（2026-09-15 用户发现）：只取最近 50 笔时，
        面板「交易所已实现盈亏」显示 **-2.48**，而账户从充值的 19.97 只跌到
        18.84（真实亏损 -1.13）。差额来自被截掉的 9 笔更早的（盈利）仓位 ——
        分页取全 59 笔后合计正好是 **-1.13**，与实际权益变化对得上。
        """
        wallet = os.getenv("PM_FUNDER_ADDRESS") or self.state.wallet_address
        if not wallet:
            return []
        page_size = 50
        max_pages = max(1, int(getattr(self.config, "live_closed_positions_max_pages", 20) or 20))
        rows = await self._fetch_v2_position_pages(
            wallet,
            status="CLOSED",
            page_size=page_size,
            max_pages=max_pages,
            sort_by="TIMESTAMP",
        )
        return [self._normalize_position(item, closed=True) for item in rows]

    async def _fetch_v2_position_pages(
        self,
        wallet: str,
        *,
        status: str,
        page_size: int,
        max_pages: int,
        sort_by: str,
        size_threshold: Optional[float] = None,
    ) -> list[dict]:
        """Walk the Data API v2 positions lifecycle with opaque cursor pagination."""
        cursor: Optional[str] = None
        rows: list[dict] = []
        seen: set[str] = set()
        for _ in range(max_pages):
            params = {
                "user": wallet,
                "status": status,
                "limit": page_size,
                "sort_by": sort_by,
                "sort_direction": "DESC",
            }
            if size_threshold is not None and status != "CLOSED":
                params["size_threshold"] = size_threshold
            if cursor:
                params["cursor"] = cursor
            payload = await self._get_json(
                f"{self.config.data_api_url}/v2/positions",
                params=params,
            )
            if not isinstance(payload, dict):
                raise RuntimeError("v2 持仓接口返回格式不是对象")
            page_rows = payload.get("data")
            if page_rows is None:
                break
            if not isinstance(page_rows, list):
                raise RuntimeError("v2 持仓接口的 data 不是数组")
            for item in page_rows:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("token_id") or item.get("condition_id") or "")
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                rows.append(item)
            pagination = payload.get("pagination")
            if not isinstance(pagination, dict):
                break
            cursor = pagination.get("next_cursor")
            if not cursor:
                break
        return rows

    async def _fetch_paginated_clob(self, path: str) -> list[dict]:
        cursor = "MA=="
        end_cursor = "LTE="
        results: list[dict] = []
        for _ in range(3):
            data = await self._get_json(
                f"{self.config.polymarket_clob_url}{path}",
                params={"next_cursor": cursor},
                headers=self._level_2_headers(path),
            )
            if not isinstance(data, dict) or "data" not in data:
                raise RuntimeError(f"{path} 返回格式不是分页对象")
            rows = data.get("data", [])
            if not isinstance(rows, list):
                raise RuntimeError(f"{path} 的 data 不是数组")
            results.extend(row for row in rows if isinstance(row, dict))
            cursor = data.get("next_cursor", end_cursor)
            if not cursor or cursor == end_cursor:
                break
        return results[:100]

    async def _fetch_orders(self) -> list[dict]:
        if not self.credentials_configured:
            raise RuntimeError("未配置只读 API 凭据")
        return [self._normalize_order(item) for item in await self._fetch_paginated_clob("/data/orders")]

    async def _fetch_trades(self) -> list[dict]:
        if not self.credentials_configured:
            raise RuntimeError("未配置只读 API 凭据")
        return [self._normalize_trade(item) for item in await self._fetch_paginated_clob("/data/trades")]

    def _normalize_position(self, item: dict, *, closed: bool = False) -> dict:
        size = self._float(
            item.get(
                "total_size" if closed else "current_size",
                item.get(
                    "size",
                    item.get(
                        "totalShares",
                        item.get("shares", item.get("current_size", 0)),
                    ),
                ),
            )
        )
        price = self._float(
            item.get(
                "current_price",
                item.get("curPrice", item.get("cur_price", item.get("price", 0))),
            )
        )
        value = self._float(
            item.get("currentValue", item.get("current_value", size * price))
        )
        realized = self._float(item.get("realizedPnl", item.get("realized_pnl", 0)))
        cash_pnl = self._float(
            item.get(
                "unrealized_pnl",
                item.get("cashPnl", item.get("cash_pnl", 0)),
            )
        )
        return {
            "id": item.get(
                "id",
                item.get("token_id", item.get("assetId", "")),
            ),
            "asset_id": item.get(
                "token_id",
                item.get("asset", item.get("assetId", "")),
            ),
            "title": item.get("title", item.get("market", item.get("slug", "-"))),
            "outcome": item.get("outcome", item.get("outcomeName", "-")),
            "size": size,
            "avg_price": self._float(item.get("avgPrice", item.get("avg_price", 0))),
            "current_price": price,
            "value": value,
            "status": item.get("status", item.get("redeemable", "")),
            "closed": closed,
            "realized_pnl": realized,
            "cash_pnl": cash_pnl,
            "pnl": realized if closed else cash_pnl,
            "percent_pnl": self._float(item.get("percentPnl", item.get("percent_pnl", 0))),
            "redeemable": bool(item.get("redeemable", False)),
            "settled_at": item.get("last_event_at", item.get("timestamp")),
            "raw": item,
        }

    def _normalize_order(self, item: dict) -> dict:
        return {
            "id": item.get("id", ""),
            "market": item.get("market", item.get("market_id", "")),
            "asset_id": item.get("asset_id", item.get("assetId", "")),
            "side": item.get("side", ""),
            "price": self._float(item.get("price", 0)),
            "original_size": self._float(item.get("original_size", item.get("size", 0))),
            "size_matched": self._float(item.get("size_matched", item.get("matched_size", 0))),
            "status": item.get("status", ""),
            "created_at": item.get("created_at", item.get("createdAt", "")),
            "raw": item,
        }

    def _normalize_trade(self, item: dict) -> dict:
        return {
            "id": item.get("id", ""),
            "market": item.get("market", item.get("market_id", "")),
            "asset_id": item.get("asset_id", item.get("assetId", "")),
            "side": item.get("side", ""),
            "price": self._float(item.get("price", 0)),
            "size": self._float(item.get("size", 0)),
            "status": item.get("status", ""),
            "match_time": item.get("match_time", item.get("matchTime", item.get("created_at", ""))),
            "raw": item,
        }

    @staticmethod
    def _float(value: object) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _optional_float(value: object) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
