"""建议引擎：这一回合怎么打。

做三件事：
1. 斩杀检测 —— 先算「这回合能不能弄死对面」，这是最有用也最容易算准的。
2. 动作评分 —— 把每个合法动作模拟一遍，用 model.evaluate 比较局面优劣。
3. 组合成一条「本回合路线」—— 贪心地一步步来，形成可执行的顺序建议。

它是 1 层搜索（每步只看一步后果），不是 MCTS。所以它擅长：
斩杀、交换、站场、节奏；不擅长：多回合组合技、需要保留资源的运营。
这个边界会如实写在建议理由里，不假装懂更多。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

from .cards import Card, CardDB
from .infer import Prediction
from .model import (
    Board,
    Effect,
    SideState,
    Unit,
    evaluate,
    hero_power_effect,
    parse_effect,
)

EPS = 0.35          # 提升小于这个值就不算改进

# 用户定的优先级：斩杀 > 对场面有帮助 > 过牌 > 发现 > 防斩杀。
# 用「分类加分」实现：它不会推翻明显更好的选择，但同档收益时按这个顺序偏向。
PRIORITY_BONUS = {
    "lethal": 1000.0,
    "board": 3.0,
    "draw": 1.6,
    "discover": 1.0,
    "defend": 0.6,
    "other": 0.0,
}


def categorize(action: Action, before: Board, after: Board, side: int) -> str:
    """给一个动作归类，用于优先级排序。"""
    if after.sides[1 - side].hero_hp <= 0:
        return "lethal"
    killed = sum(1 for u in before.sides[1 - side].board
                 if u.alive and not any(x is u or x.card_id == u.card_id for x in after.sides[1 - side].board))
    if killed:
        return "board"
    if action.kind == "play":
        card = before.sides[side].hand[action.card_idx] if action.card_idx < len(before.sides[side].hand) else None
        if card is not None:
            eff = parse_effect(card)
            if eff.draw:
                return "draw"
            if "发现" in (card.text or ""):
                return "discover"
            if eff.heal or "护甲" in (card.text or ""):
                return "defend"
        return "board"      # 出随从/其它牌都算推进场面
    if action.kind == "hero_power":
        return "defend"
    if action.kind == "attack":
        return "board"
    return "other"
MAX_STEPS = 6


# ---------------------------------------------------------------- 动作

@dataclass
class Action:
    kind: str                      # play | attack | hero_power | end
    card_idx: int = -1
    unit_idx: int = -1
    target: str = "none"           # enemy_hero | enemy:<i> | friendly:<i> | none
    cost: int = 0
    desc: str = ""
    card_id: str = ""

    def key(self) -> tuple:
        return (self.kind, self.card_idx, self.unit_idx, self.target)


def _enemy_units(board: Board, side: int) -> list[Unit]:
    return [u for u in board.sides[1 - side].board if u.alive]


def legal_actions(board: Board, side: int, cards: CardDB) -> list[Action]:
    me, foe = board.sides[side], board.sides[1 - side]
    acts: list[Action] = []

    taunts = [u for u in foe.board if u.alive and u.taunt]
    for i, u in enumerate(me.board):
        if not u.alive or u.atk <= 0 or u.attacks_left <= 0 or u.frozen:
            continue
        if taunts:
            for j, t in enumerate(foe.board):
                if t.alive and t.taunt:
                    acts.append(Action("attack", unit_idx=i, target=f"enemy:{j}",
                                       desc=f"{u.name} 攻击嘲讽 {t.name}"))
        else:
            acts.append(Action("attack", unit_idx=i, target="enemy_hero",
                               desc=f"{u.name} 攻击英雄"))
            for j, t in enumerate(foe.board):
                if t.alive:
                    acts.append(Action("attack", unit_idx=i, target=f"enemy:{j}",
                                       desc=f"{u.name} 攻击 {t.name}"))

    full_board = len([u for u in me.board if u.alive]) >= 7
    for i, card in enumerate(me.hand):
        if card.cost > me.mana:
            continue
        eff = parse_effect(card)
        if card.is_minion and full_board:
            continue
        if card.is_spell and eff.damage and eff.scope == "target":
            # 直伤法术既能打随从也能打脸 —— 只有打随从是之前漏的，导致
            # 手里握着能斩杀的火球却永远不推荐
            for j, t in enumerate(foe.board):
                if t.alive:
                    acts.append(Action("play", card_idx=i, target=f"enemy:{j}",
                                       cost=card.cost, desc=f"用 {card.name} 打 {t.name}", card_id=card.id))
            acts.append(Action("play", card_idx=i, target="enemy_hero",
                               cost=card.cost, desc=f"用 {card.name} 打脸", card_id=card.id))
            continue
        if card.is_spell and eff.destroy and eff.scope == "target":
            for j, t in enumerate(foe.board):
                if t.alive:
                    acts.append(Action("play", card_idx=i, target=f"enemy:{j}",
                                       cost=card.cost, desc=f"用 {card.name} 解 {t.name}", card_id=card.id))
            continue
        if card.is_spell and (eff.buff_atk or eff.buff_hp or eff.heal):
            if any(u.alive for u in me.board):
                for j, t in enumerate(me.board):
                    if t.alive:
                        acts.append(Action("play", card_idx=i, target=f"friendly:{j}",
                                           cost=card.cost, desc=f"用 {card.name} 强化 {t.name}", card_id=card.id))
                continue
        acts.append(Action("play", card_idx=i, cost=card.cost, desc=f"打出 {card.name}", card_id=card.id))

    name, hp_cost, hp_eff = hero_power_effect(me.hero_class)
    if not me.hero_power_used and me.mana >= hp_cost:
        if hp_eff.damage and hp_eff.scope == "enemy_hero":
            acts.append(Action("hero_power", cost=hp_cost, target="enemy_hero",
                               desc=f"英雄技能：{name}（打脸）"))
        elif hp_eff.damage:
            acts.append(Action("hero_power", cost=hp_cost, target="enemy_hero",
                               desc=f"英雄技能：{name}（打脸）"))
            for j, t in enumerate(foe.board):
                if t.alive:
                    acts.append(Action("hero_power", cost=hp_cost, target=f"enemy:{j}",
                                       desc=f"英雄技能：{name} 打 {t.name}"))
        else:
            acts.append(Action("hero_power", cost=hp_cost, desc=f"英雄技能：{name}"))

    acts.append(Action("end", desc="结束回合"))
    return acts


def _find_unit(board: Board, side: int, target: str) -> Unit | None:
    if not target.startswith(("enemy:", "friendly:")):
        return None
    which, idx = target.split(":")
    pool = board.sides[1 - side].board if which == "enemy" else board.sides[side].board
    try:
        u = pool[int(idx)]
    except (IndexError, ValueError):
        return None
    return u if u.alive else None


def apply_action(board: Board, side: int, action: Action, cards: CardDB) -> Board:
    """在副本上执行动作，返回新局面。"""
    nb = board.clone()
    nb.active = side
    me, foe = nb.sides[side], nb.sides[1 - side]

    if action.kind == "end":
        return nb

    if action.kind == "hero_power":
        name, cost, eff = hero_power_effect(me.hero_class)
        me.mana -= cost
        me.hero_power_used = True
        if eff.damage and (u := _find_unit(nb, side, "enemy:0")):
            nb.damage_unit(foe, u, eff.damage)
        elif eff.damage:
            nb.damage_hero(foe, eff.damage)
        elif eff.heal:
            me.hero_hp = min(30, me.hero_hp + eff.heal)
        elif eff.summon_atk or eff.summon_hp:
            me.board.append(Unit(card_id="", name=name, atk=eff.summon_atk, hp=max(1, eff.summon_hp)))
        nb.note(f"英雄技能：{name}")
        return nb

    if action.kind == "attack":
        u = me.board[action.unit_idx] if action.unit_idx < len(me.board) else None
        if u is None or not u.alive:
            return nb
        if action.target == "enemy_hero":
            nb.damage_hero(foe, u.atk)
            if u.lifesteal:
                me.hero_hp = min(30, me.hero_hp + u.atk)
            if u.poisonous:
                pass
            nb.note(f"{u.name} 攻击英雄（{u.atk} 点）")
        else:
            t = _find_unit(nb, side, action.target)
            if t is None:
                return nb
            atk_t, atk_u = t.atk, u.atk
            nb.damage_unit(foe, t, atk_u)
            nb.damage_unit(me, u, atk_t)
            if u.lifesteal:
                me.hero_hp = min(30, me.hero_hp + min(atk_u, t.max_hp + 9))
            nb.note(f"{u.name} 交换 {t.name}")
        u.attacks_left -= 1
        return nb

    # play
    if action.card_idx < 0 or action.card_idx >= len(me.hand):
        return nb
    card = me.hand.pop(action.card_idx)
    me.mana -= card.cost
    if card.is_minion:
        u = Unit(
            card_id=card.id,
            name=card.name,
            atk=max(0, card.atk),
            hp=max(1, card.health),
            max_hp=max(1, card.health),
            taunt=card.has("TAUNT"),
            divine=card.has("DIVINE_SHIELD"),
            charge=card.has("CHARGE"),
            rush=card.has("RUSH"),
            windfury=card.has("WINDFURY"),
            lifesteal=card.has("LIFESTEAL"),
            poisonous=card.has("POISONOUS"),
            spell_damage=_spell_damage(card),
            attacks_left=2 if card.has("WINDFURY") else 1,
            frozen=card.has("FREEZE"),
        )
        u.can_attack_now = u.charge
        if not u.charge:
            u.attacks_left = 0
        me.board.append(u)
        eff = parse_effect(card)
        if eff.damage or eff.buff_atk or eff.heal or eff.draw:
            tgt = _find_unit(nb, side, action.target)
            nb.apply_effect(eff, me, tgt)
        nb.note(f"打出 {card.name}")
    elif card.is_spell:
        me.spells_cast += 1
        eff = parse_effect(card)
        if eff.mana:
            me.mana += eff.mana          # 幸运币这类：打出来本回合多 1 费
        tgt = _find_unit(nb, side, action.target)
        if eff.damage and eff.scope == "target" and tgt is None:
            # 没有目标：直伤打脸
            nb.damage_hero(foe, eff.damage + sum(u.spell_damage for u in me.board if u.alive))
        else:
            nb.apply_effect(eff, me, tgt)
        nb.note(f"施放 {card.name}")
    elif card.is_weapon:
        me.weapon = Unit(card_id=card.id, name=card.name, atk=card.atk, hp=max(1, card.durability),
                         max_hp=max(1, card.durability))
        nb.note(f"装备 {card.name}")
    else:
        eff = parse_effect(card)
        nb.apply_effect(eff, me, _find_unit(nb, side, action.target))
    return nb


def _spell_damage(card: Card) -> int:
    if card.has("SPELL_DAMAGE") and card.text:
        import re

        m = re.search(r"法术伤害\+(\d+)", card.text)
        if m:
            return int(m.group(1))
    return 0


# ---------------------------------------------------------------- 单步最优

def best_action(
    board: Board,
    side: int,
    cards: CardDB,
    *,
    weights: dict[str, float] | None = None,
    aoe_risk: float = 0.0,
    min_gain: float = 0.25,
) -> tuple[Action, Board, float] | None:
    """返回 (动作, 执行后的局面, 局面分提升)。没有值得做的动作则返回 None。"""
    baseline = evaluate(board, weights, aoe_risk, side)
    best: tuple[Action, Board, float] | None = None
    for act in legal_actions(board, side, cards):
        if act.kind == "end":
            continue
        nb = apply_action(board, side, act, cards)
        score = evaluate(nb, weights, aoe_risk, side)
        if nb.sides[1 - side].hero_hp <= 0:
            score += 5_000
        if nb.sides[side].hero_hp <= 0:
            score -= 5_000
        gain = score - baseline
        if gain <= min_gain:
            continue
        if best is None or gain > best[2]:
            best = (act, nb, gain)
    return best


# ---------------------------------------------------------------- 斩杀检测

def max_face_damage(board: Board, side: int, cards: CardDB | None = None) -> dict[str, Any]:
    """估算本回合能打到对面脸上的最大伤害（已考虑嘲讽与费用）。"""
    me, foe = board.sides[side], board.sides[1 - side]
    taunts = [u for u in foe.board if u.alive and u.taunt]

    # 攻击是「整段」消耗的：一个 8/8 撞 2/5 嘲讽，溢出的 3 点不会转到脸上。
    # 所以不能简单用「总攻击力 - 嘲讽血量」，要把攻击段一个个分配出去。
    swings: list[int] = []
    for u in me.board:
        if not u.alive or u.atk <= 0 or u.frozen or u.attacks_left <= 0:
            continue
        hits = 2 if (u.windfury and u.attacks_left >= 2) else 1
        swings.extend([u.atk] * hits)
    if me.weapon and me.weapon.atk > 0:
        swings.append(me.weapon.atk)

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

    # 手里的直伤
    spell_dmg = 0
    mana = me.mana
    face_spells = []
    for card in sorted(me.hand, key=lambda c: c.cost):
        if not card.is_spell or card.cost > mana:
            continue
        eff = parse_effect(card)
        if eff.damage and eff.scope in ("target", "enemy_hero", "random_enemy"):
            face_spells.append((card.cost, eff.damage, card.name))
    for cost, dmg, name in face_spells:
        if cost <= mana:
            mana -= cost
            spell_dmg += dmg

    hp_name, hp_cost, hp_eff = hero_power_effect(me.hero_class)
    hero_power = 0
    if not me.hero_power_used and mana >= hp_cost and hp_eff.damage:
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


# ---------------------------------------------------------------- 路线规划

def _reason(before: Board, after: Board, action: Action, side: int) -> str:
    me0, foe0 = before.sides[side], before.sides[1 - side]
    me1, foe1 = after.sides[side], after.sides[1 - side]
    killed = [u.name for u in foe0.board if u.alive and not any(x is u or x.card_id == u.card_id for x in foe1.board)]
    hurt = []
    for u in foe0.board:
        if not u.alive:
            continue
        still = next((x for x in foe1.board if x.card_id == u.card_id and x.alive), None)
        if still is not None and still.hp < u.hp:
            hurt.append(f"{u.name} 剩 {still.hp} 血")
    dealt = max(0, (foe0.hero_hp + foe0.armor) - (foe1.hero_hp + foe1.armor))
    parts = []
    if killed:
        parts.append("解掉 " + "、".join(killed[:2]))
    if hurt and not killed:
        parts.append("打残 " + "、".join(hurt[:2]) + "（下一手可以换掉）")
    if dealt:
        parts.append(f"打脸 {dealt} 点")
    if len(me1.board) > len([u for u in me0.board if u.alive]):
        new = [u.name for u in me1.board if u not in me0.board][:1]
        if new:
            parts.append(f"站上 {new[0]}")
    if action.kind == "end":
        return "保留费用，等对方先动"
    if not parts:
        parts.append("调整站位与手牌")
    return "，".join(parts)


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


def plan_turn(
    board: Board,
    cards: CardDB,
    *,
    aoe_risk: float = 0.0,
    weights: dict[str, float] | None = None,
    side: int = 0,
    steps: int = MAX_STEPS,
    with_alternatives: bool = True,
) -> dict[str, Any]:
    """贪心规划一整回合，返回路线 + 备选。"""
    working = board.clone()
    working.active = side
    baseline = evaluate(working, weights, aoe_risk, side)

    # 能斩杀就别做别的：先按伤害贪心拼出完整顺序
    lethal_seq = _lethal_sequence(board, side, cards)
    if lethal_seq:
        plan: list[dict[str, Any]] = []
        cur = board.clone()
        for act in lethal_seq:
            nb = apply_action(cur, side, act, cards)
            plan.append({"desc": act.desc, "reason": _reason(cur, nb, act, side), "gain": 999.0})
            cur = nb
        return {
            "plan": plan,
            "gain": 999.0,
            "alternatives": [],
            "lethal": max_face_damage(board, side, cards),
            "final": cur,
        }

    plan: list[dict[str, Any]] = []
    total_gain = 0.0
    for _ in range(steps):
        acts = legal_actions(working, side, cards)
        best: tuple[float, Action, Board] | None = None
        for act in acts:
            if act.kind == "end":
                continue
            nb = apply_action(working, side, act, cards)
            score = evaluate(nb, weights, aoe_risk, side)
            # 斩杀直接置顶
            if nb.sides[1 - side].hero_hp <= 0:
                score += 5_000
            raw = score - evaluate(working, weights, aoe_risk, side)
            if raw <= 0.01 and nb.sides[1 - side].hero_hp > 0:
                continue          # 什么也没改变的动作（例如没有效果的英雄技能）不参与排序
            gain = raw + PRIORITY_BONUS.get(categorize(act, working, nb, side), 0.0)
            if gain <= EPS:
                continue
            if best is None or gain > best[0]:
                best = (gain, act, nb)
        if best is None:
            break
        gain, act, nb = best
        reason = _reason(working, nb, act, side)
        plan.append({"desc": act.desc, "reason": reason, "gain": round(gain, 2)})
        total_gain += gain
        working = nb
        if working.sides[1 - side].hero_hp <= 0:
            break

    lethal = max_face_damage(board, side, cards)
    if lethal["lethal"] and not plan:
        # 理论伤害够、但枚举出的动作里没有能兑现的线路（例如只有法术却算错口径）。
        # 这时宁可说「不算斩杀」，也不要塞一条点不出来的假动作。
        lethal = dict(lethal, lethal=False, note="理论伤害够，但没有能兑现的出牌线路")

    # 备选路线：不同起手的 Top 动作（让用户有得选）
    alts: list[dict[str, Any]] = []
    planned = {p["desc"] for p in plan}
    for act in (legal_actions(board, side, cards) if with_alternatives else []):
        if act.kind == "end" or act.desc in planned:
            continue
        nb = apply_action(board, side, act, cards)
        gain = evaluate(nb, weights, aoe_risk, side) - baseline
        if nb.sides[1 - side].hero_hp <= 0:
            gain += 5_000
        sub = plan_turn(nb, cards, aoe_risk=aoe_risk, weights=weights, side=side, steps=2,
                        with_alternatives=False)
        if gain + sub.get("gain", 0) <= 0.01 and nb.sides[1 - side].hero_hp > 0:
            continue          # 负收益动作不进备选，否则会出现「建议打幸运币」这种
        alts.append({
            "desc": act.desc,
            "reason": _reason(board, nb, act, side),
            "gain": round(gain + sub.get("gain", 0), 2),
            "then": [s["desc"] for s in sub.get("plan", [])],
        })
    alts.sort(key=lambda a: -a["gain"])

    return {
        "plan": plan,
        "gain": round(total_gain, 2),
        "alternatives": alts[:3],
        "lethal": lethal,
        "final": working,
    }


def advice_lines(plan_result: dict[str, Any]) -> list[dict[str, Any]]:
    """把规划结果整理成 UI 需要的「1-3 条建议」。"""
    out: list[dict[str, Any]] = []
    plan = plan_result.get("plan") or []
    if plan:
        out.append({
            "rank": 1,
            "title": plan[0]["desc"] if len(plan) == 1 else f"{plan[0]['desc']} → 共 {len(plan)} 步",
            "score": _score_from_gain(plan_result.get("gain", 0.0)),
            "reason": "；".join(p["reason"] for p in plan[:3]),
            "actions": [p["desc"] for p in plan],
        })
    if not out and not (plan_result.get("alternatives") or []):
        return [{
            "rank": 1,
            "title": "本回合没有值得做的动作",
            "score": 50,
            "reason": "现在能出的牌都是负收益（例如为了一点法力把幸运币交了）。留着费用和手牌，直接结束回合更划算。",
            "actions": ["结束回合"],
        }]

    for alt in plan_result.get("alternatives") or []:
        if len(out) >= 3:
            break
        if out and alt["desc"] == out[0]["title"]:
            continue
        out.append({
            "rank": len(out) + 1,
            # 没有主推荐时，第一条备选就是主推荐，别再挂「或者：」
            "title": ("或者：" if out else "") + alt["desc"],
            "score": _score_from_gain(alt["gain"]),
            "reason": alt["reason"] + (f"，之后可接 {alt['then'][0]}" if alt.get("then") else ""),
            "actions": [alt["desc"]] + list(alt.get("then") or []),
        })
    return out


def _score_from_gain(gain: float) -> int:
    if gain >= 100:
        return 99
    return max(1, min(97, int(round(55 + math.log1p(max(gain, 0)) * 9))))
