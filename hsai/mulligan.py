"""留牌建议。

留牌是炉石里性价比最高的决策：起手 3-4 张牌，留错一整局都在还债。
这里的打分是「可调的启发式」，不是从百万局数据里拟合的模型：

    分数 = 费用基础分 + 对位修正(快攻/控制) + 功能修正(解牌/清场/嘲讽/治疗) + 重复惩罚

所有修正量都在 `DEFAULT_WEIGHTS` 里，`hsai learn` 会用你自己的对局结果去调它。
所以一开始它只能说「大概」，用得越久越贴合你的卡组与分段。
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Iterable

from .cards import CLASS_CN, Card, CardDB
from .infer import Prediction
from .tag_rules import RULES, TAG_ORDER
from .tag_weights import Situation, explain, tag_weights

# 可学习权重：名字 -> 默认值
DEFAULT_WEIGHTS: dict[str, float] = {
    # 费用曲线基础分（0..7，7 表示 7 费及以上）
    "cost_0": 74.0, "cost_1": 80.0, "cost_2": 76.0, "cost_3": 64.0,
    "cost_4": 52.0, "cost_5": 40.0, "cost_6": 32.0, "cost_7": 24.0,
    # 对位修正
    "vs_aggro_cheap": 8.0,      # 对快攻，低费牌更该留
    "vs_aggro_expensive": -14.0,  # 对快攻，高费牌该换
    "vs_control_expensive": 7.0,
    "vs_control_cheap": -3.0,
    # 功能修正
    "keep_aoe_vs_aggro": 18.0,
    "keep_removal_vs_aggro": 7.0,
    "keep_taunt_vs_aggro": 10.0,
    "keep_heal_vs_aggro": 9.0,
    "keep_value_vs_control": 6.0,
    "keep_weapon": 6.0,
    # 其他
    "duplicate_penalty": -11.0,
    "second_player_bonus": 4.0,
}

AGGRO_TAGS = {"快攻", "打脸", "铺场", "节奏"}

# 认不出对面形态时的「按职业兜底」：天梯里每个职业最常见的节奏取向。
# 这不是精确统计，只是为了在「未知卡组」时也能给出有职业针对性的留牌，
# 而不是所有对局都套同一套通用规则。
CLASS_STYLE: dict[str, tuple[list[str], str]] = {
    "WARRIOR": (["节奏"], "中速"),
    "HUNTER": (["快攻"], "快攻"),
    "ROGUE": (["节奏"], "节奏"),
    "DEMONHUNTER": (["快攻"], "快攻"),
    "PALADIN": (["中速"], "中速"),
    "SHAMAN": (["中速"], "中速"),
    "DRUID": (["跳费"], "跳费"),
    "MAGE": (["节奏"], "节奏"),
    "WARLOCK": (["铺场"], "铺场"),
    "PRIEST": (["控制"], "控制"),
    "DEATHKNIGHT": (["中速"], "中速"),
}
CONTROL_TAGS = {"控制", "清场", "后期", "护甲", "治疗"}

KEEP_THRESHOLD = 55.0

# 动态标签权重的缩放：单牌标签权重和一般 1~4，×3 后附加分约 3~12 分。
# 极端局面（斩杀信号 + 多标签牌）可能超过 15，所以整项再钳一次，保证附加分落在
# ±15 区间、不超过相邻费用档差（4~16 分），只做排序微调，不推翻费用基础分。
MULLIGAN_SCALE = 3.0
MULLIGAN_BONUS_CAP = 15.0

# explain 的这几句是在讲阶段/无信号，不是本局加权的理由
_EXPLAIN_SKIP = ("留牌阶段", "出牌阶段", "没有额外的")


def _as_situation(value: Situation | dict | None) -> Situation | None:
    """dict 也收：调用方从 JSON/配置拼局势时不用先转 dataclass。

    按 dataclass 字段反射取值，快照里多出来的键直接忽略（快照常带额外字段），
    也不写死 A 的字段清单，免得 A 改接口时这边漏改。
    """
    if value is None or isinstance(value, Situation):
        return value
    names = {f.name for f in fields(Situation)}
    return Situation(**{k: v for k, v in value.items() if k in names})


def _tag_reason(s: Situation | None) -> str:
    """动态权重的人话理由，取自 explain；没有额外对位/局势信号时返回空串。"""
    if s is None:
        return ""
    for line in explain(s):
        if not line.startswith(_EXPLAIN_SKIP):
            return line
    return ""


@dataclass
class MulliganEntry:
    card_id: str
    name: str
    cost: int
    action: str          # keep | toss
    score: float
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.card_id,
            "name": self.name,
            "cost": self.cost,
            "action": self.action,
            "score": round(self.score),
            "reason": self.reason,
        }


def _cost_score(cost: int, w: dict[str, float]) -> float:
    return w.get(f"cost_{min(cost, 7)}", w["cost_7"])


def _opp_style(pred: Prediction | None) -> set[str]:
    if pred is None:
        return set()
    tags: set[str] = set()
    if pred.candidates:
        tags.update(pred.candidates[0].tags)
    return tags


def advise(
    hand: Iterable[Card],
    *,
    cards: CardDB,
    prediction: Prediction | None = None,
    our_tags: Iterable[str] = (),
    is_first: bool = True,
    weights: dict[str, float] | None = None,
    opp_class: str = "",
    situation: Situation | dict | None = None,
    tag_weight_overrides: dict[str, float] | None = None,
    tag_scale: float = MULLIGAN_SCALE,
) -> list[dict[str, Any]]:
    """返回每张牌的留/换决策与理由，按分数从高到低。

    opp_class 是兜底：当预判是「未知卡组」时，按该职业最常见的形态来留牌，
    否则所有对局都会退化成同一套通用建议。

    situation 传入后按当前局势追加动态标签权重（DYNAMIC-WEIGHTS.md §3），
    不传则与旧行为逐字节一致；tag_weight_overrides 只覆盖权重基础表，
    且只在传了 situation 时生效；tag_scale 缩放附加分（默认 MULLIGAN_SCALE，
    配置由 advisor 读 data/tag_weights.json 后传入）。
    """
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    sit = _as_situation(situation)
    tag_w = tag_weights(sit, tag_weight_overrides) if sit is not None else None
    tag_note = _tag_reason(sit)
    opp_tags = set(_opp_style(prediction))
    style_note = ""
    if not opp_tags and opp_class:
        tags, label = CLASS_STYLE.get(opp_class, ([], ""))
        opp_tags = set(tags)
        if label:
            style_note = f"对位 {CLASS_CN.get(opp_class, opp_class)}（形态未知，按{label}处理）"
    vs_aggro = bool(opp_tags & AGGRO_TAGS)
    vs_control = bool(opp_tags & CONTROL_TAGS)
    our_aggro = bool(set(our_tags) & AGGRO_TAGS)

    entries: list[MulliganEntry] = []
    seen: dict[str, int] = {}
    for card in hand:
        if card is None:
            continue
        score = _cost_score(card.cost, w)
        reasons: list[str] = []
        # 一张牌只算一次标签，别在分支里反复扫卡面。口径见 hsai/tag_rules.py 与
        # docs/card-tagging/CONTEXT.md。
        tags = {t for t in TAG_ORDER if RULES[t](card)}

        if vs_aggro:
            if card.cost <= 2:
                score += w["vs_aggro_cheap"]
                reasons.append("对快攻要有前期场面")
            elif card.cost >= 5:
                score += w["vs_aggro_expensive"]
                reasons.append("对快攻，高费牌起手太拖")
            if "群解" in tags:
                if card.cost <= 5:
                    score += w["keep_aoe_vs_aggro"]
                    reasons.append("清场是打快攻的关键牌")
                else:
                    reasons.append("清场虽好，但这个费用起手会卡死")
            elif "单解" in tags:
                score += w["keep_removal_vs_aggro"]
                reasons.append("单解能处理对方前期核心")
            if card.has("TAUNT"):  # 嘲讽是关键词（mechanics），不是 11 类功能标签
                score += w["keep_taunt_vs_aggro"]
                reasons.append("嘲讽能挡住打脸")
            if tags & {"治疗", "护甲"}:  # 回血和叠甲都算稳血线
                score += w["keep_heal_vs_aggro"]
                reasons.append("回复能撑过爆发")
        elif vs_control:
            if card.cost >= 5:
                score += w["vs_control_expensive"]
                reasons.append("对控制，后期价值牌要留")
            elif card.cost <= 2:
                score += w["vs_control_cheap"]
                reasons.append("对控制，纯前期的低费牌没那么急")
            if card.cost >= 4 and not (tags & {"群解", "单解"}):
                score += w["keep_value_vs_control"]
                reasons.append("往后拖需要价值")

        if our_aggro and card.cost >= 4:
            score -= 6
            reasons.append("你自己的卡组偏快，4 费以上起手偏重")
        if card.is_weapon:
            score += w["keep_weapon"]
            reasons.append("武器能持续换血")
        if not is_first:
            score += w["second_player_bonus"]

        n = seen.get(card.id, 0)
        seen[card.id] = n + 1
        if n >= 1:
            score += w["duplicate_penalty"] * n
            reasons.append("同名牌留一张就够")

        if tag_w is not None and tags:
            # 按 TAG_ORDER 求和：set 遍历顺序随哈希种子变，浮点尾数要稳定。
            bonus = tag_scale * sum(tag_w[t] for t in TAG_ORDER if t in tags)
            bonus = max(-MULLIGAN_BONUS_CAP, min(MULLIGAN_BONUS_CAP, bonus))
            score += bonus
            if tag_note and bonus:
                reasons.append(tag_note)

        score = max(0.0, min(100.0, score))
        action = "keep" if score >= KEEP_THRESHOLD else "toss"
        if style_note:
            reasons.insert(0, style_note)
        if not reasons:
            reasons.append("费用曲线尚可" if action == "keep" else "起手偏贵或作用有限")
        entries.append(
            MulliganEntry(card.id, card.name, card.cost, action, score, "；".join(reasons[:3]))
        )

    entries.sort(key=lambda e: -e.score)
    return [e.as_dict() for e in entries]


def summary(entries: list[dict[str, Any]]) -> str:
    keep = [e["name"] for e in entries if e["action"] == "keep"]
    toss = [e["name"] for e in entries if e["action"] == "toss"]
    return f"留：{'、'.join(keep) or '无'}｜换：{'、'.join(toss) or '无'}"
