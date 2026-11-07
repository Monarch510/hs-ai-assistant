"""被动技能与威胁估算：不依赖其它 policy 块的最底层。"""

from __future__ import annotations

import re

from .model import Board


def passive_rule(hero_power) -> dict:
    """从被动英雄技能的文本里读出「计数器规则」。

    返回 {"count": 统计什么, "every": 阈值, "effect": 触发什么}，
    识别不了就返回 {}（宁可什么都不做，也不乱触发）。
    """
    text = (hero_power.text or "") if hero_power is not None else ""
    if not text:
        return {}
    # 兹伊的力量这类：每使用五张随从牌，第五个随从的战吼触发两次
    if "每使用五张随从牌" in text and "战吼" in text and "两次" in text:
        return {"count": "minion", "every": 5, "effect": "battlecry_double"}
    if "每使用三张" in text and "战吼" in text:
        return {"count": "minion", "every": 3, "effect": "battlecry_double"}
    return {}

def battlecry_times(played_minions: int, rule: dict) -> int:
    """这一张随从的战吼要结算几次。"""
    every = int(rule.get("every") or 0)
    if every <= 0 or rule.get("effect") != "battlecry_double":
        return 1
    return 2 if played_minions > 0 and played_minions % every == 0 else 1

def unit_threat(unit, cards, board: Board | None = None, side: int = 0) -> float:
    """随从的威胁值。

    身体：攻 ×1.25 + 血（攻击力决定能杀什么，比血更值钱）
    圣盾：接近翻倍（多一条命）
    嘲讽：加一点（只是逼你先打它）
    光环：按「加成 × 受影响随从数」算实际收益
    其余（突袭/吸血/风怒/亡语/剧毒）：固定小值
    """
    if unit is None:
        return 0.0
    body = unit.atk * 1.25 + max(0, unit.hp)
    value = body

    if unit.taunt:
        value += 1.5
    if unit.divine:
        # 圣盾相当于多出一个「同攻击、1 血」的随从
        value += unit.atk * 1.25 + 1.0
    if unit.rush:
        value += 1.0
    if unit.lifesteal:
        value += 1.0
    if unit.windfury:
        value += unit.atk * 0.6
    if unit.poisonous:
        value += 1.5

    card = cards.get(unit.card_id) if (cards is not None and unit.card_id) else None
    text = (card.text or "") if card is not None else ""
    keyword = str(getattr(card, "keyword", "") or "") if card is not None else ""

    if "光环" in keyword or "光环" in text:
        m = re.search(r"\+(\d+)/\+(\d+)", text)
        if m:
            bonus = int(m.group(1)) * 1.25 + int(m.group(2))
            if board is not None:
                others = len([u for u in board.sides[side].board if u.alive]) - 1
            else:
                others = 1
            value += bonus * max(1, others)
        else:
            value += 2.0
    if "亡语" in keyword or "亡语" in text:
        value += 1.5
    return value

def board_threat(board: Board, side: int, cards) -> float:
    """一方的场面价值：随从威胁 + 武器。"""
    me = board.sides[side]
    total = sum(unit_threat(u, cards, board, side) for u in me.board if u.alive)
    if me.weapon is not None:
        total += me.weapon.atk * 1.2 + max(0, me.weapon.hp) * 0.25
    return total
