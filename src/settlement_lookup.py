# -*- coding: utf-8 -*-
"""市场结算结果查询（Gamma public-search）：模拟盘结算兜底的唯一来源。

为什么需要它（2026-09-20）：
  * 实盘的结算来自**交易所对账**（token 已归零 / 可赎回），权威、且不怕重启；
  * 模拟盘的结算来自**比分流** —— 必须在"该场比赛还在快照里、且 ended=True"
    的那一 tick 结算。而 PM 实时流的比赛行在终局 30 分钟后就被淘汰，中间只要
    WS 抖一下，终局那条消息就永远丢了：实测 CS2 1681114（09-19 02:10 开仓）
    的终局恰好落在 03:42–03:48 的重连潮里，仓位 39 小时后仍然挂在 OPEN，
    于是模拟盘 CS2 显示 22 胜 0 负，实盘却是 13 胜 1 负。

所以模拟盘需要第二条与传输层无关的结算来源：直接问市场"这个 token 最后结算成
什么"。本模块**只做查询**，不动账本；写账在 `PaperTrader.close_position`（在线
兜底，见 src/settlement_reconcile.py）与 `tmp/recalc_paper_cash.py`（重启补录）。

这段解析历史上抄过两份（`tmp/recalc_paper_cash.py` / `tmp/fix_settlement_prices.py`），
2026-09-20 收拢到这里 —— 项目自己的规矩就是"复用同一份市场解析，避免两套实现走偏"。
"""

import json
import unicodedata
import urllib.parse
import urllib.request

GAMMA = "https://gamma-api.polymarket.com"


def normalize(name: object) -> str:
    text = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    return "".join(ch for ch in text.lower() if ch.isalnum())


def gamma_get(url: str, timeout: float = 25.0) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def market_prices(market: dict) -> tuple[list[str], list[float]] | None:
    try:
        outcomes = json.loads(market.get("outcomes") or "[]")
        prices = [float(x) for x in json.loads(market.get("outcomePrices") or "[]")]
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if outcomes and len(outcomes) == len(prices):
        return outcomes, prices
    return None


def market_is_resolved(market: dict) -> bool:
    """市场是否**真的结算完了**：closed 且每个 outcome 价都落在 0/1 附近。

    只查 `closed` 还不够：结算前的最后一刻盘口价可能还停在 0.365，
    拿它当结算价会把一个还在跑的仓位按盘中价结掉。
    """
    if not market.get("closed"):
        return False
    pair = market_prices(market)
    if not pair:
        return False
    prices = pair[1]
    return all(abs(price) <= 0.02 or abs(price - 1.0) <= 0.02 for price in prices)


def market_token_ids(market: dict) -> list[str]:
    try:
        return [str(x) for x in json.loads(market.get("clobTokenIds") or "[]")]
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def pick_resolved_market(
    markets: list[dict], question: str, token_id: object = None
) -> tuple[dict | None, str]:
    """从一批候选市场里挑出**真正已结算**的那一个，挑不出来就说清原因。

    2026-09-16 实测踩到的坑：`Miami Marlins vs. Arizona Diamondbacks` 这个**字面完全相同**
    的问题在 Gamma 上有 3 个市场（endDate 9/22、9/23、9/24），只有 9/22 那个已结算。
    原实现返回"第一个问题字面相同的"，于是拿到了**还在跑的市场价 0.365** ——
    按它结算等于凭空造一笔假盈亏。

    更早还有更贵的代价：主模拟盘里有 6 笔就是这样被按**盘中价**结掉的
    （0.905 / 0.973 / 0.987 / 0.9995 / 0.984 / 0.905），其中 2 笔实际赢了却被记成亏损
    （见 tmp/fix_settlement_prices.py 的修复记录）。

    所以这里：
      * 有 `token_id` 就用它定位 —— 这是**唯一无歧义**的键（同一个 token 只属于一个市场）。
        注意不能用信号事件里的 `market_id`：那是 **event id**（如 993729），跟 market id
        不是一个量纲，拿它匹配等于没匹配（实测就是这么又选错一次的）；
      * 只在「已结算」的市场里选（`closed` 且价已到 0/1）；
      * 仍有歧义就**拒绝结算**并说明，绝不猜。
    """
    if token_id:
        token_text = str(token_id)
        for market in markets:
            if token_text not in market_token_ids(market):
                continue
            if market_is_resolved(market):
                return market, f"token 命中市场 {market.get('id')}（已结算）"
            return None, (
                f"token 命中的市场 {market.get('id')} 尚未结算"
                f"（closed={market.get('closed')}，价 {market.get('outcomePrices')}）"
            )
    same = [m for m in markets if str(m.get("question") or "").strip() == question.strip()]
    resolved = [m for m in same if market_is_resolved(m)]
    if not resolved:
        return None, f"问题匹配到 {len(same)} 个市场，其中没有一个已结算"
    if len(resolved) > 1:
        return None, f"问题匹配到 {len(resolved)} 个已结算市场，无法确定是哪一个"
    return resolved[0], "问题精确匹配（已结算）"


def resolve_market(
    question: str, token_id: object = None, timeout: float = 25.0
) -> tuple[tuple[list[str], list[float]] | None, str]:
    """联网查结算价，返回 ((outcomes, prices) | None, 说明)。"""
    if not question and not token_id:
        return None, "缺少市场信息"
    query = question or str(token_id)
    try:
        data = gamma_get(f"{GAMMA}/public-search?q={urllib.parse.quote(query[:90])}", timeout)
    except Exception as exc:  # noqa: BLE001
        return None, f"查询失败：{exc}"
    markets = [m for event in (data.get("events") or []) for m in (event.get("markets") or [])]
    market, note = pick_resolved_market(markets, question, token_id)
    if market is None:
        return None, note
    pair = market_prices(market)
    if not pair:
        return None, "命中的市场缺 outcomes/outcomePrices"
    return pair, note


def resolve_outcome_price(
    question: str, token_id: object, team: object, timeout: float = 25.0
) -> tuple[float | None, str]:
    """查某个队伍对应的 token 最终结算价，返回 (0/1 | None, 说明)。

    只有拿到 0/1 才返回价格；查到"市场还没结算"或"结算价不是 0/1"时返回 None +
    原因 —— 调用方据此保持仓位 OPEN，绝不按盘中价结账。
    """
    resolved, note = resolve_market(question, token_id, timeout)
    if not resolved:
        return None, note
    outcomes, prices = resolved
    target = normalize(team)
    index = next((i for i, name in enumerate(outcomes) if normalize(name) == target), None)
    if index is None:
        return None, f"结算结果里没有 {team}（{outcomes}）"
    price = float(prices[index])
    if abs(price) > 0.02 and abs(price - 1.0) > 0.02:
        return None, f"{outcomes[index]} 的结算价是 {price}，不是 0/1，拒绝据此结算"
    return price, f"{outcomes[index]} 结算价 {price}"
