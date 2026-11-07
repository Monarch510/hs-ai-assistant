"""局面评估：把 Board 变成一个可比较的分数。

只读局面，不修改。依赖 .model_effects 与 .model_units；
评估缓存 _DB 留在门面 model 上，这里在函数内部回查（会被 set_card_db 写）。
"""

from __future__ import annotations

from .model_effects import parse_effect, warden_buff, hero_power_effect, is_passive_hero_power
from .model_units import Board, SideState


# ---------------------------------------------------------------- 评估函数
DEFAULT_WEIGHTS = {
    "tempo": 1.0,        # 场面交换优势
    "face": 0.9,         # 打脸进度
    "hand": 0.7,         # 手牌差
    "health": 0.55,      # 血量差
    "taunt": 0.8,        # 嘲讽墙对进攻的阻滞
    "aoe_risk": 1.2,
    "fatigue": 0.6,     # 牌库差 / 疲劳伤害（残局里牌多的一方更能拖）     # 铺场被清的风险
}


def resource_score(side_state, cards=None) -> float:
    """估算一方还有多少「资源」。

    = 手牌张数
      + 手牌里能抽牌/发现/加费的量（还能再生出多少张牌）
      + 英雄技能能否造资源
      + 场上带「抽牌/发现」的随从（光环、亡语，按半张算）

    为什么不用「手牌总费用」：费用高不代表能再生出牌，一张 8 费可能只是
    一张大随从；而「抽两张」才是真的多两条命。
    """
    from . import model as _model

    # 牌库剩余也算资源：每 4 张折 1 点（离摸到还早，不能和手牌等价）
    score = 0.25 * len(side_state.deck) + float(len(side_state.hand))
    for card in side_state.hand:
        if card is None:
            continue
        eff = parse_effect(card)
        try:
            score += float(eff.draw or 0)
        except (TypeError, ValueError):
            pass
        if eff.discover:
            score += 1.0
        if eff.mana:
            score += 1.0
        # 费用变化的收益不在本回合兑现，只给保守分；调参留给下一批
        if eff.cost_delta_self < 0:
            score += 0.5 * abs(eff.cost_delta_self)
        if eff.cost_delta_opp > 0:
            score += 1.0 * eff.cost_delta_opp

    hp = getattr(side_state, "hero_power", None)
    if hp is not None and not is_passive_hero_power(hp):
        _, _, eff = hero_power_effect(side_state.hero_class, hp)
        if eff.draw or eff.discover:
            score += 1.0

    cards = cards or _model._DB
    if cards is not None:
        for unit in side_state.board:
            if getattr(unit, "card_id", ""):
                card = cards.get(unit.card_id)
                text = (card.text or "") if card is not None else ""
                if "抽" in text or "发现" in text:
                    score += 0.5          # 站在场上的资源，按半张算
    return score


def _adjacency_value(side_state) -> float:
    """相邻光环带来的额外价值：只在两个随从真的挨着时才算。"""
    import re as _re

    from . import model as _model

    total = 0.0
    board = [u for u in side_state.board if u.alive]
    for i, u in enumerate(board):
        card = _model._DB.get(u.card_id) if _model._DB is not None and u.card_id else None
        text = (card.text or "") if card is not None else ""
        if "相邻" not in text:
            continue
        m = _re.search(r"\+(\d+)/\+(\d+)", text)
        buff = (int(m.group(1)) + int(m.group(2))) if m else 2
        neighbors = 0
        if i > 0:
            neighbors += 1
        if i + 1 < len(board):
            neighbors += 1
        total += buff * neighbors * 0.5
    return total


def evaluate(board: Board, weights: dict[str, float] | None = None, aoe_risk: float = 0.0,
             perspective: int = 0) -> float:
    """从 perspective 这一方视角给局面打分。数值本身无意义，只用于横向比较。"""
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    me, foe = board.sides[perspective], board.sides[1 - perspective]

    # 武器也是「能持续输出的资源」：按攻击力计价，耐久略加权
    # 休眠的随从这回合动不了，价值打折（玛维的 +3/+3 代价就在这里）
    def _bval(units):
        return sum(u.value * (0.6 if u.dormant else 1.0) for u in units if u.alive)

    my_val = _bval(me.board)
    foe_val = _bval(foe.board)
    if me.weapon is not None:
        my_val += me.weapon.atk * 1.2 + max(0, me.weapon.hp) * 0.25
    if foe.weapon is not None:
        foe_val += foe.weapon.atk * 1.2 + max(0, foe.weapon.hp) * 0.25

    # 嘲讽墙：对方有墙时，我们的攻击力打折
    taunt_val = sum(u.hp + (0 if u.divine else 0) for u in foe.board if u.alive and u.taunt)
    my_reach = me.board_attack + (me.weapon.atk if me.weapon else 0)
    blocked = min(my_reach, taunt_val) * 0.6

    score = 0.0
    score += w["tempo"] * (my_val - foe_val)
    # 摆位会影响相邻光环的价值，这样「第几位」才有意义
    score += w["tempo"] * (_adjacency_value(me) - _adjacency_value(foe))
    score += w["face"] * ((30 - foe.hero_hp - foe.armor) - (30 - me.hero_hp - me.armor)) * 0.5
    # 场上有「之后每个随从 +X/+X」的光环（玛维）：按每只随从能吃到一次估
    for _u in me.board:
        if _u.alive and warden_buff(_u, getattr(board, "cards", None)) > 0:
            score += 1.6
            break
    score += w["hand"] * (resource_score(me) - resource_score(foe)) * 1.2
    score += w["health"] * ((me.hero_hp + me.armor) - (foe.hero_hp + foe.armor)) * 0.35
    score += w["taunt"] * blocked * -0.4

    # 疲劳伤害：牌库差已经算进「资源」，这里只管还在吃的疲劳
    fatigue_gap = foe.fatigue - me.fatigue
    score += w["fatigue"] * fatigue_gap * 0.8

    # 铺场风险：我方随从越多、对方有 AOE 的概率越高，扣分越狠
    if aoe_risk > 0.05 and len(me.board) >= 2:
        exposure = sum(u.value for u in me.board if u.alive and u.hp <= 4)
        score -= w["aoe_risk"] * aoe_risk * exposure * 0.22

    # 斩杀优先：对方已经死了就是最大分
    if foe.hero_hp <= 0:
        score += 10_000
    if me.hero_hp <= 0:
        score -= 10_000

    # 被对手斩杀线覆盖时要开始防守（这个阶跃是想要的：杀死对方一个随从让
    # 他的总攻降到斩杀线以下，值得重奖）。
    #
    # 但**不要再加「我能斩杀」的阶跃加分**：打一下脸就会让「还能不能斩杀」翻转，
    # 评分出现 -800 的悬崖，AI 反而打一下就不打了。斩交由 policy._lethal_sequence
    # 单独处理（按伤害贪心拼完整顺序）。
    # 固定阶跃，不是可调权重：DEFAULT_WEIGHTS 里没有 lethal_bonus，
    # w.get(..., 1.0) 看着像参数，实际外部传 100 就会把分数压掉 42000
    foe_reach = foe.board_attack + (foe.weapon.atk if foe.weapon else 0)
    if foe_reach >= me.hero_hp + me.armor:
        score -= 420
    return score
