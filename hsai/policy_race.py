"""抢血 / 解场判断：现在是该抢脸还是该清场。"""

from __future__ import annotations

from typing import Any

from .model import Board, hero_power_effect, is_passive_hero_power, parse_effect
from .policy_actions import Action, apply_action
from .policy_threat import board_threat, unit_threat

def _hand_burst(board: Board, side: int, cards) -> float:
    """手里能直接打脸的伤害（斩杀时能一次性砸出去的量）。"""
    me = board.sides[side]
    total = 0.0
    for card in me.hand:
        if card is None or getattr(card, "type", "") != "SPELL":
            continue
        try:
            eff = parse_effect(card)
        except Exception:
            continue
        if not eff.known or not eff.damage:
            continue
        if eff.scope in ("target", "any", "enemy_hero", "enemy") and not eff.aoe:
            total += eff.damage
    return total

def race_plan(board: Board, side: int, cards, opp_coeff: float = 1.5) -> dict[str, Any]:
    """判断这回合该抢血还是解场。

    比的是「谁先死」：
        我赢的回合 = (他血量 - 我手上的爆发) / 我每回合的持续伤害
        他赢的回合 = 我血量 / 他每回合的伤害
    """
    me, foe = board.sides[side], board.sides[1 - side]
    my_damage = float(me.board_attack)
    if me.weapon is not None:
        my_damage += me.weapon.atk
    if me.hero_power is not None and not is_passive_hero_power(me.hero_power):
        _, _, eff = hero_power_effect(me.hero_class, me.hero_power)
        if eff.damage and eff.scope in ("enemy_hero", "any"):
            my_damage += eff.damage

    burst = _hand_burst(board, side, cards)
    his_damage = float(foe.board_attack) + len(foe.hand) * opp_coeff

    foe_hp = max(1.0, foe.hero_hp + foe.armor - burst)
    my_hp = max(1.0, me.hero_hp + me.armor)
    my_turns = foe_hp / my_damage if my_damage > 0 else 99.0
    his_turns = my_hp / his_damage if his_damage > 0 else 99.0
    mode = "race" if my_turns < his_turns else "trade"
    return {
        "mode": mode,
        "my_turns": round(my_turns, 1),
        "his_turns": round(his_turns, 1),
        "my_damage": round(my_damage, 1),
        "his_damage": round(his_damage, 1),
        "burst": round(burst, 1),
        "reason": (
            f"我 {my_turns:.1f} 回合赢，他 {his_turns:.1f} 回合赢"
            if my_damage > 0 and his_damage > 0
            else "伤害信息不足"
        ),
    }

def _turns_after(board: Board, side: int, lose_attack: float, kill_attack: float) -> tuple[float, float]:
    """假设打掉对方一只随从（kill_attack）、同时损失我方一些攻击力，重算双方回合数。"""
    me, foe = board.sides[side], board.sides[1 - side]
    my_damage = max(1.0, float(me.board_attack) - lose_attack)
    his_damage = max(0.0, float(foe.board_attack) - kill_attack)
    my_turns = max(1.0, foe.hero_hp + foe.armor) / my_damage
    his_turns = (me.hero_hp + me.armor) / his_damage if his_damage > 0 else 99.0
    return my_turns, his_turns

def _mode_allows(action: Action, before: Board, side: int, mode: str, cards) -> bool:
    """按「抢血 / 解场」过滤动作。

    抢血：只有白吃不拖慢节奏、或者放着它下回合会打死我，才允许交换。
    解场：有划算的交换时不去打脸。
    """
    if action.kind != "attack":
        return True
    me, foe = before.sides[side], before.sides[1 - side]
    unit = me.board[action.unit_idx] if action.unit_idx < len(me.board) else None
    if unit is None:
        return True

    if action.target == "enemy_hero":
        if mode == "race":
            return True
        # 解场模式：只要还有划算的交换，就先换
        return not _has_profitable_trade(before, side, cards)

    if not (action.target or "").startswith("enemy:"):
        return True
    try:
        j = int(action.target.split(":")[1])
    except (IndexError, ValueError):
        return True
    target = foe.board[j] if j < len(foe.board) else None
    if target is None:
        return True

    free = unit.atk >= target.hp and target.atk < unit.hp
    if mode == "race":
        if not free:
            # 抢血模式不主动亏血换怪；除非放着它我会被打死
            return target.atk >= me.hero_hp
        # 白吃之后如果自己一点输出都不剩了，「更快」就是假的，不能吃
        if me.board_attack - unit.atk <= 0:
            return False
        # 和「现在的」对手回合数比，而不是和白吃之后的比 ——
        # 白吃会把他伤害打成 0，那样任何交换都显得划算
        _, his_now = _turns_after(before, side, 0.0, 0.0)
        my_after, _ = _turns_after(before, side, unit.atk, target.atk)
        return my_after <= his_now            # 白吃之后仍然更快，就吃
    # 解场模式：换完之后的「双方差值」更大才换（只看随从 + 武器）
    # 注意是差值：交换会降低我方价值，只看我方会永远判成亏
    try:
        after = apply_action(before, side, action, cards)
    except Exception:
        return True
    gain = ((board_threat(after, side, cards) - board_threat(after, 1 - side, cards))
            - (board_threat(before, side, cards) - board_threat(before, 1 - side, cards)))
    return gain > 0

def _has_profitable_trade(board: Board, side: int, cards=None) -> bool:
    me, foe = board.sides[side], board.sides[1 - side]
    for u in me.board:
        if not u.alive or u.attacks_left <= 0:
            continue
        for t in foe.board:
            if not t.alive:
                continue
            if u.atk >= t.hp and (t.atk < u.hp or t.value >= u.value):
                # 用净值复核：真的划算才算「有划算的交换」
                after_gain = ((unit_threat(u, cards) - unit_threat(t, cards)))
                if after_gain > 0:
                    return True
    return False
