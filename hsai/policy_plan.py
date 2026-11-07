"""路线规划：把动作组合成本回合能照着打的一条线。"""

from __future__ import annotations

import math
from typing import Any

from . import tag_rules
from .cards import CardDB
from .model import Board, evaluate
from .policy_actions import (
    Action, apply_action, categorize, legal_actions,
)
from .policy_consts import (
    EPS, MAX_STEPS, PRIORITY_BONUS, TAG_WEIGHT_SCALE,
)
from .policy_lethal import _lethal_sequence, max_face_damage
from .policy_race import _mode_allows, race_plan
from .policy_threat import unit_threat


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

def _tag_bonus(
    action: Action,
    before: Board,
    side: int,
    tag_weights: dict[str, float] | None,
    tag_scale: float,
) -> float:
    """打出一张手牌能拿到的标签排序分；主路线和备选路线共用同一个口径。

    只加给 kind=="play"：标签是打出的那张牌的功能属性，攻击/英雄技能/结束回合
    没有对应手牌。标签用纯规则 RULES 现算，绝不读 data/card_tags.json 缓存
    （缓存可能没 build 过，会静默变 0）。
    """
    if tag_weights is None or action.kind != "play":
        return 0.0
    hand = before.sides[side].hand
    card = hand[action.card_idx] if 0 <= action.card_idx < len(hand) else None
    if card is None:
        return 0.0
    return tag_scale * sum(
        tag_weights.get(t, 0.0)
        for t in tag_rules.TAG_ORDER if tag_rules.RULES[t](card)
    )

