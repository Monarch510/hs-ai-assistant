"""自对弈引擎：让 AI 真的把一局牌打完。

这是「自主学习」的训练场，也是验证建议引擎的工具：
把两套牌交给两个 agent，让它们真的打，然后看谁赢。

它不是炉石客户端，也不联网：牌组从卡池里组，规则是本项目那套近似模型
（直伤/抽牌/BUFF/消灭 + 交换 + 疲劳）。所以模拟出的胜率只能用于
**相对比较**（A 权重 vs B 权重哪个好），不能当成天梯真实胜率。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .. import mulligan, policy
from ..cards import Card, CardDB
from ..meta import Archetype
from ..model import Board, SideState

Agent = Callable[[Board, int], "policy.Action | None"]

@dataclass
class GameOutcome:
    winner: int | None          # 0 / 1 / None(平局或超时)
    turns: int
    log: list[str] = field(default_factory=list)
    hp: tuple[int, int] = (0, 0)

    @property
    def decided(self) -> bool:
        return self.winner is not None

def make_deck(arch: Archetype, cards: CardDB, rng: random.Random) -> list[Card]:
    """按原型组 30 张：标志牌优先，剩余槽位从职业牌池随机补。"""
    deck: list[Card] = []
    counts: dict[str, int] = {}
    for cid, n in arch.cards.items():
        card = cards.get(cid)
        if card is None:
            continue
        for _ in range(n):
            if len(deck) >= 30:
                break
            deck.append(card)
        counts[cid] = n
    pool = cards.standard_pool(arch.card_class)
    guard = 0
    while len(deck) < 30 and guard < 500:
        guard += 1
        if not pool:
            break
        card = rng.choice(pool)
        if counts.get(card.id, 0) >= 2:
            continue
        counts[card.id] = counts.get(card.id, 0) + 1
        deck.append(card)
    rng.shuffle(deck)
    return deck

def greedy_agent(weights: dict[str, float] | None = None, aoe_risk: float = 0.0) -> Agent:
    """每步取「局面分提升最大」的动作，没有提升就结束回合。"""

    def _act(board: Board, side: int) -> "policy.Action | None":
        if board.cards is None:
            return None
        best = policy.best_action(board, side, board.cards, weights=weights, aoe_risk=aoe_risk)
        return best[0] if best else None

    return _act

def _draw(side: SideState, board: Board, n: int = 1) -> None:
    for _ in range(n):
        board.draw(side)

def _mulligan_phase(
    board: Board,
    side_idx: int,
    cards: CardDB,
    *,
    opp_tags: Sequence[str],
    weights: dict[str, float] | None,
    do_mulligan: bool,
) -> None:
    side = board.sides[side_idx]
    if not do_mulligan:
        return
    hand = list(side.hand)
    for card in hand:
        advice = mulligan.advise(
            [card], cards=cards, prediction=None, our_tags=opp_tags, is_first=side_idx == 0, weights=weights
        )
        if advice and advice[0]["action"] == "toss" and side.deck:
            side.hand.remove(card)
            replacement = side.deck.pop(0)
            side.hand.append(replacement)
            side.deck.append(card)

def _reset_attacks(side: SideState) -> None:
    # 攻击次数只放 hero_attacks_left：斩杀估算也只认它，
    # 两个计数器都置位会让 apply_action 每回合多打一次
    side.weapon_attacks_left = 0
    side.hero_attacks_left = 2 if (side.weapon is not None and side.weapon.windfury) else 1

def run_game(
    deck_a: Sequence[Card],
    deck_b: Sequence[Card],
    cards: CardDB,
    *,
    weights_a: dict[str, float] | None = None,
    weights_b: dict[str, float] | None = None,
    mulligan_weights: dict[str, float] | None = None,
    mulligan_weights_a: dict[str, float] | None = None,
    mulligan_weights_b: dict[str, float] | None = None,
    do_mulligan: bool = True,
    aoe_risk_a: float = 0.0,
    aoe_risk_b: float = 0.0,
    seed: int = 0,
    max_turns: int = 50,
    trace: bool = False,
    class_a: str | None = None,
    class_b: str | None = None,
) -> GameOutcome:
    rng = random.Random(seed)
    first = seed % 2  # 轮换先后手，避免训练时先手优势污染结论
    sides = [SideState(), SideState()]
    sides[0].deck = list(deck_a)
    sides[1].deck = list(deck_b)
    sides[0].hero_class = class_a or (getattr(deck_a[0], "card_class", "NEUTRAL") if deck_a else "NEUTRAL")
    sides[1].hero_class = class_b or (getattr(deck_b[0], "card_class", "NEUTRAL") if deck_b else "NEUTRAL")
    board = Board(sides, turn=1, active=first, cards=cards)

    for idx in (0, 1):
        _draw(sides[idx], board, 3 if idx == first else 4)
    mw_a = mulligan_weights_a if mulligan_weights_a is not None else mulligan_weights
    mw_b = mulligan_weights_b if mulligan_weights_b is not None else mulligan_weights
    for idx in (0, 1):
        if idx == 0:
            _mulligan_phase(board, 0, cards, opp_tags=(), weights=mw_a, do_mulligan=do_mulligan)
        else:
            _mulligan_phase(board, 1, cards, opp_tags=(), weights=mw_b, do_mulligan=do_mulligan)

    agents = [
        greedy_agent(weights_a, aoe_risk_a),
        greedy_agent(weights_b, aoe_risk_b),
    ]

    turn_no = 0
    while turn_no < max_turns:
        turn_no += 1
        idx = (first + turn_no - 1) % 2
        side = board.sides[idx]
        board.active = idx
        own_turn = (turn_no + 1) // 2 if idx == first else turn_no // 2
        side.max_mana = max(1, min(10, own_turn))
        side.mana = side.max_mana
        side.hero_power_used = False
        _reset_attacks(side)
        for u in side.board:
            if not u.alive:
                continue
            u.attacks_left = 2 if u.windfury else 1
            u.frozen = False

        _draw(side, board, 1)
        if side.hero_hp <= 0:
            return GameOutcome(1 - idx, turn_no, list(board.log) if trace else [], (sides[0].hero_hp, sides[1].hero_hp))

        # 一个回合内反复选动作
        for _ in range(12):
            act = agents[idx](board, idx)
            if act is None:
                break
            board = policy.apply_action(board, idx, act, cards)
            if trace:
                board.log.append(f"[T{turn_no}] {'我方' if idx == 0 else '对方'} {act.desc}")
            if board.sides[1 - idx].hero_hp <= 0 or board.sides[idx].hero_hp <= 0:
                break
        if board.sides[0].hero_hp <= 0 or board.sides[1].hero_hp <= 0:
            winner = 1 if board.sides[0].hero_hp <= 0 else 0
            return GameOutcome(winner, turn_no, list(board.log) if trace else [], (board.sides[0].hero_hp, board.sides[1].hero_hp))

    return GameOutcome(None, turn_no, list(board.log) if trace else [], (board.sides[0].hero_hp, board.sides[1].hero_hp))

def match(
    arch_a: Archetype,
    arch_b: Archetype,
    cards: CardDB,
    *,
    games: int = 10,
    weights_a: dict[str, float] | None = None,
    weights_b: dict[str, float] | None = None,
    mulligan_weights: dict[str, float] | None = None,
    do_mulligan: bool = True,
    seed: int = 0,
) -> dict[str, Any]:
    """两个卡组打 N 局，返回胜率统计。"""
    rng = random.Random(seed)
    deck_a = make_deck(arch_a, cards, rng)
    deck_b = make_deck(arch_b, cards, rng)
    wins = [0, 0]
    draws = 0
    for g in range(games):
        out = run_game(
            deck_a, deck_b, cards,
            weights_a=weights_a, weights_b=weights_b,
            mulligan_weights=mulligan_weights, do_mulligan=do_mulligan,
            seed=seed + g, class_a=arch_a.card_class, class_b=arch_b.card_class,
        )
        if out.winner is None:
            draws += 1
        else:
            wins[out.winner] += 1
    decided = max(1, wins[0] + wins[1])
    return {
        "games": games,
        "wins": wins,
        "draws": draws,
        "winrate_a": wins[0] / decided,
        "winrate_b": wins[1] / decided,
    }
