"""发现 / 抉择：从多个选项里挑一个。"""

from __future__ import annotations

from .cards import CardDB
from .model import Board, Effect, parse_effect
from .model_effects import unit_spell_damage
from .policy_consts import CTHUN_MULTIPLIER, CTHUN_SKILL_NAMES
from .policy_race import race_plan


def _deck_theme(deck_cards, cards) -> str:
    """从卡组构成判断这套牌靠什么赢：法伤 / 低费 / 随从。"""
    if not deck_cards:
        return ""
    total = len(deck_cards)
    spells = sum(1 for c in deck_cards if c is not None and getattr(c, "type", "") == "SPELL")
    cheap = sum(1 for c in deck_cards if c is not None and (c.cost or 0) <= 2)
    minions = sum(1 for c in deck_cards if c is not None and getattr(c, "type", "") == "MINION")
    if spells / total >= 0.45:
        return "spell"
    if cheap / total >= 0.5:
        return "aggro"
    if minions / total >= 0.6:
        return "minion"
    return ""

def _spell_damage_on_board(board: Board, side: int, cards) -> int:
    """场上「法术伤害 +N」的总量。"""
    return sum(unit_spell_damage(u, cards) for u in board.sides[side].board if u.alive)

def score_choice(card, effect, board: Board, side: int, mana: int,
                 *, deck_theme: str = "", spell_damage: int = 0,
                 hand_spells: int = 0, opp_fast: bool = False,
                 tripled: bool = False) -> tuple[float, str]:
    """给一个发现/抉择选项打分，返回 (分数, 理由)。

    分六层，从上到下依次叠加：当回合可打 → 立即收益 → 卡组主题
    → 场上加成 → 手牌组合 → 对手形态。
    """
    score = 0.0
    reasons: list[str] = []
    foe = board.sides[1 - side]
    cost = getattr(card, "cost", 0) or 0
    is_spell = getattr(card, "type", "") == "SPELL"
    is_minion = getattr(card, "type", "") == "MINION"

    if cost <= mana:
        score += 2.0
        reasons.append("当回合可打")
    else:
        score -= 2.0

    mult = CTHUN_MULTIPLIER if tripled else 1
    if effect.known:
        if effect.damage and effect.scope in ("enemy", "target", "enemy_minions"):
            killable = [u for u in foe.board
                        if u.alive and u.hp <= effect.damage * mult + spell_damage]
            if killable:
                # 单体伤害只能解掉一个；群伤才算全部
                n = len(killable) if effect.aoe else 1
                best = max(killable, key=lambda u: u.atk)
                score += 3.0 * n + best.atk * 0.3
                reasons.append(f"能解掉{n}个随从（最痛 {best.atk} 攻）")
        if effect.destroy:
            score += 3.0
            reasons.append("硬解")
        if effect.draw:
            score += 1.8 * effect.draw
            reasons.append(f"过{effect.draw}张")
        if effect.heal:
            score += 0.8
            reasons.append("回血")
        if effect.armor:
            score += 0.7
            reasons.append("加甲")
        if (effect.summon_atk or effect.summon_hp) and effect.summon_count:
            # 召唤类（克苏恩的仆从：两个 2/2 嘲讽）之前完全没算分
            n = max(1, int(effect.summon_count))
            per = effect.summon_atk * 1.25 * mult + effect.summon_hp * mult
            if "嘲讽" in ((getattr(card, "text", "") or "")):
                per *= 1.2
            if per > 0:
                score += per * n * 0.7
                reasons.append(f"召唤 {n} 个 {effect.summon_atk * mult}/{effect.summon_hp * mult}")

        if effect.mana:
            score += 1.2
            reasons.append("加费")
        if effect.buff_atk or effect.buff_hp:
            # 加 buff：按「每个随从获得的属性价值 × 能加几个」算
            targets_n = min(2, len([u for u in board.sides[side].board if u.alive]))
            per = (effect.buff_atk or 0) * 1.25 * mult + (effect.buff_hp or 0) * mult
            if targets_n and per > 0:
                score += per * targets_n * 0.6
                reasons.append(f"{targets_n} 个随从 +{ (effect.buff_atk or 0)*mult }/+{ (effect.buff_hp or 0)*mult }")
    if is_minion:
        atk = getattr(card, "atk", 0) or 0
        hp = getattr(card, "health", 0) or 0
        value = (atk + hp) / max(1, cost)
        score += min(2.5, value * 0.8)

    if deck_theme == "spell" and is_spell:
        score += 0.8
    elif deck_theme == "aggro" and cost <= 2:
        score += 0.6
    elif deck_theme == "minion" and is_minion:
        score += 0.5

    if spell_damage > 0 and is_spell and effect.damage:
        score += 0.5 * spell_damage

    # 手牌组合：手里法术多、场上又有法伤，法伤随从更值
    if hand_spells >= 3 and spell_damage > 0 and is_minion and "法术伤害" in (card.text or ""):
        score += 0.8
        reasons.append("和手牌法术配合")

    # 对手形态：快攻优先解场/回血，控制优先大身材/过牌
    if opp_fast:
        # 认不出的牌不因为「可能是解牌」加分，否则快攻对局会偏向未建模的牌
        if effect.known and (effect.damage or effect.destroy):
            score += 0.8
        if effect.heal or effect.armor:
            score += 0.8
    else:
        if is_minion and (getattr(card, "atk", 0) or 0) >= 5:
            score += 0.6
        if effect.draw:
            score += 0.6

    return score, "，".join(reasons[:3])

