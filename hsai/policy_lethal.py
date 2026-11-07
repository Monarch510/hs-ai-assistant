"""斩杀检测：这回合能不能弄死对面。"""

from __future__ import annotations

from typing import Any

from .cards import CardDB
from .model import Board, hero_power_effect, is_passive_hero_power, parse_effect
from .policy_actions import Action, apply_action, legal_actions


def max_face_damage(board: Board, side: int, cards: CardDB | None = None) -> dict[str, Any]:
    """估算本回合能打到对面脸上的最大伤害（已考虑嘲讽与费用）。"""
    me, foe = board.sides[side], board.sides[1 - side]
    taunts = [u for u in foe.board if u.alive and u.taunt]

    # 攻击是「整段」消耗的：一个 8/8 撞 2/5 嘲讽，溢出的 3 点不会转到脸上。
    # 所以不能简单用「总攻击力 - 嘲讽血量」，要把攻击段一个个分配出去。
    swings: list[int] = []
    for u in me.board:
        if not u.alive or u.atk <= 0 or u.frozen or u.attacks_left <= 0 or u.dormant:
            continue
        # 突袭刚入场的这只本回合只能打随从，别把它的攻击算成打脸伤害，
        # 否则「能斩杀」会虚报；这里和 legal_actions 的口径必须一致
        if u.rush_only:
            continue
        hits = 2 if (u.windfury and u.attacks_left >= 2) else 1
        swings.extend([u.atk] * hits)
    # 英雄自己也是攻击者：有武器时 hero_atk 通常已经等于武器攻击力，
    # 取 max 而不是相加，否则同一把武器会被算两次。
    weapon_atk = me.weapon.atk if (me.weapon and (me.weapon.hp or 0) > 0) else 0
    hero_atk = max(me.hero_atk, weapon_atk)
    if hero_atk > 0:
        # 0 次表示本回合已经打过，不能当成「未知按 1 次算」
        swings.extend([hero_atk] * min(2, max(0, me.hero_attacks_left)))

    attack = sum(swings)
    taunt_hp = sum((3 if u.divine else u.hp) for u in taunts)
    left = sorted(swings)
    for t in sorted(taunts, key=lambda u: 3 if u.divine else u.hp):
        need = 3 if t.divine else t.hp
        idx = next((i for i, sw in enumerate(left) if sw >= need), None)
        if idx is None:
            left = []          # 过不去墙：剩下的攻击全被嘲讽吃掉
            break
        left.pop(idx)          # 这一次攻击被嘲讽消耗掉，溢出伤害不转脸
    usable = sum(left)

    spell_dmg = 0
    mana = me.mana
    face_spells = []
    # 场上法伤对每张直伤法术都加成，和 apply_action / model.apply_effect 同口径
    bonus = sum(u.spell_damage for u in me.board if u.alive)
    for card in sorted(me.hand, key=lambda c: c.cost):
        if not card.is_spell or card.cost > mana:
            continue
        eff = parse_effect(card)
        if eff.damage and eff.scope in ("target", "enemy_hero", "random_enemy"):
            face_spells.append((card.cost, eff.damage, card.name))
    for cost, dmg, name in face_spells:
        if cost <= mana:
            mana -= cost
            spell_dmg += dmg + bonus

    hp_name, hp_cost, hp_eff = hero_power_effect(me.hero_class, me.hero_power)
    hero_power = 0
    if (not is_passive_hero_power(me.hero_power) and not me.hero_power_used
            and mana >= hp_cost and hp_eff.damage):
        hero_power = hp_eff.damage

    total = usable + spell_dmg + hero_power
    need = foe.hero_hp + foe.armor
    return {
        "attack": attack,
        "taunt_block": taunt_hp,
        "spell": spell_dmg,
        "hero_power": hero_power,
        "damage": total,
        "need": need,
        "lethal": total >= need and need > 0,
        "has_taunt": bool(taunts),
        "face_spells": [n for _, _, n in face_spells],
    }

def _lethal_sequence(board: Board, side: int, cards: CardDB) -> list[Action] | None:
    """如果能斩杀，直接贪心拼出一条「真的能打死」的出牌顺序。

    为什么不交给 plan_turn 的评分搜索：评分里「有斩杀」是个阶跃加分，
    每打一下「还能不能斩杀」都会翻一次，导致它打一下就停（这正是之前的 bug）。
    斩杀是确定性问题，直接用伤害当目标函数更可靠。
    """
    need_check = max_face_damage(board, side, cards)
    if not need_check["lethal"]:
        return None

    work = board.clone()
    work.active = side
    seq: list[Action] = []
    for _ in range(12):
        foe = work.sides[1 - side]
        if foe.hero_hp <= 0:
            break
        best: tuple[float, Action, Board] | None = None
        for act in legal_actions(work, side, cards):
            if act.kind == "end":
                continue
            nb = apply_action(work, side, act, cards)
            nf = nb.sides[1 - side]
            dealt = (foe.hero_hp + foe.armor) - (nf.hero_hp + nf.armor)
            taunts_before = sum(1 for u in foe.board if u.alive and u.taunt)
            taunts_after = sum(1 for u in nf.board if u.alive and u.taunt)
            score = dealt * 10 + (taunts_before - taunts_after) * 4 + (1000 if nf.hero_hp <= 0 else 0)
            if best is None or score > best[0]:
                best = (score, act, nb)
        if best is None or best[0] <= 0:
            break
        seq.append(best[1])
        work = best[2]

    return seq if work.sides[1 - side].hero_hp <= 0 else None
