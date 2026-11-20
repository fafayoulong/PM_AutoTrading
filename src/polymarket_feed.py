# -*- coding: utf-8 -*-
"""Fetch Polymarket market prices via public Gamma API."""

import json
import logging
import re
import time
import unicodedata
from datetime import datetime
from dataclasses import dataclass
from typing import Optional

import aiohttp

from src.config import Config

logger = logging.getLogger(__name__)


@dataclass
class MarketPrice:
    event_id: str
    question: str
    token_ids: list[str]
    outcomes: list[str]
    best_bid: float
    best_ask: float
    # 该市场的**真实最小报价单位**（Gamma `orderPriceMinTickSize`）。
    # 实测 0.01 与 0.001 两种都存在（2026-09-28 查一个 CS2 盘口就是 0.001），
    # 所以"加 1 tick 缓冲"这件事不能硬编码 0.01 —— 见 src/entry_price.py。
    tick_size: float = 0.01


class PolymarketFeed:
    def __init__(self, config: Config):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        proxy = self.config.proxy_url
        connector = aiohttp.TCPConnector(ssl=False)
        self._session = aiohttp.ClientSession(connector=connector)
        self._proxy = proxy

    async def stop(self):
        if self._session:
            await self._session.close()

    @staticmethod
    def _parse_json_list(raw) -> list:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []

    @staticmethod
    def _normalize_name(text: str) -> str:
        """统一名字写法：去重音、转小写、压缩空格、去掉标点。

        `Atlético de Madrid` → `atletico de madrid`（PM 常带重音，ESPN 不带）。
        """
        raw = unicodedata.normalize("NFKD", str(text or ""))
        ascii_text = raw.encode("ascii", "ignore").decode("ascii")
        cleaned = re.sub(r"[^0-9a-zA-Z]+", " ", ascii_text)
        return re.sub(r"\s+", " ", cleaned).strip().lower()

    # 俱乐部通用词：比对时先剔掉，避免「Real Madrid / Real Sociedad / Real Racing Club
    # 都含 real」这种靠泛化词误配（2026-09-17 加）。
    CLUB_STOPWORDS = {
        "fc", "cf", "sc", "ac", "afc", "cfc", "cd", "sd", "ud", "sv", "fk", "sk", "bk",
        "vfl", "vfb", "tsg", "sg", "as", "us", "ss", "ssc", "cs", "ks",
        "club", "clube", "futebol", "futbol", "calcio", "team", "the",
        "de", "do", "da", "del", "la", "el", "los", "las", "of",
        "real", "atletico", "athletic", "sporting", "sport", "sports",
        "city", "town", "united", "utd", "rovers", "wanderers", "county",
        "borussia", "olympique", "gimnastic", "gimnasia",
    }

    @classmethod
    def _name_tokens(cls, text: str) -> set:
        """名字里的**实词**（去掉俱乐部通用词后的词缀），用于宽松比对。"""
        return {
            token for token in cls._normalize_name(text).split(" ")
            if len(token) >= 3 and token not in cls.CLUB_STOPWORDS
        }

    @classmethod
    def _name_hit_score(cls, title_normalized: str, variants: list, tokens: set) -> int:
        """标题里命中这个队名的强度：完整写法 4 分、实词交集 2 分、没命中 0 分。

        为什么要分两档（2026-09-17，用户问"#376 为什么没触发"）：
        ESPN 写 `Racing Santander`，Polymarket 用官方名 `Real Racing Club`，
        原来要求"客队名完整出现在 PM 标题里"，于是整场被当成"没有盘口" ——
        模型给了 90.8% 却什么都没做。改成"至少一个实词相同"即可命中，
        同时靠停用词表挡住 `Real Madrid` 这种只共有泛化词的情形。
        """
        if any(v and v in title_normalized for v in variants):
            return 4
        if tokens and tokens & cls._name_tokens(title_normalized):
            return 2
        return 0

    @classmethod
    def _name_variants(cls, name: str) -> list:
        """同一队名/人名的多种可能写法（Polymarket 与数据源的写法并不统一）。

        实测过的三类差异（2026-09-13）：
          * 亚洲姓名顺序相反：ESPN `You Xiaodi` ↔ Polymarket `Xiaodi You`
          * 多一个词：ESPN `Martina Capurro Taborda` ↔ Polymarket `Martina Capurro`
          * 多余空格：`" Gatorian"` / `"Teletubisie vs  Gatorian"`
        （2026-09-17 起还统一去重音，见 `_normalize_name`；足球那边再叠加
        "实词交集"的宽松匹配，见 `_name_hit_score`）

        **必须同时给出"保留重音"和"去重音"两种写法**：两条匹配链路比对的文本不一样 ——
        通用匹配器（`search_sports_markets`，网球/CS2/棒球）拿的是**原始标题**，
        足球匹配器（`search_soccer_winner_market`）拿的是**归一化标题**。
        只给去重音版本会让 `QUINTESSÊNCIA`、`Atlético` 这种带重音的名字在通用链路里匹配失败
        （2026-09-17 加这一步时差点踩到，见 tmp/test_sports_name_matching.py）。
        """
        plain = re.sub(r"\s+", " ", str(name or "")).strip().lower()
        text = cls._normalize_name(name)
        if not text:
            return []
        tokens = text.split(" ")
        plain_tokens = plain.split(" ")
        variants = [plain, text]
        if len(plain_tokens) >= 2:
            variants.append(" ".join(reversed(plain_tokens)))
        if len(tokens) >= 2:
            variants.append(" ".join(reversed(tokens)))
        if len(tokens) >= 3:
            variants.append(" ".join(tokens[:-1]))
            variants.append(" ".join(tokens[1:]))
        return list(dict.fromkeys(v for v in variants if v))

    @classmethod
    def match_outcome_index(cls, outcomes, name: str) -> int:
        """把「我们的人/队」映射到盘口 outcomes 的下标；找不到或**有歧义**时返回 -1。

        背景（2026-09-19 信号 #509）：原来这里是
        `next(i for i, o in enumerate(outcomes) if search_term.lower() in o.lower())`
        —— 要求检索词是 outcome 的**原文子串**。ESPN 写 `Ma YeXin`（姓在前），
        Polymarket 写 `Yexin Ma`，子串不成立 → 下标 -1 → token 为空 → `NO_TOKEN`，
        一整场看对了的比赛就这么放掉了。
        `_name_variants()` 早就处理了「亚洲姓名顺序相反」，但只用在**找盘口**那一步，
        没用在**定下标**这一步 —— 这次把两件事统一到同一个匹配器上。

        评分（越高越可信），取**唯一最高分**，并列就返回 -1：
          100 归一化后完全相同                 `yexin ma` == `yexin ma`
           80 词序不同但词集合相同（≥2 个词）  `ma yexin` ↔ `yexin ma`
           50+ 变体命中（变体词越多越可信）    `martina capurro` ⊂ `martina capurro taborda`
           40 至少 2 个实词重合                `chanya ng` ⊂ `kai ning chanya ng`

        为什么并列要返回 -1：`Alexander Zverev` 与 `Mischa Zverev` 同场时，
        "zverev" 两边都命中，猜一个就等于可能买错人 —— 宁可不做。
        """
        texts = [str(o or "") for o in (outcomes or [])]
        name_norm = cls._normalize_name(name)
        if not texts or not name_norm:
            return -1
        name_tokens = set(name_norm.split())
        variants = [cls._normalize_name(v) for v in cls._name_variants(name)]
        variants = [v for v in dict.fromkeys(variants) if v]

        scores: list[int] = []
        for outcome in texts:
            out_norm = cls._normalize_name(outcome)
            score = 0
            if out_norm:
                if out_norm == name_norm:
                    score = 100
                else:
                    out_tokens = set(out_norm.split())
                    if len(name_tokens) >= 2 and out_tokens == name_tokens:
                        score = 80
                    else:
                        hits = [v for v in variants if v and v in out_norm]
                        if hits:
                            score = 50 + max(len(v.split()) for v in hits)
                        elif (len(out_norm) >= 4
                              and len(out_tokens) >= 1
                              and name_norm.endswith(" " + out_norm)):
                            # 2026-10-29 修（PROJECT_DESIGN #142）：NBA 的盘口 outcomes
                            # 只有昵称（`["Kings", "Spurs"]`），而 ESPN 给的是全名
                            # （`San Antonio Spurs`）→ 原来直接判 -1，导致 NBA 132 个信号里
                            # 68 个死在 `NO_TOKEN`（`leader_index: -1`），几乎一笔都下不出去。
                            # 这里补"盘口名是我们名字的后缀"这一档（≥4 字符，避免 `Wu`/`Ma`
                            # 这类短姓氏误配）；并列时下面仍然会返回 -1，不会瞎猜。
                            score = 60
                        elif len(name_tokens) >= 2 and len(name_tokens & out_tokens) >= 2:
                            score = 40
            scores.append(score)

        best = max(scores)
        if best <= 0 or scores.count(best) != 1:
            return -1
        return scores.index(best)

    @classmethod
    def _outcome_matches(cls, outcome: str, name: str) -> bool:
        """outcome 是不是这个人/队（比 match_outcome_index 宽松一档）。

        2026-09-19 现场：PM 写 `Eun-Hye Lee`，数据源写 `Eunhye Lee`。
        归一化后是 `eun hye lee` vs `eunhye lee` —— 词集合不同、只共享 `lee`，
        `match_outcome_index` 会（正确地）判 -1。但这里补一条"去掉空格后比对"：
        `eunhyelee` 两边相等，能认出来。

        为什么这里敢这么宽松：`_match_market` 会同时要求
        "我们的人和对手各自唯一命中不同的 outcome"，
        所以像 `Alexander Zverev` / `Mischa Zverev` 那种同姓同场的情况
        会因为"命中 2 个"直接被拒，不会买错人。
        """
        out_norm = cls._normalize_name(outcome)
        name_norm = cls._normalize_name(name)
        if not out_norm or not name_norm:
            return False
        if out_norm == name_norm:
            return True
        variants = [cls._normalize_name(v) for v in cls._name_variants(name)]
        if any(v and (v in out_norm or out_norm in v) for v in variants):
            return True
        # 连字符 / 名字被拆开写法：`eunhye lee` ↔ `eun hye lee`
        out_joined = out_norm.replace(" ", "")
        name_joined = name_norm.replace(" ", "")
        if out_joined == name_joined or name_joined in out_joined or out_joined in name_joined:
            return True
        return bool(cls._name_tokens(outcome) & cls._name_tokens(name))

    @staticmethod
    def _match_market(market: dict, title: str, term_lower: str, opponent_lower: str) -> bool:
        if not market.get("active", True) or market.get("closed", False):
            return False

        outcomes = PolymarketFeed._parse_json_list(market.get("outcomes", "[]"))
        if len(outcomes) != 2:
            return False

        # 2026-09-19（#499/#501/#503 连续 3 次 NO_MARKET）：
        # 原来这里是 `term_lower in outcome` 的**原文子串**判断，而 PM 把名字写成
        # `Eun-Hye Lee`（数据源是 `Eunhye Lee`）—— 连字符让子串永不成立，整场被当成
        # "没有盘口"。改成：我们的人和对手必须**各自唯一**命中**不同**的 outcome。
        # 两个条件一起才放行，所以单看"共享一个实词"这种宽松判断也是安全的
        # （两个 Zverev 同场时会命中 2 个 → 直接拒绝）。
        def pick(name: str) -> list[int]:
            """先按严格规则（唯一最高分）找；找不到才放宽到"共享实词"。

            顺序很重要：`Mischa Zverev` 与 `Alexander Zverev` 同场时，
            宽松规则会让两个 outcome 都命中 → 被当成歧义而拒绝（把本来能做的
            正常比赛误杀）。所以先用 `match_outcome_index` 这种能区分全名的
            严格判断，它给不出结果时才退到宽松规则。
            """
            strict = PolymarketFeed.match_outcome_index(outcomes, name)
            if strict >= 0:
                return [strict]
            return [i for i, o in enumerate(outcomes)
                    if PolymarketFeed._outcome_matches(str(o), name)]

        ours = pick(term_lower)
        if len(ours) != 1:
            return False
        if opponent_lower:
            theirs = pick(opponent_lower)
            if len(theirs) != 1 or theirs[0] == ours[0]:
                return False
        elif {str(o).lower() for o in outcomes} <= {"yes", "no"}:
            return False

        group_title = market.get("groupItemTitle")
        market_type = str(market.get("sportsMarketType", "")).lower()
        if group_title and market_type != "moneyline":
            return False
        if market_type and market_type != "moneyline":
            return False
        return True

    def _select_market_from_event(
        self,
        event: dict,
        term_lower: str,
        opponent_lower: str,
        skip: list[str],
    ) -> Optional[dict]:
        title = event.get('title', '').lower()
        if 'vs.' not in title and ' vs ' not in title:
            return None
        if any(keyword in title for keyword in skip):
            return None
        if term_lower not in title:
            return None
        if opponent_lower and opponent_lower not in title:
            return None

        markets = event.get('markets', [])
        if not markets:
            return None

        preferred = [
            market for market in markets
            if str(market.get('sportsMarketType', '')).lower() == 'moneyline'
        ]
        fallback = [
            market for market in markets
            if not market.get('groupItemTitle') and market.get('question', '').lower() == title
        ]
        return next(
            (
                market for market in preferred + fallback
                if self._match_market(market, title, term_lower, opponent_lower)
            ),
            None,
        )

    def _build_market_price(self, event: dict, moneyline: dict, term_lower: str) -> MarketPrice:
        token_ids = self._parse_json_list(moneyline.get('clobTokenIds', '[]'))
        outcomes = self._parse_json_list(moneyline.get('outcomes', '[]'))
        best_bid = float(moneyline.get('bestBid', 0) or 0)
        best_ask = float(moneyline.get('bestAsk', 1) or 1)

        leader_index = -1
        for index, outcome in enumerate(outcomes):
            if term_lower in str(outcome).lower():
                leader_index = index
                break

        if leader_index == 1:
            best_bid, best_ask = round(1 - best_ask, 3), round(1 - best_bid, 3)

        return MarketPrice(
            event_id=event.get('id', ''),
            question=event.get('title', ''),
            token_ids=token_ids,
            outcomes=outcomes,
            best_bid=best_bid,
            best_ask=best_ask,
            tick_size=self._tick_size(moneyline),
        )

    @staticmethod
    def _tick_size(market: dict) -> float:
        """该市场的最小报价单位；拿不到就用 0.01（绝大多数体育盘的取值）。"""
        try:
            tick = float((market or {}).get("orderPriceMinTickSize") or 0)
        except (TypeError, ValueError):
            return 0.01
        return tick if tick > 0 else 0.01

    def _log_near_misses(self, search_term: str, opponent_term: str, near_misses: list) -> None:
        """没匹配上时，把"名字像但没对上"的标题打出来，方便判断是没盘口还是名字差异。"""
        if not search_term:
            return
        _, _, surname = search_term.rpartition(" ")
        candidates = [t for t in near_misses if t and surname and surname.lower() in t.lower()]
        if not candidates:
            return
        logger.info(
            "NO_MARKET 近似候选：搜 '%s vs %s' 未命中，但同名/近名的盘口有 %d 个，例如 %s",
            search_term, opponent_term, len(candidates),
            " | ".join(str(t)[:70] for t in candidates[:3]),
        )

    async def _public_search_events(self, query: str) -> list[dict]:
        if not self._session or not query:
            return []
        url = f'{self.config.polymarket_gamma_url}/public-search'
        try:
            async with self._session.get(
                url,
                params={'q': query},
                proxy=self._proxy,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    logger.debug('Gamma public-search status %d: %s', resp.status, query)
                    return []
                data = await resp.json()
                events = data.get('events', []) if isinstance(data, dict) else []
                return events if isinstance(events, list) else []
        except Exception as exc:
            logger.debug('Gamma public-search error for %s: %s', query, exc)
            return []


    async def search_sports_markets(
        self,
        tag_slug: str,
        search_term: str,
        opponent_term: str = "",
        exclude_keywords: list = None,
        tag_id: str = None,
    ) -> Optional[MarketPrice]:
        """Search Polymarket events for a full-game moneyline market matching search_term."""
        if not self._session:
            return None

        url = f"{self.config.polymarket_gamma_url}/events"
        params = {
            "active": "true",
            "closed": "false",
            "limit": 200,
            "order": "endDate",
            "ascending": "false",
        }
        if tag_id:
            params["tag_id"] = str(tag_id)
        else:
            params["tag_slug"] = tag_slug
        try:
            async with self._session.get(url, params=params, proxy=self._proxy, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    logger.warning("Gamma events status %d for tag=%s", resp.status, tag_slug)
                    return None
                events = await resp.json()
        except Exception as exc:
            logger.warning("Gamma events error for tag=%s: %s", tag_slug, exc)
            return None

        # 名字先做空白归一化（实时流偶尔带多余空格，如 " Gatorian"）
        search_term = re.sub(r"\s+", " ", str(search_term or "")).strip()
        opponent_term = re.sub(r"\s+", " ", str(opponent_term or "")).strip()
        term_lower = search_term.lower()
        opponent_lower = opponent_term.lower()
        term_variants = self._name_variants(search_term)
        opponent_variants = self._name_variants(opponent_term)
        skip = [
            "inning", "props", "player", "first five", "doubles", "mixed doubles",
            "exact score", "set winner", "completed match", "handicap", "total",
            "over/under", "o/u",
        ]
        if exclude_keywords:
            skip.extend(exclude_keywords)

        if opponent_lower:
            search_events = await self._public_search_events(f'{search_term} {opponent_term}')
            if search_events:
                # 2026-09-20：把 public-search 的结果**放在前面**（原来是追加在 tag 结果后面）。
                # 原因：网球 tag 里根本没有当日比赛 —— 实测 tag 下只有 100 个 active 事件、
                # 结束时间最早也是 9/26（#104 查证），当日盘口只能靠 public-search 找到。
                # 而 tag 里那些远期赛事反而可能先命中、把匹配机会抢走。
                # 搜索引擎对"球员A 球员B"这种查询的相关性，本来就比按 endDate 排的 tag 列表好。
                seen_ids = {str(event.get("id")) for event in search_events}
                events = list(search_events) + [
                    event for event in events if str(event.get("id")) not in seen_ids
                ]

        near_misses: list = []
        for event in events:
            title = event.get("title", "").lower()

            if "vs." not in title and " vs " not in title:
                continue
            if any(kw in title for kw in skip):
                continue

            # 用候选写法匹配标题：姓名顺序/多词/空格差异都能兼容
            matched_term = next((v for v in term_variants if v in title), None)
            if matched_term is None:
                # 原文没命中时再用「归一化标题 + 实词」判一次（与足球那条链路同一套打分）。
                # 2026-09-19：PM 标题 `Mai Hontama vs Eun-Hye Lee` 里的连字符让
                # `eunhye lee` 这种原文子串永远对不上 → 连续 3 次 NO_MARKET。
                title_norm = self._normalize_name(title)
                norm_variants = [self._normalize_name(v) for v in term_variants]
                if self._name_hit_score(
                    title_norm, norm_variants, self._name_tokens(search_term)
                ) <= 0:
                    if term_variants and term_variants[-1] in title:
                        near_misses.append(event.get("title"))
                    continue
                matched_term = search_term.lower().strip()
            matched_opponent = ""
            if opponent_variants:
                matched_opponent = next((v for v in opponent_variants if v in title), None)
                if matched_opponent is None:
                    # 对手名同样要容忍连字符/空格差异（`Ga-Eul Jang` vs `Gaeul Jang`）
                    title_norm = self._normalize_name(title)
                    norm_opp = [self._normalize_name(v) for v in opponent_variants]
                    # ⚠️ 这里必须是 **or 的关系（任一成立即放行）**。
                    # 2026-09-20 #549/#550 的教训：我上一版写成了 `not any(...) or not tokens`，
                    # 等于要求"归一化写法是子串"**且**"实词有交集"同时成立。
                    # 而 PM 经常把对手名缩写（`Joelle Lilly Sophie Steur` → `Joelle Steur`），
                    # 子串必然不成立、实词却有交集 → 被这条判断直接毙掉，
                    # 结果就是"盘口明明开着、outcomes 就是那两个人，我们却报 NO_MARKET"。
                    if not (any(v and v in title_norm for v in norm_opp)
                            or (self._name_tokens(opponent_term) & self._name_tokens(title))):
                        near_misses.append(event.get("title"))
                        continue
                    matched_opponent = opponent_term.lower().strip()

            markets = event.get("markets", [])
            if not markets:
                # 2026-09-20：这里原来是静默 continue，让 #104 那类 bug（搜到事件却拿不到
                # markets，可能是 public-search 的精简结构）拖了很久才发现。
                # 现在按事件 id 去重地告警一次，保证"搜到了但用不了"一定会出现在日志里。
                eid = str(event.get("id") or "")
                warned = getattr(PolymarketFeed, "_warned_empty_markets", None)
                if warned is None:
                    warned = set()
                    PolymarketFeed._warned_empty_markets = warned
                if eid and eid not in warned:
                    warned.add(eid)
                    logger.warning(
                        "匹配到事件但拿不到 markets（可能是精简结构）：id=%s title=%s",
                        eid, event.get("title"),
                    )
                continue

            preferred = [market for market in markets if str(market.get("sportsMarketType", "")).lower() == "moneyline"]
            fallback = [
                market for market in markets
                if not market.get("groupItemTitle") and market.get("question", "").lower() == title
            ]
            moneyline = next(
                (
                    market for market in preferred + fallback
                    if self._match_market(
                        market, title, matched_term, matched_opponent or opponent_lower
                    )
                ),
                None,
            )
            if moneyline is None:
                # 同上：标题对上了却没有 moneyline —— 也要能被看见，不能只在 debug 里。
                eid = str(event.get("id") or "")
                warned = getattr(PolymarketFeed, "_warned_no_moneyline", None)
                if warned is None:
                    warned = set()
                    PolymarketFeed._warned_no_moneyline = warned
                if eid and eid not in warned:
                    warned.add(eid)
                    logger.warning(
                        "事件标题命中但没有 moneyline 盘：id=%s title=%s",
                        eid, event.get("title"),
                    )
                continue

            token_ids = self._parse_json_list(moneyline.get("clobTokenIds", "[]"))
            outcomes = self._parse_json_list(moneyline.get("outcomes", "[]"))

            best_bid = float(moneyline.get("bestBid", 0) or 0)
            best_ask = float(moneyline.get("bestAsk", 1) or 1)

            # Determine which token corresponds to the search term
            # outcomes = ["Team A", "Team B"] or ["Player A", "Player B"]
            # 统一走 match_outcome_index（认得姓名顺序相反，有歧义时返回 -1），
            # 保证与 main.py 定 token 时用的是同一套判断，不会一个翻面一个认不出。
            leader_index = self.match_outcome_index(outcomes, matched_term or search_term)
            if leader_index < 0:
                # 兜底：沿用旧的宽松匹配（标题命中写法的原文子串）
                for i, outcome in enumerate(outcomes):
                    if matched_term and matched_term in outcome.lower():
                        leader_index = i
                        break
            if leader_index < 0:
                for i, outcome in enumerate(outcomes):
                    if any(v in outcome.lower() for v in term_variants):
                        leader_index = i
                        break

            # If leader is the second outcome, flip the prices
            if leader_index == 1:
                best_bid, best_ask = round(1 - best_ask, 3), round(1 - best_bid, 3)

            return MarketPrice(
                event_id=event.get("id", ""),
                question=event.get("title", ""),
                token_ids=token_ids,
                outcomes=outcomes,
                best_bid=best_bid,
                best_ask=best_ask,
                tick_size=self._tick_size(moneyline),
            )

        self._log_near_misses(search_term, opponent_term, near_misses)
        return None

    async def search_nba_markets(self, search_term: str) -> Optional[MarketPrice]:
        return await self.search_sports_markets("nba", search_term)

    async def search_soccer_winner_market(
        self,
        tag_slug: str,
        team_term: str,
        opponent_term: str = "",
        tag_id: str | None = None,
    ) -> Optional[MarketPrice]:
        """找足球单场「<某队> 赢」的二元盘。

        为什么不能复用 search_sports_markets：那条链路要求 market.outcomes 里
        出现两支队的名字（篮球/棒球是「两队对阵」盘）。而足球单场在 Polymarket 上
        是**三个独立的 Yes/No 二元盘**：

            Will Seattle Sounders FC win on 2026-04-12?          ← 要的就是它
            Will Seattle Sounders FC vs. Real Salt Lake end in a draw?
            Will Real Salt Lake win on 2026-04-12?

        所以这里改成「按 question 文本匹配」，并显式排除平局盘与远期冠军盘。
        返回的 MarketPrice 里 token 对应的是 **Yes** 那一侧。
        """
        if not self._session:
            return None
        url = f"{self.config.polymarket_gamma_url}/events"
        params = {
            "active": "true",
            "closed": "false",
            "limit": 200,
            # 新的在前：按开赛时间升序会返回几个月前的旧场次（实测匹配不上），
            # 而足球要匹配的是"这几天正在打/即将开打"的比赛。
            "order": "startDate",
            "ascending": "false",
        }
        if tag_id:
            params["tag_id"] = str(tag_id)
        else:
            params["tag_slug"] = tag_slug
        try:
            async with self._session.get(
                url, params=params, proxy=self._proxy, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    logger.warning("Gamma soccer events status %d for tag=%s", resp.status, tag_slug)
                    return None
                events = await resp.json()
        except Exception as exc:
            logger.warning("Gamma soccer events error for tag=%s: %s", tag_slug, exc)
            return None

        if not isinstance(events, list):
            return None

        team_term = re.sub(r"\s+", " ", str(team_term or "")).strip()
        opponent_term = re.sub(r"\s+", " ", str(opponent_term or "")).strip()
        if not team_term:
            return None
        # 只靠 tag + 时间排序取不到「主事件」：实测 tag=mls 的前 40 个事件
        # 全是衍生盘（- More Markets / - Total Corners），含胜平负的主事件不在里面。
        # 所以再按「主队 客队」做一次全站搜索并合并结果。
        if opponent_term:
            search_events = await self._public_search_events(f"{team_term} {opponent_term}")
            if search_events:
                seen = {str(e.get("id")) for e in events}
                events = list(events) + [
                    e for e in search_events if str(e.get("id")) not in seen
                ]
        team_variants = self._name_variants(team_term)
        opponent_variants = self._name_variants(opponent_term)
        team_tokens = self._name_tokens(team_term)
        opponent_tokens = self._name_tokens(opponent_term)
        # 远期盘/其它盘型的排除词：只保留"单场某队赢"。
        # 注意这里比对的是**归一化后**的问句（去标点/重音），所以别写 "draw?"、"o/u" 这种带符号的。
        skip = [
            "champion", "to win the", "ballon", "relegat", "top scorer", "golden boot",
            "end in a draw", "draw", "total ", "o u", "over under", "handicap",
            "correct score", "both teams", "clean sheet", "first goal",
        ]

        best: tuple | None = None   # (score, market_dict, event, outcomes, token_ids, yes_index)
        now = time.time()
        for event in events:
            title = str(event.get("title") or "")
            title_norm = self._normalize_name(title)
            if " vs" not in f" {title_norm}" and " v " not in f" {title_norm}":
                continue
            team_score = self._name_hit_score(title_norm, team_variants, team_tokens)
            if team_score == 0:
                continue
            opp_score = 0
            if opponent_variants or opponent_tokens:
                opp_score = self._name_hit_score(title_norm, opponent_variants, opponent_tokens)
                if opp_score == 0:
                    continue
            # 同一支队可能有多个日期的盘（今天 / 下周），日期最近的优先
            age_hours = self._event_age_hours(event, now)
            if age_hours is None:
                age_bonus = 0
            elif age_hours <= 36:
                age_bonus = 3
            else:
                age_bonus = -2
            score = team_score + opp_score + age_bonus

            for market in event.get("markets", []):
                if market.get("closed") or market.get("active") is False:
                    continue
                question = str(market.get("question") or "")
                question_norm = self._normalize_name(question)
                if any(word in question_norm for word in skip):
                    continue
                # 必须是「Will <队> win」这种问法
                if not question_norm.startswith("will ") and " will " not in f" {question_norm}":
                    continue
                if "win" not in question_norm:
                    continue
                if self._name_hit_score(question_norm, team_variants, team_tokens) == 0:
                    continue

                outcomes = self._parse_json_list(market.get("outcomes", "[]"))
                token_ids = self._parse_json_list(market.get("clobTokenIds", "[]"))
                if len(outcomes) != len(token_ids) or not token_ids:
                    continue
                yes_index = next(
                    (i for i, o in enumerate(outcomes) if str(o).strip().lower() == "yes"),
                    0,
                )
                if best is None or score > best[0]:
                    best = (score, event, market, question, outcomes, token_ids, yes_index)
        if best is None:
            return None
        _score, event, market, question, outcomes, token_ids, yes_index = best
        best_bid = float(market.get("bestBid", 0) or 0)
        best_ask = float(market.get("bestAsk", 1) or 1)
        logger.info(
            "SOCCER market matched: %s | %s (yes_index=%d ask=%.3f score=%d)",
            str(event.get("title") or "")[:60], question[:60], yes_index, best_ask, _score,
        )
        return MarketPrice(
            event_id=str(event.get("id") or ""),
            question=question,
            token_ids=[str(token_ids[yes_index])],
            outcomes=[str(outcomes[yes_index])],
            best_bid=best_bid,
            best_ask=best_ask,
            tick_size=self._tick_size(market),
        )

    @staticmethod
    def _event_age_hours(event: dict, now: float) -> Optional[float]:
        """事件开赛时间距今多少小时（取不到就返回 None，不做时间偏好）。"""
        raw = str(event.get("startDate") or "")
        if not raw:
            return None
        try:
            stamp = datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            return None
        return abs(now - stamp) / 3600.0

    async def search_mlb_markets(self, search_term: str) -> Optional[MarketPrice]:
        return await self.search_sports_markets("mlb", search_term)

    async def search_tennis_markets(self, search_term: str) -> Optional[MarketPrice]:
        return await self.search_sports_markets("tennis", search_term)
