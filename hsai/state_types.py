"""state 的类型与纯工具层。

门面 state.py 保留 GameState 本体（apply / snapshot / _on_* 全在这里），
这里只放不读 GameState 实例字段的常量、数据类和纯函数。子模块不 import 门面，
靠参数传入的 state 对象回查 GameState 的方法即可。
"""

from __future__ import annotations

import re

from dataclasses import dataclass, field
from typing import Any

T_ZONE = "ZONE"
T_CONTROLLER = "CONTROLLER"
T_ATK = "ATK"
T_HEALTH = "HEALTH"
T_DAMAGE = "DAMAGE"
T_PLAYER_ID = "PLAYER_ID"
T_PLAYSTATE = "PLAYSTATE"
T_TURN = "TURN"
T_CURRENT_PLAYER = "CURRENT_PLAYER"
T_RESOURCES = "RESOURCES"
T_RESOURCES_USED = "RESOURCES_USED"
T_TEMP_RESOURCES = "TEMP_RESOURCES"
T_ARMOR = "ARMOR"
T_TAUNT = "TAUNT"
T_DIVINE_SHIELD = "DIVINE_SHIELD"
T_COST = "COST"
T_HERO_ENTITY = "HERO_ENTITY"
T_ATTACKS_THIS_TURN = "NUM_ATTACKS_THIS_TURN"

ZONE_PLAY, ZONE_DECK, ZONE_HAND, ZONE_GRAVE, ZONE_SECRET, ZONE_SETASIDE = 1, 2, 3, 4, 5, 7


@dataclass
class Entity:
    id: int
    card_id: str | None = None
    controller: int | None = None
    zone: int = 0
    zone_pos: int | None = None
    tags: dict[int, Any] = field(default_factory=dict)

    @property
    def atk(self) -> int | None:
        return _int(self.tags.get(T_ATK))

    @property
    def health(self) -> int | None:
        return _int(self.tags.get(T_HEALTH))

    @property
    def damage(self) -> int:
        return _int(self.tags.get(T_DAMAGE)) or 0

    @property
    def armor(self) -> int:
        return _int(self.tags.get(T_ARMOR)) or 0

    @property
    def cost(self) -> int | None:
        return _int(self.tags.get(T_COST))

_EFFECT_CACHE: dict[str, tuple[str, bool]] = {}


def _cached_effect(state: "GameState", card_id: str | None) -> str:
    """效果摘要（带缓存）。给 AI 看牌面用，避免每帧重复解析。"""
    if not card_id:
        return ""
    hit = _EFFECT_CACHE.get(card_id)
    if hit is None:
        hit = state._effect_text(card_id)
        _EFFECT_CACHE[card_id] = hit
    summary, known = hit
    if not known:
        return ""
    # 随从没有可解析效果时，摘要就是「攻/血 + 关键字」，和前面重复了，丢掉
    if re.match(r"^\d+/\d+", summary):
        return ""
    return summary


def _clean_text(raw: str) -> str:
    """去掉富文本标记：<b>、$（伤害）、#（治疗）、&（换行）、{0}（占位符）。"""
    t = re.sub(r"<[^>]+>", "", raw or "")
    t = t.replace("$", "").replace("#", "").replace("&", "")
    t = re.sub(r"\{\d+\}", "", t)
    return " ".join(t.split())


def _int(v: Any) -> int | None:
    """把标签值转成整数；转不了就返回 None 并记一行日志（不崩）。"""
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        try:
            return int(v)
        except ValueError:
            from .logutil import log as _log
            _log(f"标签不是整数：{v!r}")
    return None


@dataclass
class Side:

    player_id: int
    class_cn: str = "未知"
    class_en: str = ""
    health: int = 30
    armor: int = 0
    mana: int = 0
    max_mana: int = 0
    fatigue: int = 0
    hand: list[dict[str, Any]] = field(default_factory=list)
    hand_count: int = 0
    board: list[dict[str, Any]] = field(default_factory=list)
    deck_count: int = 0
    # True 表示牌库数是「卡组张数 − 手牌 − 场面」的粗估（没赶上 CREATE_GAME，
    # 拿不到初始牌库实体），不是真值
    deck_estimated: bool = False
    # SECRET 区混着三类实体："" 表示客户端没给 cardId 的隐藏奥秘
    secrets: list[str] = field(default_factory=list)
    quests: list[dict[str, Any]] = field(default_factory=list)      # 任务 / 任务线 / 支线任务
    ongoing: list[dict[str, Any]] = field(default_factory=list)     # 咒符等持续法术
    graveyard: list[dict[str, Any]] = field(default_factory=list)   # 已死的随从
    weapon: dict[str, Any] | None = None
    hero_power: dict[str, Any] | None = None
    hero_power_used: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "class_cn": self.class_cn,
            "class_en": self.class_en,
            "health": self.health,
            "armor": self.armor,
            "mana": self.mana,
            "max_mana": self.max_mana,
            "hand": self.hand,
            "hand_count": self.hand_count,
            "board": self.board,
            "deck_count": self.deck_count,
            "deck_estimated": self.deck_estimated,
            "secrets": self.secrets,
            "quests": self.quests,
            "ongoing": self.ongoing,
            "weapon": self.weapon,
            "hero_power": self.hero_power,
            "hero_power_used": self.hero_power_used,
            # 只给数量：墓地内容在 advisor 里按需查（复活推荐/亡语池），
            # 面板上只关心「死了几个」，整份列表塞进快照没必要
            "graveyard_count": len(self.graveyard),
        }