def _cthun_choice(board: Board, side: int, options: list, cards: CardDB
) -> tuple[int, str] | None:
    """克苏恩四技能：按优先级排序，同档时优先「还没用过」的那个。

    优先级（用户定的）：
      ① 抢血 / 对面随从多 → 愤怒
      ② 我方 ≥2 个随从   → 领域
      ③ 缺血 / 对面有大随从 → 凝视
      ④ 其余             → 仆从
    用过的技能照样能选，只是排在同档未用过的后面。
    """
    me, foe = board.sides[side], board.sides[1 - side]
    names = [str(o.get("name") or "") for o in options]
    used = set(getattr(me, "cthun_used", set()) or set())

    race = race_plan(board, side, cards, 1.5)
    my_minions = [u for u in me.board if u.alive]
    foe_minions = [u for u in foe.board if u.alive]
    foe_atk = sum(u.atk for u in foe_minions if not u.dormant)
    need_heal = me.hero_hp <= max(12, foe_atk + 4)
    big = max(foe_minions, key=lambda u: u.atk, default=None)

    tiers: dict[str, tuple[int, str]] = {}
    if race["mode"] == "race":
        tiers["克苏恩的愤怒"] = (1, "抢血：愤怒铺伤害")
    if len(foe_minions) >= 3:
        tiers["克苏恩的愤怒"] = (1, "解场：愤怒铺开打多个随从")
    if len(my_minions) >= 2:
        tiers["克苏恩的领域"] = (2, "两个随从吃满 +2/+2")
    if need_heal or (big is not None and big.atk >= 4):
        tiers["克苏恩的凝视"] = (3, "凝视：解掉大随从并回血")
    tiers.setdefault("克苏恩的仆从", (4, "仆从：两个 2/2 嘲讽铺场护脸"))

    best: tuple[int, int, int, str] | None = None
    for i, n in enumerate(names):
        if not n:
            continue
        tier, why = tiers.get(n, (5, "克苏恩技能"))
        rank = 0 if n not in used else 1          # 同档时未用过的优先
        cand = (tier, rank, i, why)
        if best is None or cand[:3] < best[:3]:
            best = cand
    if best is None:
        return None
    return best[2], best[3]

def pick_choice(options: list, board: Board, cards, side: int, mana: int,
                *, deck_cards=None, opp_fast: bool = False,
                tripled: bool = False) -> tuple[int, str, float]:
    """从发现/抉择选项里挑一个。返回 (下标, 理由, 与第二名的分差)。"""
    if not options:
        return 0, "", 0.0
    # 克苏恩技能的选择里会混进打出的那张源牌，它不是候选
    # 过滤时保留原始下标，否则返回的 index 会对不上调用方传进来的列表
    pairs = [(idx, o) for idx, o in enumerate(options)]
    skill_pairs = [(idx, o) for idx, o in pairs if str(o.get("name")) in CTHUN_SKILL_NAMES]
    if len(skill_pairs) >= 2:
        pairs = skill_pairs
    theme = _deck_theme(deck_cards or [], cards)
    spell_damage = _spell_damage_on_board(board, side, cards)
    hand_spells = sum(1 for c in board.sides[side].hand
                      if c is not None and getattr(c, "type", "") == "SPELL")
    scored: list[tuple[float, int, str]] = []
    # 克苏恩四技能：按用户定的优先级选，不走打分
    if len(skill_pairs) >= 2:
        ruled = _cthun_choice(board, side, [o for _, o in skill_pairs], cards)
        if ruled is not None:
            pick, why = ruled
            orig_idx = skill_pairs[pick][0]
            return orig_idx, why, 9.9

    for idx, opt in pairs:
        card = cards.by_name_or_id(str(opt.get("name") or ""))
        effect = parse_effect(card) if card is not None else Effect(known=False)
        if card is None:
            scored.append((-9.0, idx, "认不出这张牌"))
            continue
        score, why = score_choice(card, effect, board, side, mana,
                                  deck_theme=theme, spell_damage=spell_damage,
                                  hand_spells=hand_spells, opp_fast=opp_fast,
                                  tripled=tripled)
        scored.append((score, idx, why))
    scored.sort(key=lambda x: -x[0])
    best_score, best_idx, best_why = scored[0]
    gap = best_score - scored[1][0] if len(scored) > 1 else 99.0
    return best_idx, best_why, gap
