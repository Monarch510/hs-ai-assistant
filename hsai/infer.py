"""对手卡组预判。

思路（朴素贝叶斯 + 职业先验）：

   P(卡组 A | 看到牌集合 S) ∝ P(A) · Π_{c ∈ S} P(c | A)^{w_c}

P(c | A) 由「标志牌张数 + 职业牌池平滑」构成：

   P(c | A) = (n_A(c) + α·p_pool(c)) / (Σn_A + α)

α 越大越保守（更信任职业牌池），w_c 是按回合的权重 —— 前几回合打出的牌
更能定义卡组形态。这是可解释的近似模型，不是拟合出来的神经网络：
每个结论都能追到「因为看到了哪张牌」。

除了「对面是什么卡组」，这里还回答三个更实用的问题：
  1. 对方手牌里大概率有什么（下一回合的威胁）
  2. 对方手上有没有清场/单解（铺不铺场、敢不敢下大怪）
  3. 对方还可能抽到什么（斩杀线判断）
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

from .cards import CLASS_CN, Card, CardDB
from .meta import Archetype, MetaDB

# 观测权重：越早打出越能说明形态；太晚的牌可能只是补位
EARLY_TURNS = 3
W_EARLY = 1.35
W_MID = 1.0
W_LATE = 0.85
LATE_TURN = 8

# 温度系数：<1 会让后验更保守（避免两张牌就把置信度拉到 99%）
BETA = 0.32
# 置信度上限：再像也不说「百分之百」，因为卡组会被改牌
CONF_CAP = 0.95


def _is_aoe(card: Card) -> bool:
    if card.type != "SPELL" or not card.text:
        return False
    t = card.text
    # 清场不一定造成伤害：「消灭所有随从」（如绝命乱斗）同样是清场
    return ("所有" in t and ("伤害" in t or "消灭" in t)) or "全场" in t


def _is_single_removal(card: Card) -> bool:
    if card.type != "SPELL" or not card.text:
        return False
    t = card.text
    # 「消灭所有随从」是清场，不是单解 —— 否则同一张牌会同时被算进
    # aoe_risk 和 removal_risk，界面上两行互相矛盾
    if "所有" in t:
        return False
    return "消灭" in t or ("造成" in t and "点伤害" in t)


def _is_buff(card: Card) -> bool:
    return card.type == "SPELL" and card.text and ("+" in card.text and ("攻击力" in card.text or "生命值" in card.text))


def _hypergeom_at_least_one(copies: int, population: int, draws: int) -> float:
    """population 里放了 copies 张，抽 draws 张，至少抽到一张的概率。"""
    if copies <= 0 or draws <= 0:
        return 0.0
    population = max(population, copies, draws)
    if copies >= population:
        return 1.0
    try:
        miss = math.comb(population - copies, draws) / math.comb(population, draws)
    except (ValueError, ZeroDivisionError):
        return 0.0
    return max(0.0, min(1.0, 1.0 - miss))


@dataclass
class Candidate:
    id: str
    name: str
    confidence: float
    class_cn: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class Prediction:
    name: str = "未知"
    archetype_id: str = ""
    confidence: float = 0.0
    candidates: list[Candidate] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    key_cards: list[dict[str, Any]] = field(default_factory=list)
    next_threats: list[str] = field(default_factory=list)
    likely_hand: list[dict[str, Any]] = field(default_factory=list)
    aoe_risk: float = 0.0
    removal_risk: float = 0.0
    remaining: dict[str, float] = field(default_factory=dict)
    prior_only: bool = False
    note: str = ""
    source: str = ""
    estimated: bool = False       # 数字是不是「估的」（库里没这套牌时）

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "id": self.archetype_id,
            "confidence": round(self.confidence, 3),
            "candidates": [
                {"id": c.id, "name": c.name, "confidence": round(c.confidence, 3), "tags": c.tags}
                for c in self.candidates
            ],
            "evidence": self.evidence,
            "key_cards": self.key_cards,
            "next_threats": self.next_threats,
            "likely_hand": self.likely_hand,
            "aoe_risk": round(self.aoe_risk, 3),
            "removal_risk": round(self.removal_risk, 3),
            "prior_only": self.prior_only,
            "note": self.note,
            "source": self.source,
            "estimated": self.estimated,
        }


class DeckPredictor:
    def __init__(self, meta: MetaDB, cards: CardDB, *, alpha: float = 6.0) -> None:
        self.meta = meta
        self.cards = cards
        self.alpha = alpha
        self._pool_cache: dict[str, dict[str, float]] = {}
        self._dist_cache: dict[str, dict[str, float]] = {}

    # ------------------------------------------------------------ 概率模型
    def _pool(self, card_class: str) -> dict[str, float]:
        """职业可用牌池的均匀分布（用于平滑，避免「没见过这张牌 -> 概率 0」）。"""
        if card_class in self._pool_cache:
            return self._pool_cache[card_class]
        pool = self.cards.standard_pool(card_class)
        d = {c.id: 1.0 / len(pool) for c in pool} if pool else {}
        self._pool_cache[card_class] = d
        return d

    def _dist(self, arch: Archetype) -> dict[str, float]:
        if arch.id in self._dist_cache:
            return self._dist_cache[arch.id]
        pool = self._pool(arch.card_class)
        total = arch.total()
        denom = total + self.alpha
        dist: dict[str, float] = {}
        for cid, n in arch.cards.items():
            dist[cid] = (n + self.alpha * pool.get(cid, 1e-6)) / denom
        # 没有列入模板的池内卡牌：只剩平滑项
        for cid, p in pool.items():
            if cid not in dist:
                dist[cid] = (self.alpha * p) / denom
        self._dist_cache[arch.id] = dist
        return dist

    def _weight(self, turn: int | None) -> float:
        if turn is None:
            return W_MID
        if turn <= EARLY_TURNS:
            return W_EARLY
        if turn >= LATE_TURN:
            return W_LATE
        return W_MID

    def posteriors(
        self,
        opp_class: str,
        observed: list[tuple[str, int | None]],
    ) -> list[tuple[Archetype, float]]:
        pool = self.meta.for_class(opp_class)
        if not pool:
            return []
        scored: list[tuple[Archetype, float]] = []
        for arch in pool:
            dist = self._dist(arch)
            log_post = math.log(max(arch.prior, 1e-9))
            for cid, turn in observed:
                # 按牌名认亲：卡组里可能有同一张牌的另一个版本 id
                p = max((dist.get(s, 0.0) for s in self.cards.siblings(cid)), default=0.0)
                if p <= 0:
                    p = dist.get(cid)
                if p is None or p <= 0:
                    # 卡组里完全没有这张牌：按该原型的「意外容忍度」给似然。
                    # 拷贝类卡组会把对手的牌也打出来，不能一票否决。
                    p = arch.unexpected
                log_post += BETA * self._weight(turn) * math.log(max(p, 1e-9))
            scored.append((arch, log_post))
        # log-sum-exp 归一化
        top = max(s for _, s in scored)
        exps = [(a, math.exp(s - top)) for a, s in scored]
        total = sum(e for _, e in exps)
        return sorted(((a, e / total) for a, e in exps), key=lambda x: -x[1])

    # ------------------------------------------------------------ 主入口
    def predict(
        self,
        opp_class: str,
        observed: Iterable[str | tuple[str, int | None]],
        *,
        turn: int | None = None,
        hand_count: int = 0,
        deck_count: int = 20,
        opp_mana: int = 0,
    ) -> Prediction:
        obs: list[tuple[str, int | None]] = []
        for item in observed:
            if isinstance(item, tuple):
                obs.append((item[0], item[1]))
            else:
                obs.append((item, turn))

        post = self.posteriors(opp_class, obs)
        if not post:
            return Prediction(name="未知卡组", note="该职业没有可用的卡组原型，先导入或自举 meta", prior_only=True)

        # 该职业库里只有一套牌时，1.0 的置信度是假的（没有比较对象），封顶
        if len(post) == 1 and not obs:
            post = [(post[0][0], min(post[0][1], 0.5))]
        elif len(post) == 1:
            post = [(post[0][0], min(post[0][1], 0.7))]
        if post[0][1] > CONF_CAP and len(post) > 1:
            rest = post[1:]
            rest_sum = sum(p for _, p in rest) or 1.0
            post = [(post[0][0], CONF_CAP)] + [(a, (1 - CONF_CAP) * p / rest_sum) for a, p in rest]

        best, best_p = post[0]

        # 「见过的牌一张都不在任何原型里」= 这个卡组我们不认识。
        # 这时候给出 100% 是假自信：只因该职业只有一个候选就能拿满分。
        # 宁可说「未识别」，也不要把猜测包装成结论。
        matched = [cid for cid, _ in obs
                   if any(any(s in arch.cards for s in self.cards.siblings(cid))
                          for arch, _ in post)]
        # 只看到一张「谁都没有的牌」（比如幸运币）不足以判定「未知卡组」——
        # 那多半是中性牌。至少两张都对不上，才说这套牌我们没见过。
        discriminative = bool(matched) or len(obs) < 2
        if obs and not discriminative:
            pred = Prediction(
                name="未知卡组",
                archetype_id="",
                confidence=0.0,
                prior_only=False,
                candidates=[Candidate(a.id, a.name, p, a.class_cn, a.tags) for a, p in post[:2]],
                note="看到的牌都不在任何已知卡组原型里，可能是新卡组",
            )
            pred.evidence = [f"对手职业：{CLASS_CN.get(opp_class, opp_class)}"] + [
                f"已出现 {self.cards.display_name(cid)}" for cid, _ in obs[:4]
            ]
            mix, has_lib = self.class_mix(opp_class)
            pred.aoe_risk = self._risk(mix, deck_count, hand_count, _is_aoe, opp_mana)
            pred.removal_risk = self._risk(mix, deck_count, hand_count, _is_single_removal, opp_mana)
            pred.likely_hand = (
                self._likely_hand(mix, hand_count, deck_count, opp_mana) if has_lib else []
            )
            pred.estimated = not has_lib
            return pred
        pred = Prediction(
            name=best.name,
            archetype_id=best.id,
            confidence=best_p,
            prior_only=not obs,
            candidates=[Candidate(a.id, a.name, p, a.class_cn, a.tags) for a, p in post[:3]],
            source=best.source,
        )
        # 后续所有「对方可能有什么」的估计都按后验做混合，
        # 只盯着第一名会让 30% 概率的另一种形态完全消失。
        mixture: dict[str, float] = {}
        for arch, p in post[:3]:
            for cid, v in self._remaining(arch, obs).items():
                mixture[cid] = mixture.get(cid, 0.0) + p * v

        pred.evidence = self._explain(opp_class, obs, best, post)
        pred.key_cards = self._key_cards(best, obs)
        pred.remaining = mixture
        pred.next_threats = self._threats(mixture, opp_mana)
        pred.likely_hand = self._likely_hand(mixture, hand_count, deck_count, opp_mana)
        pred.aoe_risk = self._risk(mixture, deck_count, hand_count, _is_aoe, opp_mana)
        pred.removal_risk = self._risk(mixture, deck_count, hand_count, _is_single_removal, opp_mana)
        if pred.prior_only:
            pred.note = "还没有足够证据，这里只是该职业的常见形态先验"
        return pred

    # ------------------------------------------------------------ 解释
    def _explain(self, opp_class: str, obs, best: Archetype, post) -> list[str]:
        from .cards import CLASS_CN

        out = [f"对手职业：{CLASS_CN.get(opp_class, opp_class)}"]
        if not obs:
            out.append("尚未出现可识别的牌，等待信息")
            return out
        for cid, turn in obs:
            card = self.cards.get(cid)
            name = card.name if card else cid
            # 这张牌最偏向哪个原型
            lean = None
            lean_p = 0.0
            for arch in self.meta.for_class(opp_class):
                n = max((arch.cards.get(s, 0) for s in self.cards.siblings(cid)), default=0)
                if n > lean_p:
                    lean, lean_p = arch, n
            if lean is not None and lean_p > 0:
                out.append(f"已出现 {name} → {lean.name}（标志牌 ×{int(lean_p)}）")
            else:
                out.append(f"已出现 {name}（不在已知标志牌里，可能是补位卡）")
        return out

    def _key_cards(self, arch: Archetype, obs) -> list[dict[str, Any]]:
        seen = {c for c, _ in obs}
        rows = []
        for cid, n in arch.cards.items():
            if cid in seen:
                continue
            card = self.cards.get(cid)
            if card is None:
                continue
            rows.append(
                {
                    "id": cid,
                    "name": card.name,
                    "cost": card.cost,
                    "atk": card.atk,
                    "health": card.health,
                    "count": n,
                    "text": card.text[:40],
                }
            )
        rows.sort(key=lambda r: (-r["count"], r["cost"]))
        return rows[:5]

    def _remaining(self, arch: Archetype, obs) -> dict[str, float]:
        seen: dict[str, int] = {}
        for cid, _ in obs:
            for s in self.cards.siblings(cid):
                seen[s] = seen.get(s, 0) + 1
        out: dict[str, float] = {}
        for cid, n in arch.cards.items():
            left = max(0, n - seen.get(cid, 0))
            if left:
                out[cid] = float(left)
        return out

    def _threats(self, remaining: dict[str, float], opp_mana: int) -> list[str]:
        rows = []
        for cid, copies in remaining.items():
            card = self.cards.get(cid)
            if card is None:
                continue
            if card.cost <= opp_mana + 3:
                tag = ""
                if card.type == "MINION":
                    tag = f" ({card.atk}/{card.health})"
                elif _is_aoe(card):
                    tag = " [清场]"
                elif _is_single_removal(card):
                    tag = " [单解]"
                rows.append((card.cost, f"{card.cost} 费：{card.name}{tag}"))
        rows.sort()
        return [t for _, t in rows[:4]]

    def _likely_hand(
        self, remaining: dict[str, float], hand_count: int, deck_count: int, mana: int = 99
    ) -> list[dict[str, Any]]:
        """他**这回合最可能打出来**的牌。

        旧版只按「手上有这张的概率」排序，而同张数的牌概率完全一样
        （2 张的永远都是 31%），等于没有区分度。现在改成：
          1. 他这回合费用打不出来的，大幅降权
          2. AOE / 单解这类「针对牌」加权——那才是你会关心的
        """
        if hand_count <= 0:
            return []
        pool = deck_count + hand_count
        out = []
        for cid, n in remaining.items():
            card = self.cards.get(cid)
            if card is None or n < 0.15:
                continue
            # 混合后的张数常是小数（例如 0.45 张），取整会把它抹成 0，
            # 结果「不太可能但很关键」的牌整类消失。用泊松近似处理小数。
            p = 1.0 - math.exp(-max(n, 0.0) * hand_count / pool)
            if p < 0.18:
                continue
            if card.cost is not None and card.cost > mana:
                continue          # 这回合根本打不出来，不列
            impact = 1.6 if _is_aoe(card) else 1.3 if _is_single_removal(card) else 1.0
            out.append(
                {
                    "name": card.name,
                    "cost": card.cost,
                    "p": round(p, 2),
                    "impact": impact,
                    "score": round(p * impact, 3),
                    "type_cn": card.type_cn,
                }
            )
        out.sort(key=lambda d: -d["score"])
        return out[:3]

    def _risk(
        self, remaining: dict[str, float], deck_count: int, hand_count: int, test,
        mana: int | None = None,
    ) -> float:
        """他**这回合**能打出这张牌的概率。

        口径：当前手牌 + 本回合抽的那一张。所以抽牌数用 hand_count + 1，
        而不是只算「现在手上有没有」——决策是针对这一回合做的。
        """
        copies = 0.0
        pool = max(deck_count + hand_count, 1)
        draws = max(hand_count, 0) + 1
        for cid, n in remaining.items():
            card = self.cards.get(cid)
            if card is None or n < 0.05 or not test(card):
                continue
            if mana is not None and card.cost is not None and card.cost > mana:
                continue          # 他这回合的费用根本打不出来，不算这张牌的威胁
            copies += n
        # 先把所有相关牌的张数加起来，再算一次「至少摸到一张」。
        # 逐张相加会把多个小概率叠成 100%，那是错的。
        return min(0.99, 1.0 - math.exp(-copies * draws / pool))

    def class_mix(self, opp_class: str) -> tuple[dict[str, float], bool]:
        """该职业所有原型的「期望张数」混合分布，用于认不出具体卡组时兜底。

        返回 (card_id -> 期望张数, 是否有真实卡组库)。没有库时按职业牌池
        里每张 AOE/单解「平均 0.5 张」粗估 —— 数字会加「≈」提示是估的。
        """
        archs = self.meta.for_class(opp_class)
        if archs:
            mix: dict[str, float] = {}
            for a in archs:
                for cid, n in a.cards.items():
                    mix[cid] = mix.get(cid, 0.0) + a.prior * n
            return mix, True
        mix = {c.id: 0.5 for c in self.cards.standard_pool(opp_class)
               if _is_aoe(c) or _is_single_removal(c)}
        return mix, False
