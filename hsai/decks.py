"""读取玩家自己的卡组：从客户端的 `Decks.log` 里解析。

炉石在 `[Decks]` 日志里会把「卡组内容」整份打出来，格式是标准的：

    ### 卡组名
    # Deck ID: 9518046526
    AAECAcn1Ahap9QaCmAf1mAe...

所以自己有哪些卡组、每套 30 张是什么，**不需要手动导入**。
（对手的卡组日志里没有 —— 服务端根本不下发，只能靠打出来的牌反推。）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from . import deckcode
from .cards import CLASS_CN, CardDB
from .logwatch import find_power_log

_NAME_RE = re.compile(r"^###\s*(?P<name>.+?)\s*$")
_ID_RE = re.compile(r"^#\s*Deck ID:\s*(?P<id>\d+)\s*$")
_CODE_RE = re.compile(r"^(?P<code>AA[0-9A-Za-z+/=_\-]{10,})$")


@dataclass
class SavedDeck:
    deck_id: str
    name: str
    code: str
    class_en: str
    cards: dict[str, int]  # cardId -> 张数

    @property
    def class_cn(self) -> str:
        return CLASS_CN.get(self.class_en, self.class_en or "未知")

    @property
    def size(self) -> int:
        return sum(self.cards.values())

    def as_dict(self) -> dict:
        return {"id": self.deck_id, "name": self.name, "class": self.class_en,
                "class_cn": self.class_cn, "size": self.size}


def find_decks_log() -> Path | None:
    """和 Power.log 同目录的 Decks.log（客户端把一局的所有日志放在同一个会话目录）。"""
    power = find_power_log()
    if power is None:
        return None
    for cand in (power.parent / "Decks.log", power.parent.parent / "Decks.log"):
        if cand.exists():
            return cand
    # 同一会话目录里找不到，就退到最新的会话
    root = power.parent.parent
    try:
        cands = sorted(root.glob("*/Decks.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    return cands[0] if cands else None


def parse_text(text: str, cards: CardDB) -> list[SavedDeck]:
    """解析 Decks.log 文本。同一套牌在文件里会重复出现多次，按 Deck ID 去重。"""
    out: dict[str, SavedDeck] = {}
    name = ""
    deck_id = ""
    for raw in text.splitlines():
        line = raw.strip()
        # 去掉日志前缀 "I 13:14:06.6737988 "
        line = re.sub(r"^[A-Z]\s+\d{2}:\d{2}:\d{2}\.\d+\s+", "", line)
        if not line:
            continue
        m = _NAME_RE.match(line)
        if m:
            name = m.group("name")
            continue
        m = _ID_RE.match(line)
        if m:
            deck_id = m.group("id")
            continue
        m = _CODE_RE.match(line)
        if m and deck_id:
            code = m.group("code")
            try:
                parsed = deckcode.decode(code)
            except deckcode.DeckCodeError:
                continue
            resolved = deckcode.resolve(parsed, cards, name=name or deck_id)
            if resolved.size < 20:      # 太短的（威兹班之类）不要
                continue
            out[deck_id] = SavedDeck(
                deck_id=deck_id,
                name=name or deck_id,
                code=code,
                class_en=resolved.card_class,
                cards=dict(resolved.counts),
            )
            deck_id = ""
    return list(out.values())


def load(cards: CardDB, path: Path | None = None) -> list[SavedDeck]:
    p = path or find_decks_log()
    if p is None:
        return []
    try:
        return parse_text(p.read_text(encoding="utf-8", errors="replace"), cards)
    except OSError:
        return []


def best_match(decks: Iterable[SavedDeck], seen: Iterable[str], our_class: str = "") -> tuple[SavedDeck | None, float]:
    """在「我保存的卡组」里找出这局用的是哪套。

    依据：职业必须一致，然后看**已经看到的牌**（起手 + 已打出的 + 牌库里露出的）
    落在这套牌里的比例。起手 4 张通常就足以区分同职业的几套牌。
    """
    seen_set = {c for c in seen if c}
    candidates = [d for d in decks if not our_class or d.class_en == our_class]
    if not candidates:
        return None, 0.0
    if len(candidates) == 1:
        return candidates[0], 1.0
    # 已看到的牌里会混进「生成牌」（幸运币、衍生物、发现出来的牌），
    # 它们本来就不在任何卡组里，直接拉低命中率。先把这些剔掉。
    pool: set[str] = set()
    for d in candidates:
        pool |= set(d.cards)
    seen_set = {c for c in seen_set if c in pool}
    if not seen_set:
        return None, 0.0

    best: tuple[SavedDeck | None, float] = (None, 0.0)
    for deck in candidates:
        if not seen_set:
            score = 0.0
        else:
            hit = sum(1 for cid in seen_set if cid in deck.cards)
            score = hit / len(seen_set)
        if score > best[1]:
            best = (deck, score)
    return best


def load_queued(cards) -> list[SavedDeck]:
    """从 Decks.log 里读「点开始对战」时记录的那套牌。

    客户端会写：Finding Game With Deck: / ### 名字 / # Deck ID / 牌组代码
    这是**唯一直接告诉你「这局用哪套」的地方**，不用靠看牌去猜。
    """
    path = find_decks_log()
    if path is None:
        return []
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    cached = _QUEUED_CACHE.get("key")
    if cached and cached[0] == str(path) and cached[1] == mtime:
        return cached[2]

    out: list[SavedDeck] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for i, line in enumerate(lines):
        if "Finding Game With Deck" not in line:
            continue
        name = deck_id = code = None
        for j in range(i + 1, min(i + 6, len(lines))):
            text = lines[j]
            if "### " in text:
                name = text.split("### ", 1)[1].strip()
            elif "# Deck ID" in text:
                deck_id = text.split(":", 1)[-1].strip()
            elif "Deck ID" not in text and text.strip():
                tail = text.split("-", 1)[-1].strip() if " - " in text else text.strip()
                token = tail.split()[-1] if tail.split() else ""
                if token.startswith("AA"):
                    code = token
        if name and code:
            try:
                resolved = deckcode.resolve(deckcode.decode(code), cards, name=name)
            except Exception:
                continue
            if resolved is None or resolved.size < 20:
                continue
            out.append(SavedDeck(name=name, deck_id=deck_id or "", code=code,
                                 cards=dict(resolved.counts),
                                 class_en=resolved.card_class or ""))
    _QUEUED_CACHE["key"] = (str(path), mtime, out)
    return out


_QUEUED_CACHE: dict[str, object] = {}