def plan_turn(
    board: Board,
    cards: CardDB,
    *,
    aoe_risk: float = 0.0,
    weights: dict[str, float] | None = None,
    side: int = 0,
    steps: int = MAX_STEPS,
    with_alternatives: bool = True,
    opp_coeff: float = 1.5,
    tag_weights: dict[str, float] | None = None,
    tag_scale: float = TAG_WEIGHT_SCALE,
) -> dict[str, Any]:
    """贪心规划一整回合，返回路线 + 备选。

    `tag_weights` 是局势算出的 12 类标签权重（hsai/tag_weights.py），只给
    「打出手牌」的动作加同档排序分；不传（None）时行为与接标签权重之前一致。
    """
    working = board.clone()
    working.active = side
    baseline = evaluate(working, weights, aoe_risk, side)
    race = race_plan(board, side, cards, opp_coeff)
    mode = race["mode"]

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
            if not _mode_allows(act, working, side, mode, cards):
                continue
            nb = apply_action(working, side, act, cards)
            score = evaluate(nb, weights, aoe_risk, side)
            if nb.sides[1 - side].hero_hp <= 0:
                score += 5_000
            raw = score - evaluate(working, weights, aoe_risk, side)
            if raw <= 0.01 and nb.sides[1 - side].hero_hp > 0:
                continue          # 什么也没改变的动作（例如没有效果的英雄技能）不参与排序
            gain = raw + PRIORITY_BONUS.get(categorize(act, working, nb, side), 0.0)
            gain += _tag_bonus(act, working, side, tag_weights, tag_scale)
            # 抽到的牌比看起来值钱：一张牌 ≈ 1.2，比 1 点血量（0.55）更该被看重
            if act.kind == "play":
                before = len(working.sides[side].hand) - 1      # 打出去那张不在手里
                drawn = len(nb.sides[side].hand) - max(0, before)
                if drawn > 0:
                    gain += drawn * 1.2
            if (mode == "trade" and act.kind == "attack"
                    and (act.target or "").startswith("enemy:")):
                # 解场模式：两个交换都不亏时，优先解威胁更大的那只。
                # 光看评分会为了少挨 1 点反伤去解 1/2，放着 2/2 继续打脸。
                try:
                    j = int((act.target or "").split(":", 1)[1])
                    tgt = working.sides[1 - side].board[j]
                    atker = working.sides[side].board[act.unit_idx]
                except (ValueError, IndexError):
                    tgt = atker = None
                if tgt is not None and atker is not None and tgt.alive:
                    kills = tgt.hp <= atker.atk + (0 if not atker.poisonous else tgt.hp)
                    survives = atker.hp > tgt.atk or atker.divine or atker.poisonous
                    if kills and survives:
                        gain += unit_threat(tgt, cards, working, 1 - side) * 0.6
            if act.kind == "attack" and (act.target or "").startswith("enemy:"):
                # 并列时用更不值钱的那只去换，保住高价值随从
                attacker = working.sides[side].board[act.unit_idx] \
                    if act.unit_idx < len(working.sides[side].board) else None
                gain -= unit_threat(attacker, cards, working, side) * 1e-3
            if gain <= EPS:
                continue
            if best is None or gain > best[0]:
                best = (gain, act, nb)
        if best is None:
            # 没有“有收益”的动作，但手里还有能打出的牌（比如 0 费奥秘）：
            # 别直接说“结束回合”，先把它打出去。
            fallback = [a for a in acts
                        if a.kind == "play" and a.cost <= working.sides[side].mana]
            if fallback and not plan:
                pick = min(fallback, key=lambda a: a.cost)
                nb2 = apply_action(working, side, pick, cards)
                plan.append({"desc": pick.desc,
                             "reason": "没有更好的选择，能打就先打掉",
                             "gain": 0.0})
                working = nb2
                continue
            break
        gain, act, nb = best
        reason = _reason(working, nb, act, side)
        plan.append({"desc": act.desc, "reason": reason, "gain": round(gain, 2)})
        total_gain += gain
        working = nb
        if working.sides[1 - side].hero_hp <= 0:
            break

    # 剩下法力又没别的事可做时，把「预备」当作收尾动作推荐出来
    leftover = working.sides[side].mana
    if leftover > 0:
        prep = [a for a in legal_actions(working, side, cards) if a.kind == "prepare"]
        if prep and (not plan or leftover >= 3):
            act = prep[0]
            nb = apply_action(working, side, act, cards)
            plan.append({
                "desc": act.desc,
                "reason": f"剩下的 {leftover} 点法力留着浪费，预备后这张牌费用直降 {leftover + 1}",
                "gain": 0.0,
            })
            working = nb

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
        # 首个备选动作本身也要算标签分；后续动作由递归的 sub 负责，别重复加
        gain += _tag_bonus(act, board, side, tag_weights, tag_scale)
        sub = plan_turn(nb, cards, aoe_risk=aoe_risk, weights=weights, side=side, steps=2,
                        with_alternatives=False, tag_weights=tag_weights, tag_scale=tag_scale)
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
        "mode": mode,
        "race": race,
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
            "title": "结束回合",
            "score": 50,
            "reason": "没有值得做的事",
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

def validate_sequence(board: Board, descs: list[str], cards: CardDB,
                      side: int = 0) -> list[dict[str, Any]]:
    """把一串「动作描述」按顺序模拟一遍，检查费用和可执行性。

    AI 规划路线时只看得到开局的法力，很容易排出「3+3+3 费但只有 5 费」的路线。
    这里逐步扣费，扣不起的那步标成费用不足，后面的也一并标出来。
    """
    work = board.clone()
    work.active = side
    out: list[dict[str, Any]] = []
    for desc in descs:
        acts = legal_actions(work, side, cards)
        act = next((a for a in acts if a.desc == desc), None)
        if act is None:
            out.append({"desc": desc, "ok": False, "why": "当前不可执行"})
            continue
        if act.cost > work.sides[side].mana:
            out.append({"desc": desc, "ok": False, "why": "费用不足"})
            continue
        out.append({"desc": desc, "ok": True, "why": ""})
        work = apply_action(work, side, act, cards)
    return out
