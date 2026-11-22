"""把日志状态翻译成 Board 时用的辅助函数。

board_from_state 本体**故意留在 advisor.py**：test_advisor_board.py 用源码切片
（src.index("def board_from_state")）做静态防回归，函数定义必须在那儿。
这里只放它能调用的纯工具。
"""

from __future__ import annotations

import re
from typing import Any

from . import logutil
from .cards import Card, CardDB, windfury_attacks
from .model import Unit
from .model_effects import spell_damage_from_card


# UNKNOWN 是「未知牌」占位，_unknown_cards 用它填手牌
UNKNOWN = Card(id="__unknown__", name="未知牌", cost=99)


def _view_max_attacks(card, view: dict[str, Any]) -> int:
    # 视图带 max_attacks 就是算好的（含日志动态赋予），否则退回卡面静态识别
    n = view.get("max_attacks")
    return int(n) if n is not None else windfury_attacks(card)


def _attacks_left(card, view: dict[str, Any]) -> int:
    """这回合还能攻击几次。上限由视图给出（风怒 2、超级风怒 4、普通 1），要减掉已打过的。"""
    total = _view_max_attacks(card, view)
    if _woke_this_turn(card, view):
        return 0
    if view.get("woke_turn") is not None and view.get("woke_turn") == view.get("turn_now"):
        return 0
    used = int(view.get("attacks_used") or 0)
    if not (view.get("attackable") or view.get("can_attack")):
        # 客户端说不能攻击时，多段攻击且已打过的还可能剩几次
        return max(0, total - used) if used else 0
    return max(0, total - used)

def _woke_this_turn(card, view: dict[str, Any]) -> bool:
    """休眠随从是不是本回合刚醒——刚醒的这回合不能攻击。"""
    text = (card.text or "") if card is not None else ""
    m = re.search(r"(\d+)\s*(?:或\s*\d+\s*)?回合后唤醒", text)
    played = view.get("played_turn")
    now = view.get("turn_now")
    if not m or played is None or not isinstance(now, int):
        return False
    return (now - int(played)) == int(m.group(1))

def _still_dormant(card, view: dict[str, Any]) -> bool:
    """还没到唤醒回合的休眠随从。

    卡面写「N回合后唤醒」，入场回合记为 T，那么到 T+N 才醒。
    """
    text = (card.text or "") if card is not None else ""
    # 文本不一定写「休眠」，但只要有「N回合后唤醒」就是休眠牌
    m = re.search(r"(\d+)\s*(?:或\s*\d+\s*)?回合后唤醒", text)
    if not m:
        return False
    played = view.get("played_turn")
    now = view.get("turn_now")
    if played is None or not isinstance(now, int):
        return bool(view.get("dormant"))
    return (now - int(played)) < int(m.group(1))

def _unit_from_view(view: dict[str, Any], cards: CardDB) -> Unit:
    card = cards.get(view.get("id") or "")
    return Unit(
        card_id=view.get("id") or "",
        name=view.get("name") or "?",
        atk=max(0, int(view.get("atk") or 0)),
        hp=max(1, int(view.get("health") or 1)),
        max_hp=max(1, int(view.get("health") or 1)),
        taunt=bool(view.get("taunt")),
        divine=bool(view.get("divine_shield")),
        charge=bool(card and card.has("CHARGE")),
        rush=bool(card and card.has("RUSH")),
        # 突袭刚入场：这回合能打随从，不能打脸
        rush_only=bool(card and card.has("RUSH") and view.get("fresh")),
        dormant=bool(view.get("dormant")) or _still_dormant(card, view),
        windfury=_view_max_attacks(card, view) >= 2,
        lifesteal=bool(card and card.has("LIFESTEAL")),
        poisonous=bool(card and card.has("POISONOUS")),
        spell_damage=spell_damage_from_card(card),
        attacks_left=_attacks_left(card, view),
        mega_windfury=_view_max_attacks(card, view) >= 4,
        cannot_attack_hero=bool(view.get("cannot_attack_hero")),
    )

def _unknown_cards(n: int) -> list[Card]:
    """占位牌：只为「张数」和「摸牌不会疲劳」服务，费用 99 保证永远不会被当成可出的牌。"""
    return [UNKNOWN for _ in range(max(0, n))]

def _log_err(msg: str) -> None:
    """把复盘/记录阶段的异常写进 data/hsai.log，别再静默吞掉。"""
    logutil.log(msg)
