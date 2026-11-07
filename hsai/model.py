"""局面模型：卡牌效果解释 + 局面数据结构 + 评估函数。

这个模块现在是**门面**：
  · model_effects.py  卡牌文本 → Effect（parse_effect / warden_buff / 英雄技能）
  · model_units.py    单位、一方状态、棋盘（Unit / SideState / Board）
  · model_eval.py     评分（evaluate / resource_score / DEFAULT_WEIGHTS）

为什么 _EFFECT_TABLE / effect_table() / _DB / set_card_db() 还留在这里：
effects_ai.py 用 `model._EFFECT_TABLE = ...` **赋值**来切换规则表，测试也用
`mock.patch.object(model, "_EFFECT_TABLE", ...)`。这些是「可被外部重新绑定的模块
全局」，必须与门面是同一个对象；搬进子模块会让 patch 打到门面、读到子模块，静默失效。
所以缓存与读缓存的两个函数物理留在这里，子模块在函数内部 `from . import model` 回查。

改卡牌效果时记住这条铁律：Effect 字段（model_effects）、parse_effect（model_effects）、
apply_effect（model_units）三处必须一起改；evaluate 读的每个权重键都必须在
DEFAULT_WEIGHTS（model_eval）里。
"""

from __future__ import annotations

import json
from typing import Any

from .cards import Card

# 关键词/文本效果的解释表（data/effects.json），改表就能改行为
_EFFECT_TABLE: dict[str, Any] | None = None


def effect_table() -> dict[str, Any]:
    global _EFFECT_TABLE
    if _EFFECT_TABLE is not None:
        return _EFFECT_TABLE
    from .paths import EFFECTS_JSON

    path = EFFECTS_JSON
    try:
        _EFFECT_TABLE = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _EFFECT_TABLE = {"keywords": {}, "patterns": []}
    return _EFFECT_TABLE


_DB = None


def set_card_db(db) -> None:
    """让评估函数能查到场上随从的卡面文本（场上资源用）。"""
    global _DB
    _DB = db


# ---- 子模块（三者顶层都不 import 本门面，无环） ----
from .model_effects import (  # noqa: E402
    Effect,
    _as_int,
    _CN_NUM,
    _CN_NUM_EXTRA,
    _DMG_AMOUNT_RE,
    _DRAW_RE,
    _HEAL_RE,
    _BUFF_RE,
    _SUMMON_RE,
    _num,
    _pattern_effect,
    hero_power_effect,
    is_passive_hero_power,
    parse_effect,
    warden_buff,
)
from .model_units import Board, SideState, Unit  # noqa: E402
from .model_eval import (  # noqa: E402
    DEFAULT_WEIGHTS,
    _adjacency_value,
    evaluate,
    resource_score,
)

__all__ = [
    "Card",
    "Effect",
    "Unit",
    "SideState",
    "Board",
    "parse_effect",
    "warden_buff",
    "hero_power_effect",
    "is_passive_hero_power",
    "effect_table",
    "set_card_db",
    "evaluate",
    "resource_score",
    "DEFAULT_WEIGHTS",
    "_EFFECT_TABLE",
    "_DB",
]
