"""卡牌效果规则的验证器：一条新规则会不会误伤已有的牌。

早年这里还会让 AI 局中补规则、验证通过后写回 effects.json；那套已下线，
现在只留验证逻辑，供测试与人工核对规则用 —— 运行时不调、也不写盘。
未建模的牌只记日志，要补规则由人看过复盘后手动加。

三道闸门：
    ① 目标牌必须从「未建模」变成「能解析」
    ② 全库里已经能解析的牌，结果一个都不能变
    ③ 一条规则最多新覆盖 3 张牌（再多说明规则写得太宽）

为什么这么严：一条宽泛的正则（比如「造成X点伤害」）可能同时改掉几十张牌的
解析结果，而你看不出来 —— 直到某个斩杀算错。
"""

from __future__ import annotations

import json
import re
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any

from .model import Effect, parse_effect
from .paths import EFFECTS_JSON

EFFECTS_PATH = EFFECTS_JSON
MAX_NEW_COVER = 3          # 一条规则最多新覆盖几张牌
MAX_HITS = 12
# 这些字段必须是整数（或 "" 这种分组引用），否则会让解析结果变成字符串
NUMERIC_FIELDS = {
    "damage", "draw", "heal", "armor", "buff_atk", "buff_hp",
    "summon_atk", "summon_hp", "summon_count", "mana", "min_atk",
    "cost_delta_self", "cost_delta_opp",
}              # 一条规则最多能命中几张牌（防「造成X点伤害」这种万能规则）


FIELDS = ("damage", "scope", "draw", "heal", "buff_atk", "buff_hp",
          "destroy", "freeze", "summon_atk", "summon_hp", "mana", "armor",
          "discover", "known")


def _sig_diff(before: tuple, after: tuple) -> str:
    """列出到底哪个字段变了，例如 scope: target→all_minions。"""
    parts = []
    for name, old, new in zip(FIELDS, before, after):
        if old != new:
            parts.append(f"{name}: {old}→{new}")
    return "；".join(parts) or "（无差异）"


def _effect_signature(card) -> tuple:
    """把解析结果压成可比较的元组。"""
    e = parse_effect(card)
    return (e.damage, e.scope, e.draw, e.heal, e.buff_atk, e.buff_hp,
            e.destroy, e.freeze, e.summon_atk, e.summon_hp, e.mana, e.armor,
            e.discover, e.known)


def _load_rules(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "keywords": {}, "patterns": []}


def _apply_one(card, rule: dict[str, Any]) -> dict[str, Any] | None:
    """只针对这一条规则做一次匹配，返回它命中的字段。"""
    regex = str(rule.get("regex") or "")
    if not regex:
        return None
    text = card.text or ""
    text = re.sub(r"<[^>]+>", "", text).replace("$", "").replace("#", "")
    m = re.search(regex, text)
    if not m:
        return None
    effect = rule.get("effect") or {}
    out: dict[str, Any] = {}
    for field, raw in effect.items():
        value: Any = raw
        if isinstance(raw, str) and raw.lstrip("-").startswith("$"):
            neg = raw.startswith("-")
            key = raw.lstrip("-")[1:]
            if key.startswith("cn"):
                token = m.group(int(key[2:]))
                value = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
                         "五": 5, "六": 6, "七": 7}.get(token, 1)
            else:
                try:
                    value = int(m.group(int(key)))
                except (IndexError, ValueError):
                    continue
            if neg:
                value = -value
        out[field] = value
    return out


def validate_rule(cards, rule: dict[str, Any], target_ids: list[str]) -> tuple[bool, str]:
    """验证一条候选规则。返回 (是否通过, 原因)。"""
    if not isinstance(rule, dict) or not rule.get("regex"):
        return False, "规则缺少 regex"

    # 数值字段必须是整数或 "" 引用 —— 字符串会让解析结果变成脏数据
    effect = rule.get("effect") or {}
    # 闸门⑤：字段必须是 Effect 真有的。写个没人认的字段（如 adjacent_targets）
    # 会被 parse_effect 用 setattr 静默挂上去 —— 看起来「规则生效了、校验也过了」，
    # 实际没有任何结算。
    known_fields = {f.name for f in dataclass_fields(Effect)}
    unknown = sorted(set(effect) - known_fields)
    if unknown:
        return False, f"字段 {unknown} 不是 Effect 的字段，引擎不会结算"
    for field, value in effect.items():
        if field not in NUMERIC_FIELDS:
            continue
        if isinstance(value, int):
            continue
        if isinstance(value, str):
            if value.lstrip("-").startswith("$"):
                continue
            # AI 常把数字写成字符串（"2"）：转成整数就行，没必要拒绝
            if value.strip().lstrip("+-").isdigit():
                effect[field] = int(value.strip())
                continue
        return False, f"字段 {field} 不是数字：{value!r}"

    # 闸门④：费用变化写进 mana 会被引擎当成「白送法力」，必须用新字段
    regex_text = str(rule.get("regex") or "")
    if ("mana" in effect and ("法力值消耗" in regex_text or "费用" in regex_text)
            and not ("获得" in regex_text or "水晶" in regex_text)):
        return False, "费用变化请用 cost_delta_self / cost_delta_opp，不要写 mana"

    # 闸门②：先记录全库现状
    before = {cid: _effect_signature(c) for cid, c in cards.by_id.items()}

    hits = [cid for cid, c in cards.by_id.items() if _apply_one(c, rule) is not None]

    # 闸门①：目标牌至少命中一张
    if not any(cid in hits for cid in target_ids):
        return False, "目标牌没被这条规则命中"

    # 闸门③：不能太宽泛
    # 「造成X点伤害」这种规则几乎会命中全库，虽然合并逻辑不会覆盖已有结果，
    # 但它说明规则写得没有针对性，拒绝掉更安全
    if len(hits) > MAX_HITS:
        return False, f"规则太宽泛：命中了 {len(hits)} 张牌（上限 {MAX_HITS}）"
    newly = [cid for cid in hits if not before[cid][-1]]      # 原本未建模、现在能解析
    if len(newly) > MAX_NEW_COVER:
        # 命中的牌如果和目标是同一段文本（同一张牌的多个版本），不算宽泛
        target_texts = {(cards.get(t).text or "") for t in target_ids if cards.get(t)}
        new_texts = {(cards.get(n).text or "") for n in newly if cards.get(n)}
        if not (target_texts and new_texts <= target_texts):
            return False, f"规则太宽泛：一次新覆盖 {len(newly)} 张牌（上限 {MAX_NEW_COVER}）"

    # 闸门②：模拟应用后，已有的解析结果不能变

    model = __import__("hsai.model", fromlist=["x"])
    saved = model._EFFECT_TABLE
    try:
        table = _load_rules(EFFECTS_PATH)
        table.setdefault("patterns", []).insert(0, rule)
        model._EFFECT_TABLE = table
        after = {cid: _effect_signature(c) for cid, c in cards.by_id.items()}
        # 目标牌本来就该变（从「未建模」变成「能解析」），不算破坏
        targets = set(target_ids)
        changed = [cid for cid in after
                   if after[cid] != before[cid] and cid not in targets]
    finally:
        model._EFFECT_TABLE = saved
    if changed:
        names = []
        for cid in changed[:3]:
            card = cards.get(cid)
            if card is not None:
                names.append(f"{card.name}：{_sig_diff(before[cid], after[cid])}")
            else:
                names.append(cid)
        detail = "、".join(names)
        return False, f"会改变 {len(changed)} 张已有卡牌：{detail}"
    return True, f"通过：新覆盖 {len(newly)} 张"
