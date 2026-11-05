"""炉石卡组代码（Deckstring）编解码。

格式（官方，客户端「复制卡组」产出的那串 `AAEC...`）：

    byte  0     : 保留位，固定 0
    varint 1    : 版本号（目前是 1）
    varint 2    : 格式（1=狂野 2=标准 3=经典 4=经典 5=乱斗）
    varint 3    : 英雄数量（通常 1）
    varint × n  : 英雄的 dbfId
    varint      : 单张牌数量 n1
    varint × n1 : 单张牌的 dbfId
    varint      : 双张牌数量 n2
    varint × n2 : 双张牌的 dbfId
    varint      : 其它数量 nn
    (varint,varint) × nn : (dbfId, 张数)

变长整数是 LEB128（小端，7 位一组，最高位表示还有后续），
外层 base64（URL-safe 变体，可能带 `=` 补齐）。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

from .cards import Card, CardDB

FORMAT_NAMES = {0: "未知", 1: "狂野", 2: "标准", 3: "经典", 4: "经典", 5: "乱斗", 6: "酒馆战棋"}


class DeckCodeError(ValueError):
    pass


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise DeckCodeError("卡组代码被截断了")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise DeckCodeError("变长整数过长，代码可能不是卡组代码")


def _write_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


@dataclass
class DeckCode:
    version: int = 1
    fmt: int = 2
    heroes: list[int] = field(default_factory=list)
    cards: dict[int, int] = field(default_factory=dict)  # dbfId -> 张数

    @property
    def format_cn(self) -> str:
        return FORMAT_NAMES.get(self.fmt, str(self.fmt))

    @property
    def size(self) -> int:
        return sum(self.cards.values())


def decode(code: str) -> DeckCode:
    """解码卡组代码。失败抛 DeckCodeError，附带可读原因。"""
    text = (code or "").strip()
    if not text:
        raise DeckCodeError("代码为空")
    # 客户端复制出来的可能带前后空白或换行
    text = "".join(text.split())
    try:
        raw = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=False)
    except Exception as exc:  # noqa: BLE001
        raise DeckCodeError(f"不是合法的 base64：{exc}") from exc
    if len(raw) < 5:
        raise DeckCodeError("内容太短，不像是卡组代码")
    if raw[0] != 0:
        raise DeckCodeError("保留位不是 0，可能不是卡组代码")

    pos = 1
    version, pos = _read_varint(raw, pos)
    fmt, pos = _read_varint(raw, pos)
    if version > 5:
        raise DeckCodeError(f"版本号 {version} 太新，无法解析")

    hero_count, pos = _read_varint(raw, pos)
    heroes = []
    for _ in range(hero_count):
        v, pos = _read_varint(raw, pos)
        heroes.append(v)

    cards: dict[int, int] = {}
    n_single, pos = _read_varint(raw, pos)
    for _ in range(n_single):
        v, pos = _read_varint(raw, pos)
        cards[v] = cards.get(v, 0) + 1
    n_double, pos = _read_varint(raw, pos)
    for _ in range(n_double):
        v, pos = _read_varint(raw, pos)
        cards[v] = cards.get(v, 0) + 2
    n_other, pos = _read_varint(raw, pos)
    for _ in range(n_other):
        v, pos = _read_varint(raw, pos)
        count, pos = _read_varint(raw, pos)
        cards[v] = cards.get(v, 0) + count

    return DeckCode(version=version, fmt=fmt, heroes=heroes, cards=cards)


def encode(deck: DeckCode) -> str:
    """把 DeckCode 编回字符串（主要给测试和「导出」用）。"""
    buf = bytearray([0])
    buf += _write_varint(deck.version)
    buf += _write_varint(deck.fmt)
    buf += _write_varint(len(deck.heroes))
    for h in deck.heroes:
        buf += _write_varint(h)

    single = sorted(d for d, n in deck.cards.items() if n == 1)
    double = sorted(d for d, n in deck.cards.items() if n == 2)
    other = sorted((d, n) for d, n in deck.cards.items() if n not in (1, 2))

    buf += _write_varint(len(single))
    for d in single:
        buf += _write_varint(d)
    buf += _write_varint(len(double))
    for d in double:
        buf += _write_varint(d)
    buf += _write_varint(len(other))
    for d, n in other:
        buf += _write_varint(d)
        buf += _write_varint(n)

    return base64.b64encode(bytes(buf)).decode("ascii")


@dataclass
class ResolvedDeck:
    """解出来的卡组：既能给人看，也能直接喂给预判模块。"""

    name: str
    card_class: str
    format_cn: str
    hero: Card | None
    cards: list[Card]
    counts: dict[str, int]  # cardId -> 张数
    unknown: list[int]      # 卡牌库里没有的 dbfId

    @property
    def size(self) -> int:
        return sum(self.counts.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "class": self.card_class,
            "format": self.format_cn,
            "size": self.size,
            "hero": self.hero.name if self.hero else "",
            "unknown": self.unknown,
        }


def resolve(code: DeckCode, cards: CardDB, *, name: str = "") -> ResolvedDeck:
    """把 dbfId 翻译成实际卡牌。职业从英雄牌推。"""
    hero = cards.from_dbf(code.heroes[0]) if code.heroes else None
    card_class = (hero.card_class if hero else "NEUTRAL") or "NEUTRAL"

    resolved: list[Card] = []
    counts: dict[str, int] = {}
    unknown: list[int] = []
    for dbf, n in code.cards.items():
        card = cards.from_dbf(dbf)
        if card is None:
            unknown.append(dbf)
            continue
        resolved.append(card)
        counts[card.id] = counts.get(card.id, 0) + n

    resolved.sort(key=lambda c: (c.cost, c.name))
    if not name:
        # 没给名字时按「关键牌」起个临时名，方便在列表里认。
        # 以前用的是「最贵的两张牌」—— 那可能是一张 8 费的边缘牌，认不出是哪套。
        from .meta import pick_key_cards

        keys = pick_key_cards(counts, cards)
        label = cards.display_name(keys[0]) if keys else "新卡组"
        name = f"{card_class_title(card_class)}·{label}"
    return ResolvedDeck(
        name=name,
        card_class=card_class,
        format_cn=code.format_cn,
        hero=hero,
        cards=resolved,
        counts=counts,
        unknown=unknown,
    )


def card_class_title(card_class: str) -> str:
    from .cards import CLASS_CN

    return CLASS_CN.get(card_class, card_class)
