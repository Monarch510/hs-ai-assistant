"""动态标签权重：局势 → 12 类标签权重的纯函数层。

口径见 docs/card-tagging/DYNAMIC-WEIGHTS.md §2。留牌和出牌共用这一套
（各自再乘缩放系数，缩放系数在调用方），所以这里只回答一件事：
「这个局势下哪类牌更重要」。

为什么全是查表 + 线性信号
--------------------------
引擎侧不能读标签缓存（没 build 过就会静默算 0），权重同理：逻辑全写在代码里，
无 IO、无随机、无时间，同样的 Situation 永远给同样的表。每个连续信号先
线性映射到 [-1, +1] 再钳制，乘一个系数叠加到基础分上 —— 极端局面不会爆分，
而且每条调整都能用 explain() 说成人话。
"""

from __future__ import annotations

from dataclasses import dataclass

from .tag_rules import TAG_ORDER

# 基础分：12 类，键顺序跟 TAG_ORDER 一致。「未知」是看不出效果的牌，给 0 不偏不倚。
# 「保护」是 #21 从增益里分出来的防御维度：与治疗同级（0.5），低于进攻向的解场/斩杀，
# 高于纯叠甲；局势修正表里没有它的系数，等于只吃基础分，后续可单独调。
DEFAULT_TAG_WEIGHTS: dict[str, float] = {
    "单解": 0.8,
    "群解": 0.8,
    "站场": 1.0,
    "增益": 0.6,
    "减益": 0.6,
    "治疗": 0.5,
    "护甲": 0.4,
    "资源": 0.7,
    "抽牌": 0.7,
    "斩杀": 0.9,
    "未知": 0.0,
    "保护": 0.5,
}

# 最终权重的兜底边界。真正的钳制在信号上（±1），这里只防多项叠加后的极端值。
_MIN_W, _MAX_W = -2.0, 6.0

@dataclass(frozen=True)
class Situation:
    our_class: str = ""
    opp_class: str = ""
    our_archetype: str = ""
    opp_archetype: str = ""
    # 合同里这两个没给默认值，但 dataclass 不允许「有默认值」后面再跟「无默认值」，
    # 补上空的 frozenset（不可变，做默认值安全）。
    opp_style: frozenset[str] = frozenset()
    our_tags: frozenset[str] = frozenset()
    our_hp: int = 30
    our_armor: int = 0
    opp_hp: int = 30
    opp_armor: int = 0
    our_board_attack: int = 0
    opp_board_attack: int = 0
    our_board_count: int = 0
    opp_board_count: int = 0
    our_hand_count: int = 0
    opp_hand_count: int = 0
    our_mana: int = 0          # 本回合没花完的余费，0 = 无数据/已花完
    turn: int = 1
    aoe_risk: float = 0.0
    removal_risk: float = 0.0
    lethal: bool = False
    phase: str = "playing"     # mulligan | playing

def _clamp(x: float) -> float:
    return max(-1.0, min(1.0, x))

def _style_tables():
    # 延迟导入：mulligan 将来要反过来 import 本模块拿 Situation，顶层导入会成环；
    # 函数内导入发生在调用时，那时两个模块都已加载完。职业取向表直接复用，不另造。
    from .mulligan import AGGRO_TAGS, CLASS_STYLE, CONTROL_TAGS
    return AGGRO_TAGS, CONTROL_TAGS, CLASS_STYLE

def _styles(s: Situation):
    """(对快攻, 对控制, 我方偏快, 我方偏控制, 职业兜底说明)。"""
    aggro, control, class_style = _style_tables()
    # 对位形态优先用预判给的标签；没标签看卡组名；再没有才按职业兜底。
    opp = set(s.opp_style)
    if not opp and s.opp_archetype:
        opp = {w for w in aggro | control if w in s.opp_archetype}
    label = ""
    if not opp and s.opp_class:
        tags, label = class_style.get(s.opp_class.upper(), ([], ""))
        opp = set(tags)
    our = set(s.our_tags)
    if not our and s.our_archetype:
        our = {w for w in aggro | control if w in s.our_archetype}
    return (bool(opp & aggro), bool(opp & control),
            bool(our & aggro), bool(our & control), label)

