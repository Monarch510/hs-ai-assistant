"""卡牌数据库：从 HearthstoneJSON 抓取、规范化、缓存到本地。

只依赖标准库。缓存格式是紧凑的 list-of-list，加载 8000+ 张牌大约 100ms。
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .paths import DATA_DIR, cards_cache

HSJSON = "https://api.hearthstonejson.com/v1/latest/{locale}/{kind}.json"

# 卡牌类别 -> 中文名。日志里只有英文枚举。
CLASS_CN = {
    "DEATHKNIGHT": "死亡骑士",
    "DEMONHUNTER": "恶魔猎手",
    "DRUID": "德鲁伊",
    "HUNTER": "猎人",
    "MAGE": "法师",
    "PALADIN": "圣骑士",
    "PRIEST": "牧师",
    "ROGUE": "潜行者",
    "SHAMAN": "萨满祭司",
    "WARLOCK": "术士",
    "WARRIOR": "战士",
    "NEUTRAL": "中立",
    "DREAM": "梦境",
    "WHIZBANG": "威兹班",
}

TYPE_CN = {"MINION": "随从", "SPELL": "法术", "WEAPON": "武器", "HERO": "英雄", "LOCATION": "地标"}

# 基础英雄技能，按类别。日志里若出现英雄技能实体，以其真实 cardId 为准。
# 关键字机制 -> 中文
MECHANIC_CN = {
    "TAUNT": "嘲讽",
    "CHARGE": "冲锋",
    "RUSH": "突袭",
    "DIVINE_SHIELD": "圣盾",
    "WINDFURY": "风怒",
    "LIFESTEAL": "吸血",
    "POISONOUS": "剧毒",
    "STEALTH": "潜行",
    "DEATHRATTLE": "亡语",
    "BATTLECRY": "战吼",
    "SPELL_DAMAGE": "法术伤害",
    "AURA": "光环",
    "REBORN": "复生",
    "IMMUNE": "免疫",
    "FREEZE": "冻结",
    "DISCOVER": "发现",
    "CHOOSE_ONE": "抉择",
    "COMBO": "连击",
    "OVERLOAD": "过载",
}


@dataclass(frozen=True)
class Card:
    id: str
    name: str
    cost: int = 0
    card_class: str = "NEUTRAL"
    type: str = "MINION"
    set: str = ""
    rarity: str = ""
    atk: int = 0
    health: int = 0
    durability: int = 0
    armor: int = 0
    text: str = ""
    mechanics: tuple[str, ...] = ()
    races: tuple[str, ...] = ()
    spell_school: str = ""
    collectible: bool = True
    elite: bool = False
    child_ids: tuple[str, ...] = ()
    dbf: int = 0

    @property
    def class_cn(self) -> str:
        return CLASS_CN.get(self.card_class, self.card_class or "中立")

    @property
    def type_cn(self) -> str:
        return TYPE_CN.get(self.type, self.type or "随从")

    @property
    def keyword_cn(self) -> str:
        return "·".join(MECHANIC_CN.get(m, m) for m in self.mechanics if m in MECHANIC_CN)

    @property
    def is_minion(self) -> bool:
        return self.type == "MINION"

    @property
    def is_spell(self) -> bool:
        return self.type == "SPELL"

    @property
    def is_weapon(self) -> bool:
        return self.type == "WEAPON"

    def has(self, mechanic: str) -> bool:
        return mechanic in self.mechanics

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "cost": self.cost,
            "class": self.card_class,
            "class_cn": self.class_cn,
            "type": self.type,
            "type_cn": self.type_cn,
            "atk": self.atk,
            "health": self.health,
            "text": self.text,
            "mechanics": list(self.mechanics),
            "keyword": self.keyword_cn,
        }


_FIELDS = (
    "id", "name", "cost", "card_class", "type", "set", "rarity",
    "atk", "health", "durability", "armor", "text", "mechanics",
    "races", "spell_school", "collectible", "elite", "child_ids", "dbf",
)


def _norm_cost(raw: Any) -> int:
    return raw if isinstance(raw, int) else 0


def _parse_card(raw: dict[str, Any]) -> Card:
    return Card(
        id=raw.get("id") or "",
        name=raw.get("name") or "",
        cost=_norm_cost(raw.get("cost", 0)),
        card_class=raw.get("cardClass") or "NEUTRAL",
        type=raw.get("type") or "MINION",
        set=raw.get("set") or "",
        rarity=raw.get("rarity") or "",
        atk=raw.get("attack") or 0,
        health=raw.get("health") or 0,
        durability=raw.get("durability") or 0,
        armor=raw.get("armor") or 0,
        text=raw.get("text") or "",
        mechanics=tuple(sorted(raw.get("mechanics") or ())),
        races=tuple(raw.get("races") or (raw.get("race") and [raw["race"]]) or ()),
        spell_school=raw.get("spellSchool") or "",
        collectible=bool(raw.get("collectible", True)),
        elite=bool(raw.get("elite", False)),
        child_ids=tuple(raw.get("childIds") or ()),
        dbf=int(raw.get("dbfId") or 0),
    )


def _to_row(c: Card) -> list[Any]:
    row = []
    for f in _FIELDS:
        v = getattr(c, f)
        if isinstance(v, tuple):
            v = list(v)
        row.append(v)
    return row


def _from_row(row: list[Any]) -> Card:
    d = dict(zip(_FIELDS, row))
    d["mechanics"] = tuple(d.get("mechanics") or ())
    d["races"] = tuple(d.get("races") or ())
    d["child_ids"] = tuple(d.get("child_ids") or ())
    return Card(**d)


class CardDB:
    """卡牌查询。id 大小写不敏感；同时支持按中文名查。"""

    def __init__(self, cards: Iterable[Card]) -> None:
        self.by_id: dict[str, Card] = {}
        self.by_name: dict[str, Card] = {}
        self.by_dbf: dict[int, Card] = {}
        self._by_name_ids: dict[str, tuple[str, ...]] = {}
        for c in cards:
            if not c.id:
                continue
            self.by_id[c.id.upper()] = c
            self.by_name.setdefault(c.name, c)
            if c.dbf:
                self.by_dbf[c.dbf] = c
        for c in self.by_id.values():
            ids = list(self._by_name_ids.get(c.name, ()))
            ids.append(c.id)
            self._by_name_ids[c.name] = tuple(ids)

    def __len__(self) -> int:
        return len(self.by_id)

    def __contains__(self, card_id: str) -> bool:
        return card_id.upper() in self.by_id

    def get(self, card_id: str | None) -> Card | None:
        if not card_id:
            return None
        return self.by_id.get(card_id.upper())

    def by_name_or_id(self, key: str) -> Card | None:
        return self.get(key) or self.by_name.get(key)

    def siblings(self, card_id: str | None) -> tuple[str, ...]:
        """同一张牌的所有版本 id（CORE_/VAN_/普通版…）。

        卡组代码里存的是某个具体版本，日志里客户端可能用另一个版本，
        按 id 硬比会漏配，所以推断时要按「牌名」认亲。
        """
        c = self.get(card_id)
        if c is None:
            return (card_id,) if card_id else ()
        return self._by_name_ids.get(c.name, (c.id,))

    def is_token(self, card_id: str | None) -> bool:
        """是不是「衍生牌」（不在卡组里，由效果生成）。

        判据：不可收集，或 id 形如 token（PVPDR_/GAME_/结尾 t、t1 这类）。
        这类牌不该参与「对手是什么卡组」的统计。
        """
        c = self.get(card_id) if card_id else None
        if c is None:
            return False
        if not getattr(c, "collectible", True):
            return True
        cid = (c.id or "").upper()
        if "COIN" in cid or cid.startswith(("PVPDR_", "GAME_", "UNKNOWN")):
            return True
        if (c.name or "") in ("幸运币", "The Coin"):
            return True
        return bool(__import__("re").search(r"T\d*$", cid))

    def from_dbf(self, dbf: int) -> Card | None:
        """卡组代码里用的是 dbfId，不是 cardId。"""
        return self.by_dbf.get(int(dbf))

    def standard_pool(self, card_class: str) -> list[Card]:
        """该职业可用牌池（职业牌 + 中立，仅可收集）。"""
        return [
            c
            for c in self.by_id.values()
            if c.collectible and c.card_class in (card_class, "NEUTRAL") and c.type != "HERO"
        ]

    def display_name(self, card_id: str | None) -> str:
        c = self.get(card_id)
        return c.name if c else (card_id or "?")


def cache_path(locale: str = "zhCN") -> Path:
    return cards_cache(locale)


def download(locale: str = "zhCN", *, kind: str = "cards", timeout: int = 120) -> list[dict[str, Any]]:
    url = HSJSON.format(locale=locale, kind=kind)
    req = urllib.request.Request(url, headers={"User-Agent": "hsai/0.1 (+local)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 固定域名
        payload = resp.read()
    return json.loads(payload.decode("utf-8"))


# 不可收集的牌也要留一部分：场上有一半随从是衍生物（报告兵、骷髅、图腾…），
# 不认它们，界面就只能显示 RLK_008t 这种内部 id。但酒馆战棋/佣兵的几万条
# 数据对构筑模式毫无用处，这里按类型 + 集合挡掉。
_TOKEN_TYPES = {"MINION", "SPELL", "WEAPON", "LOCATION", "HERO_POWER", "ENCHANTMENT"}
_SKIP_SET_PREFIXES = ("BATTLEGROUNDS", "LETTUCE", "BATTLEGROUND")


def _keep(raw: dict[str, Any]) -> bool:
    if raw.get("id") and raw.get("collectible"):
        return True
    if raw.get("type") not in _TOKEN_TYPES:
        return False
    set_name = (raw.get("set") or "").upper()
    return not set_name.startswith(_SKIP_SET_PREFIXES)


def update(locale: str = "zhCN", *, verbose: bool = True, include_tokens: bool = True) -> CardDB:
    """联网抓取并写缓存。

    默认包含衍生物/英雄技能（第一次约 10 MB），这样场上的 token 也能显示中文名。
    """
    raw = download(locale, kind="cards" if include_tokens else "cards.collectible")
    kept = [r for r in raw] if not include_tokens else [r for r in raw if _keep(r)]
    cards = [_parse_card(r) for r in kept]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "locale": locale,
        "count": len(cards),
        "fields": list(_FIELDS),
        "rows": [_to_row(c) for c in cards],
    }
    tmp = cache_path(locale).with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, cache_path(locale))
    if verbose:
        print(f"已更新卡牌库：{len(cards)} 张 -> {cache_path(locale)}")
    return CardDB(cards)


def load(locale: str = "zhCN", *, auto_update: bool = True) -> CardDB:
    path = cache_path(locale)
    if not path.exists():
        if not auto_update:
            return CardDB([])
        try:
            return update(locale)
        except OSError as exc:  # 离线时不要让整个程序挂掉
            print(f"[warn] 卡牌库下载失败（{exc}），以空库启动")
            return CardDB([])
    payload = json.loads(path.read_text(encoding="utf-8"))
    return CardDB(_from_row(r) for r in payload["rows"])


if __name__ == "__main__":  # python -m hsai.cards
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # pragma: no cover
        pass
    db = update(sys.argv[1] if len(sys.argv) > 1 else "zhCN")
    c = db.by_name_or_id("CS2_029")
    print("样例:", c.name if c else "无", "|", c.class_cn if c else "")
