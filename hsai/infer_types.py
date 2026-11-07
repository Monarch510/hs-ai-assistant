"""infer 的数据类与纯函数层。

门面 infer.py 保留 DeckPredictor 类本体；这里只放数据类、纯谓词和它们用到的
常量。子模块不 import infer 门面。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .cards import Card
from .tag_rules import RULES

# 旧 classify 的 is_aoe/is_single_removal 已删，功能标签口径见 hsai/tag_rules.py
# （CONTEXT.md 的 12 类）。单解排除群解：同一张牌不能同时进 aoe_risk 和 removal_risk 两行。
_is_aoe = RULES["群解"]


def _is_single_removal(card: Card) -> bool:
    return RULES["单解"](card) and not _is_aoe(card)


# 局末真值的两条门槛。低于任何一条都记成「不可信」，不参与先验学习 ——
# 宁可少学几局，也不要拿猜出来的标签去训练。
TRUST_COVERAGE = 0.85   # 模板至少解释得了 85% 的已见牌
TRUST_GAP = 0.08        # 且比第二名高出 8 个百分点，否则两套牌区分不开


@dataclass
class Candidate:
    id: str
    name: str
    confidence: float
    class_cn: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class Prediction:
    name: str = "未知"
    archetype_id: str = ""
    confidence: float = 0.0
    candidates: list[Candidate] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    key_cards: list[dict[str, Any]] = field(default_factory=list)
    next_threats: list[str] = field(default_factory=list)
    likely_hand: list[dict[str, Any]] = field(default_factory=list)
    aoe_risk: float = 0.0
    removal_risk: float = 0.0
    remaining: dict[str, float] = field(default_factory=dict)
    prior_only: bool = False
    note: str = ""
    source: str = ""
    estimated: bool = False       # 数字是不是「估的」（库里没这套牌时）

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "id": self.archetype_id,
            "confidence": round(self.confidence, 3),
            "candidates": [
                {"id": c.id, "name": c.name, "confidence": round(c.confidence, 3), "tags": c.tags}
                for c in self.candidates
            ],
            "evidence": self.evidence,
            "key_cards": self.key_cards,
            "next_threats": self.next_threats,
            "likely_hand": self.likely_hand,
            "aoe_risk": round(self.aoe_risk, 3),
            "removal_risk": round(self.removal_risk, 3),
            "prior_only": self.prior_only,
            "note": self.note,
            "source": self.source,
            "estimated": self.estimated,
        }


@dataclass
class RetroTruth:
    """局末回顾得到的「对手真值卡组」。

    和 `Prediction` 是两件事：`Prediction` 回答「此刻我认为它是什么」，
    `RetroTruth` 回答「打完之后用全部已见牌回看，它最像哪套牌」。

    只有后者能当标签用来学习。以前 `observations.jsonl` 只存了预测值，
    先验学习拿它当真值 —— 预测偏哪套，先验就往哪套偏，形成自我确认。
    """

    archetype_id: str = ""
    name: str = "未知"
    coverage: float = 0.0
    explained: int = 0
    considered: int = 0
    rival_id: str = ""
    rival_coverage: float = 0.0
    unexplained: list[str] = field(default_factory=list)

    @property
    def trusted(self) -> bool:
        """这套真值够不够格当学习标签。

        覆盖率低 = 模板解释不了大部分牌，多半是没见过的卡组；
        和第一名咬得太紧 = 两套原型在这批牌上区分不开，标签本身就是猜的。
        两种都不该写进先验统计，否则等于拿噪声训练。
        """
        if not self.archetype_id or self.coverage < TRUST_COVERAGE:
            return False
        return not self.rival_id or self.coverage - self.rival_coverage >= TRUST_GAP

    def as_dict(self) -> dict[str, Any]:
        return {
            "arch": self.archetype_id,
            "name": self.name,
            "coverage": round(self.coverage, 3),
            "considered": self.considered,
            "rival": self.rival_id,
            "rival_coverage": round(self.rival_coverage, 3),
            "trusted": self.trusted,
        }