def _contributions(s: Situation):
    """[(每类系数, 已钳制信号, 正向人话, 反向人话), ...]。

    tag_weights 和 explain 共用这一份清单，保证「解释」和「权重」不会各说一套。
    """
    out: list[tuple[dict[str, float], float, str, str]] = []

    def add(coefs, signal, up, down=None):
        sig = _clamp(signal)
        if abs(sig) < 1e-9:
            return
        out.append((coefs, sig, up, down or up))

    vs_aggro, vs_control, our_aggro, our_control, label = _styles(s)
    if label:
        add({}, 1.0, f"对面形态未知，按职业取向当作「{label}」处理")

    add({"群解": .5, "单解": .4, "治疗": .4, "护甲": .35, "增益": .2,
         "减益": .15, "站场": .1, "资源": -.25, "抽牌": -.25},
        int(vs_aggro), "对面偏快攻/铺场：清场、单解、回复、护甲加权，高费补牌降权")
    add({"资源": .4, "抽牌": .4, "斩杀": .3, "群解": -.3, "单解": -.3},
        int(vs_control), "对面偏控制/后期：资源、过牌、斩杀加权，解场牌降权")
    add({"斩杀": .35, "站场": .3, "治疗": -.25, "护甲": -.2, "资源": -.15},
        int(our_aggro), "我方偏快：抢血与铺场优先，回复和囤牌降权")
    add({"群解": .3, "治疗": .3, "护甲": .3, "资源": .3},
        int(our_control), "我方偏控制：清场、回复、资源加权")

    if s.phase == "mulligan":
        return out

    add({"治疗": .6, "护甲": .55, "群解": .2, "单解": .2, "斩杀": -.5},
        (30 - (s.our_hp + s.our_armor)) / 20,
        "我方血线偏低：治疗/护甲加权、斩杀降权，先活下来",
        "我方血线健康：更敢抢血，回复牌降权")
    add({"斩杀": .8, "群解": -.35, "单解": -.35, "减益": -.3},
        (30 - (s.opp_hp + s.opp_armor)) / 20,
        "对面血线偏低：斩杀加权、解场降权，能斩就别解",
        "对面血线很高：先铺垫，斩杀牌降权")
    add({"群解": .5, "单解": .4, "减益": .35, "治疗": .15, "护甲": .15},
        s.opp_board_attack / 8 + s.opp_board_count / 6,
        "对面场攻/随从多：群解、单解、减益加权")
    add({"站场": .35, "增益": .3, "斩杀": .35, "群解": -.2, "单解": -.2},
        (s.our_board_attack - s.opp_board_attack) / 8
        + (s.our_board_count - s.opp_board_count) / 6,
        "我方场面占优：站场/增益/抢血加权",
        "对面场面占优：铺场/抢血降权，更该解场")
    # 手牌 0 视为「没读到」，不猜一个数字（AGENTS 降级口径）。
    low_hand = (3 - s.our_hand_count) / 3 if s.our_hand_count else 0.0
    add({"资源": .45, "抽牌": .45}, low_hand,
        "手牌偏少：资源/抽牌加权", "手牌充足：补牌降权")
    add({"资源": .3, "抽牌": .3}, s.our_mana / 6,
        "有余费没花完：资源/抽牌加权")
    spread = _clamp(s.our_board_count / 4)
    add({"站场": -.5, "增益": -.2, "减益": .3}, _clamp(s.aoe_risk) * spread,
        "对面群解风险高且我方铺场多：别再铺，减益优先")
    add({"站场": -.2, "增益": -.15}, _clamp(s.removal_risk) * spread,
        "对面单解风险高且我方铺场多：大随从站场略降权")
    add({"资源": .3, "抽牌": .3, "斩杀": .25}, (s.turn - 1) / 12,
        "回合数偏大（后期/疲劳）：资源、过牌、斩杀加权")
    # 斩杀仍由引擎的斩杀路线决定，这里只把排序压到最顶。
    add({"斩杀": 2.5}, int(s.lethal), "本回合能斩杀：斩杀权重压倒性置顶")
    return out

def _normalize_overrides(overrides) -> dict[str, float]:
    """支持 {"单解": 1.2} 与 {"tag_单解": 1.2} 两种键名；不认识的键忽略。"""
    if not overrides:
        return {}
    out: dict[str, float] = {}
    for key, value in overrides.items():
        tag = key[4:] if isinstance(key, str) and key.startswith("tag_") else key
        if tag in DEFAULT_TAG_WEIGHTS:
            out[tag] = float(value)
    return out

def tag_weights(s: Situation, overrides: dict | None = None) -> dict[str, float]:
    """局势 → 12 类权重，固定 TAG_ORDER 顺序、可复现、无 IO。

    overrides 是基础表的局部覆盖（跟 mulligan 的 weights 一个写法）：只列要改的键，
    其余仍用 DEFAULT_TAG_WEIGHTS，局势修正照常叠加。
    """
    w = {**DEFAULT_TAG_WEIGHTS, **_normalize_overrides(overrides)}
    for coefs, sig, _, _ in _contributions(s):
        for tag, coef in coefs.items():
            w[tag] += coef * sig
    return {t: max(_MIN_W, min(_MAX_W, w[t])) for t in TAG_ORDER}

def explain(s: Situation) -> list[str]:
    """人话：这个局势下哪几类权重为什么变了（UI 直接显示）。"""
    lines = ["留牌阶段：只看职业/对位，忽略场面信号"
             if s.phase == "mulligan" else "出牌阶段：叠加场面连续信号"]
    reasons = [up if sig > 0 else down
               for _, sig, up, down in _contributions(s) if abs(sig) > 0.05]
    if reasons:
        lines.extend(reasons)
    else:
        lines.append("没有额外的对位或局势信号，各类标签用基础权重")
    return lines

__all__ = ["Situation", "DEFAULT_TAG_WEIGHTS", "tag_weights", "explain"]
