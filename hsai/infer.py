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
from collections import Counter
from typing import Any, Iterable

from .cards import CLASS_CN, CardDB
from .meta import DECK_MAX_CARDS, Archetype, MetaDB
# 数据类 / 纯谓词移到 infer_types，门面显式 re-export 维持原有 import 面。
from .infer_types import (
    _is_aoe, _is_single_removal, TRUST_COVERAGE, TRUST_GAP,
    Candidate, Prediction, RetroTruth,
)

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

class DeckPredictor:
    def __init__(self, meta: MetaDB, cards: CardDB, *, alpha: float = 6.0) -> None:
        self.meta = meta
        self.cards = cards
        self.alpha = alpha
        self._pool_cache: dict[str, dict[str, float]] = {}
        self._dist_cache: dict[str, dict[str, float]] = {}

    def _pool(self, card_class: str) -> dict[str, float]:
        """职业可用牌池的均匀分布（用于平滑，避免「没见过这张牌 -> 概率 0」）。"""
        if card_class in self._pool_cache:
            return self._pool_cache[card_class]
        pool = self.cards.standard_pool(card_class)
        d = {c.id: 1.0 / len(pool) for c in pool} if pool else {}
        self._pool_cache[card_class] = d
        return d

    RARITY_EVIDENCE = {"LEGENDARY": 2.4, "EPIC": 1.8, "RARE": 1.4, "COMMON": 1.0, "FREE": 1.0}

    def _rarity_weight(self, card_id: str | None) -> float:
        """稀有度越高的牌，作为「他是哪套牌」的证据越强。

        传说卡几乎每套牌都只带一张，看到了就很有指向性；
        普通的低费牌则大量重叠，区分度低。
        """
        card = self.cards.get(card_id) if card_id else None
        return self.RARITY_EVIDENCE.get(getattr(card, "rarity", ""), 1.0)

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
        try:
            from .memory import class_counts
            counts = class_counts(opp_class)
        except Exception:
            counts = {}
        scored: list[tuple[Archetype, float]] = []
        for arch in pool:
            dist = self._dist(arch)
            # 同职业最近遇到最多的形态，先验更高
            # 记录的是「掘骨恶鬼」，原型名是「死亡骑士·掘骨恶鬼」，两边都试
            short = arch.name.split("·")[-1]
            seen = max(counts.get(arch.name, 0), counts.get(short, 0))
            log_post = math.log(max(arch.prior, 1e-9)) + 1.0 * math.log(1 + seen)
            for cid, turn in observed:
                # 按牌名认亲：卡组里可能有同一张牌的另一个版本 id
                p = max((dist.get(s, 0.0) for s in self.cards.siblings(cid)), default=0.0)
                if p <= 0:
                    p = dist.get(cid)
                if p is None or p <= 0:
                    # 卡组里完全没有这张牌：按该原型的「意外容忍度」给似然。
                    # 拷贝类卡组会把对手的牌也打出来，不能一票否决。
                    p = arch.unexpected
                rarity = self._rarity_weight(cid)
                log_post += BETA * self._weight(turn) * rarity * math.log(max(p, 1e-9))
            scored.append((arch, log_post))
        # log-sum-exp 归一化
        top = max(s for _, s in scored)
        exps = [(a, math.exp(s - top)) for a, s in scored]
        total = sum(e for _, e in exps)
        return sorted(((a, e / total) for a, e in exps), key=lambda x: -x[1])

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
            if isinstance(item, (list, tuple)):
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

    def retrospect(
        self, opp_class: str, revealed: Iterable[str | tuple[str, int | None]]
    ) -> RetroTruth:
        """打完之后回看：这批牌最像哪套卡组。

        和 `predict()` 的三处区别都是刻意的：
          - 不带早期回合权重（局末不再需要「越早越重要」）
          - 不带先验（问的是「最像谁」，不是「我猜是谁」）
          - 用全部已见牌，不是前几张

        判据是「这个模板能解释多少张已见的牌」。覆盖率比似然更直白：
        它的分子分母都是能数出来的牌数，解释不了多少就是解释不了。
        """
        seen: Counter[str] = Counter()
        for item in revealed:
            # schema 2 存的是 [[card_id, 回合], ...]，从 json 读出来是 list 不是 tuple ——
            # 只判 tuple 的话，list 会被当成 card_id 塞进 Counter，直接抛
            # TypeError: unhashable type: 'list'。回填历史数据时会撞上。
            cid = item[0] if isinstance(item, (list, tuple)) else item
            # 衍生物认成模板牌会抬高 coverage，真值两侧口径要一致
            if cid and self.cards.is_token(cid):
                continue
            if cid:
                seen[cid] += 1
        pool = self.meta.for_class(opp_class) if seen else []
        if not pool:
            return RetroTruth()

        # 只有「至少一个原型认识」的牌才有区分度。偷来的牌、幸运币谁都不认识，
        # 算进分母会同时拉低所有原型的覆盖率，等于集体白扣分。
        known = {cid for cid in seen if self._recognised(cid, pool)}
        considered = sum(seen[c] for c in known)
        if not considered:
            return RetroTruth(unexplained=sorted(seen))

        scored = [(arch, self._explain_count(arch, {c: seen[c] for c in known}))
                  for arch in pool if arch.total() <= DECK_MAX_CARDS]
        if not scored:
            return RetroTruth(unexplained=sorted(seen))
        scored.sort(key=lambda x: (-x[1], x[0].name))
        best, best_n = scored[0]
        rival, rival_n = scored[1] if len(scored) > 1 else (None, 0)
        return RetroTruth(
            archetype_id=best.id,
            name=best.name,
            coverage=best_n / considered,
            explained=best_n,
            considered=considered,
            rival_id=rival.id if rival else "",
            rival_coverage=(rival_n / considered) if rival else 0.0,
            unexplained=[c for c in seen if c not in known],
        )

    def _recognised(self, card_id: str, pool: list[Archetype]) -> bool:
        for s in self.cards.siblings(card_id):
            if any(s in a.cards for a in pool):
                return True
        return False

    def _explain_count(self, arch: Archetype, seen: dict[str, int]) -> int:
        """这个原型能解释多少张已见的牌。

        同一张牌可能有多个版本 id（CORE_/普通版），按牌名归到一起算，
        且总解释数不超过模板里记的张数 —— 否则一张牌被重复计数，
        覆盖率能算到 1 以上。
        """
        used: dict[str, int] = {}
        total = 0
        for cid, n in seen.items():
            fam = self.cards.siblings(cid) or (cid,)
            key = fam[0]
            cap = max((arch.cards.get(s, 0) for s in fam), default=0)
            take = min(n, max(0, cap - used.get(key, 0)))
            if take:
                used[key] = used.get(key, 0) + take
                total += take
        return total

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
