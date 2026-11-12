"""斩杀检测：这回合能不能弄死对面。"""

from __future__ import annotations

from typing import Any

from .cards import CardDB
from .model import Board, hero_power_effect, is_passive_hero_power, parse_effect
from .model_effects import unit_spell_damage
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
        n_swings = min(2, max(0, me.hero_attacks_left))
        if weapon_atk > me.hero_atk:
            # 攻击力全靠这把武器：碎了就打不出第二下这个攻击力
            n_swings = min(n_swings, max(0, me.weapon.hp))
        swings.extend([hero_atk] * n_swings)

    attack = sum(swings)
    taunt_hp = sum((3 if u.divine else u.hp) for u in taunts)
    # 拆墙和实战同一套结算：逐段把最小的攻击喂给嘲讽，圣盾自然只吃一次破盾段，
    # 亡语新召唤的嘲讽在下一轮重取时进墙。剩下没用掉的攻击段才打脸。
    db = cards if cards is not None else board.cards
    work = board.clone()
    work.cards = db
    foe_board = work.sides[1 - side]
    left = sorted(swings)
    used = 0
    while used < len(left):
        walls = [u for u in foe_board.board if u.alive and u.taunt]
        if not walls:
            break
        wall = min(walls, key=lambda u: 3 if u.divine else u.hp)
        work.damage_unit(foe_board, wall, left[used])
        used += 1
    usable = sum(left[used:])

    spell_dmg = 0
    mana = me.mana
    face_spells: list[str] = []
    # 场上法伤对每张直伤法术都加成，和 apply_action / model.apply_effect 同口径
    bonus = sum(unit_spell_damage(u, db) for u in me.board if u.alive)
    # 手牌少，穷举子集挑「这回合买得起、伤害最大」的一组，别按费用升序贪心
    pool: list[tuple[int, int, str]] = []
    for card in me.hand:
        if card is None or not card.is_spell:
            continue
        eff = parse_effect(card)
        is_face = bool(eff.damage and (eff.scope in ("target", "enemy_hero")
                       or (eff.scope == "random_enemy" and not any(u.alive for u in foe.board))))
        net = card.cost - (eff.mana or 0)
        if is_face:
            pool.append((net, eff.damage + bonus, card.name))
            face_spells.append(card.name)
        elif eff.mana:
            pool.append((net, 0, card.name))
    best_dmg, best_cost = 0, 0
    for mask in range(1 << len(pool)):
        cost_sum = dmg_sum = 0
        for i, (net, dmg, _name) in enumerate(pool):
            if mask & (1 << i):
                cost_sum += net
                dmg_sum += dmg
        if cost_sum <= mana and dmg_sum > best_dmg:
            best_dmg, best_cost = dmg_sum, cost_sum
    spell_dmg = best_dmg
    mana -= best_cost

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
        "face_spells": face_spells,
    }

def _lethal_sequence(board: Board, side: int, cards: CardDB) -> list[Action] | None:
    """如果能斩杀，直接贪心拼出一条「真的能打死」的出牌顺序。

    为什么不交给 plan_turn 的评分搜索：评分里「有斩杀」是个阶跃加分，
    每打一下「还能不能斩杀」都会翻一次，导致它打一下就停（这正是之前的 bug）。
    斩杀是确定性问题，直接用伤害当目标函数更可靠。
    """
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
