"""Power.log 词法/语法解析。

以**真实日志**为准（2026-10 实测，标准客户端 + Verbose=True）：

    D 12:16:40.7652036 GameState.DebugPrintPower() - TAG_CHANGE Entity=Player#1234 tag=RESOURCES value=1
    D 12:17:37.9187018 PowerTaskList.DebugPrintPower() - TAG_CHANGE Entity=[entityName=UNKNOWN ENTITY [cardType=INVALID] id=15 zone=DECK zonePos=0 cardId= player=1] tag=ZONE value=DECK
    D 12:16:01.6245543 GameState.DebugPrintPower() - BLOCK_START BlockType=TRIGGER Entity=1 ...
    D 12:16:01.6245543 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=74 CardID=HERO_07bk

三个必须处理的现实：
  1. `Verbose=True` 时 tag 打印的是**名字**（ZONE / CONTROLLER / PLAYSTATE），不是数字；
     夹杂少量未知数字 tag。所以内部一律用「tag 名字」当键，数字只做兼容映射。
  2. 实体引用有四种形态：`[entityName=.. id=..]`（且 entityName 里还会嵌套
     `[cardType=INVALID]`）、`Entity=GameEntity`、`Entity=42`、`Entity=玩家名#1234`。
  3. 同一个变更会同时出现在 GameState 和 PowerTaskList 两个来源里，重复但幂等。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# ---------------------------------------------------------------- 常量表

# 数字 tag -> 名字。只收录本工具用到的；日志里出现未知数字时直接丢弃。
TAG_NAMES: dict[int, str] = {
    2: "ENTITY_ID",
    17: "PLAYSTATE",
    20: "TURN",
    23: "CURRENT_PLAYER",
    26: "RESOURCES",
    27: "RESOURCES_USED",
    30: "PLAYER_ID",
    44: "DAMAGE",
    45: "HEALTH",
    47: "ATK",
    48: "COST",
    49: "ZONE",
    50: "CONTROLLER",
    73: "DIVINE_SHIELD",
    90: "NUM_ATTACKS_THIS_TURN",
    101: "TAUNT",
    190: "ARMOR",
    201: "OVERRIDE_ENTITY_ID",
    218: "CARD_ID",
    263: "HERO_ENTITY",
}

# 我们真正会读的 tag（其余一律忽略，省内存也省得误判）
KNOWN_TAGS: frozenset[str] = frozenset(
    {
        "ENTITY_ID", "PLAYSTATE", "TURN", "CURRENT_PLAYER",
        "RESOURCES", "RESOURCES_USED", "PLAYER_ID",
        "DAMAGE", "HEALTH", "ATK", "COST", "ZONE", "CONTROLLER",
        "TAUNT", "DIVINE_SHIELD", "ARMOR", "NUM_ATTACKS_THIS_TURN",
        "HERO_ENTITY", "CARDTYPE", "CARD_ID", "ZONE_POSITION", "MULLIGAN_STATE", "EXHAUSTED", "FROZEN", "FIRST_PLAYER", "QUEST_PROGRESS", "QUEST_PROGRESS_TOTAL",
    }
)

ZONE_NAMES: dict[int, str] = {
    0: "INVALID", 1: "PLAY", 2: "DECK", 3: "HAND", 4: "GRAVEYARD",
    5: "SECRET", 6: "REMOVEDFROMGAME", 7: "SETASIDE",
}
ZONE_IDS: dict[str, int] = {v: k for k, v in ZONE_NAMES.items()}

PLAYSTATE_NAMES: dict[int, str] = {
    0: "INVALID", 1: "DEALING", 2: "PLAYING", 3: "LOST", 4: "WON",
    5: "TIED", 6: "DISCONNECTED", 7: "CONCEDED",
}
PLAYSTATE_IDS: dict[str, int] = {v: k for k, v in PLAYSTATE_NAMES.items()}

POWER_SOURCES = frozenset(
    {
        "GameState.DebugPrintPower",
        "PowerTaskList.DebugPrintPower",
        "GameState.DebugPrintEntityChoices",
        "GameState.DebugPrintOptions",
        # 这两个来源里有手牌/选择目标的完整实体引用（含 zone/cardId），
        # 是「我方手牌」在某些对局里唯一的线索
        "GameState.SendChoices",
        "GameState.DebugPrintEntitiesChosen",
    }
)

# 一行里可能出现的所有实体引用
REF_ALL = re.compile(
    r"\[\s*(?:entityName=(?P<name>.*?)\s+)?id=(?P<id>\d+)"
    r"(?:\s+zone=(?P<zone>[A-Z_]+))?(?:\s+zonePos=(?P<zpos>-?\d+))?"
    r"(?:\s+cardId=(?P<card>\w*))?(?:\s+player=(?P<player>\d+))?\s*\]"
)

LINE_RE = re.compile(
    r"^[A-Za-z]?\s*(?P<time>\d{2}:\d{2}:\d{2}\.\d+)?\s*"
    r"(?P<source>[\w.]+)\(\)\s*-\s?(?P<body>.*)$"
)

# 实体引用：[entityName=UNKNOWN ENTITY [cardType=INVALID] id=15 zone=DECK zonePos=0 cardId= player=1]
# entityName 里可能嵌方括号，所以 name 用非贪婪 + 必须紧跟 ` id=数字`
REF_RE = re.compile(
    r"\[\s*(?:entityName=(?P<name>.*?)\s+)?id=(?P<id>\d+)"
    r"(?:\s+zone=(?P<zone>[A-Z_]+))?"
    r"(?:\s+zonePos=(?P<zpos>-?\d+))?"
    r"(?:\s+cardId=(?P<card>\w*))?"
    r"(?:\s+player=(?P<player>\d+))?\s*\]",
    re.DOTALL,
)

ENTITY_ID_RE = re.compile(r"\bID=(?P<id>\d+)\b")
CARD_ID_RE = re.compile(r"\bCardID=(?P<card>\w*)\b")
ENTITY_ID_EQ_RE = re.compile(r"\bEntityID=(?P<id>\d+)\b")
PLAYER_ID_RE = re.compile(r"\bPlayerID=(?P<pid>\d+)\b")
TAG_VALUE_RE = re.compile(r"\btag=(?P<tag>\w+)\s+value=(?P<val>\S+)")
BARE_TAG_RE = re.compile(r"^tag=(?P<tag>\w+)\s+value=(?P<val>\S+)")


@dataclass(slots=True)
class Event:
    """一条结构化日志事件。kind 决定 data 里的字段。"""

    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    time: str = ""

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<{self.kind} {self.data}>"


def tag_key(raw: str) -> str | None:
    """日志里的 tag 统一成「名字」。未知数字返回 None（直接忽略）。"""
    raw = raw.strip()
    if not raw:
        return None
    if raw.isdigit():
        return TAG_NAMES.get(int(raw))
    key = raw.upper()
    return key if key in KNOWN_TAGS else key


def parse_value(raw: str) -> Any:
    """tag 的 value：能转数字就转；ZONE/PLAYSTATE 的名字转成数字；其它原样。"""
    raw = raw.strip()
    if not raw:
        return ""
    try:
        return int(raw)
    except ValueError:
        pass
    up = raw.upper()
    if up in ZONE_IDS:
        return ZONE_IDS[up]
    if up in PLAYSTATE_IDS:
        return PLAYSTATE_IDS[up]
    return raw


def parse_entity_ref(text: str) -> dict[str, Any] | None:
    """解析实体引用，返回 {id?, name?, zone?, zone_pos?, card_id?, controller?}。"""
    m = REF_RE.search(text)
    if m is None and "[" in text:
        blocks = _bracket_blocks(text)
        if blocks:
            ref = _ref_from_block(blocks[0])
            if ref is not None:
                return ref
    if m:
        out: dict[str, Any] = {}
        if m.group("id") is not None:
            out["id"] = int(m.group("id"))
        name = (m.group("name") or "").strip()
        if name and not name.startswith("UNKNOWN ENTITY"):
            out["name"] = name
        if m.group("zone"):
            out["zone"] = ZONE_IDS.get(m.group("zone").upper(), m.group("zone"))
        if m.group("zpos") is not None:
            out["zone_pos"] = int(m.group("zpos"))
        if m.group("card") is not None:
            out["card_id"] = m.group("card") or None
        if m.group("player") is not None:
            out["controller"] = int(m.group("player"))
        return out or None

    if re.search(r"\bEntity=GameEntity\b", text):
        return {"id": 1}
    m2 = re.search(r"\bEntity=(\d+)\b", text)
    if m2:
        return {"id": int(m2.group(1))}
    m3 = re.search(r"\bEntity=([^\s\]]+)", text)
    if m3:
        return {"name": m3.group(1)}
    return None


def _tag_event(ref: dict[str, Any] | None, tag_raw: str, val_raw: str, time: str) -> Event | None:
    key = tag_key(tag_raw)
    if key is None:
        return None
    ref = ref or {}
    return Event(
        "tag_change",
        {
            "entity": ref.get("id"),
            "ref": ref,
            "tag": key,
            "value": parse_value(val_raw),
        },
        time,
    )


def parse_line(line: str, pending_entity: int | None = None) -> Event | None:
    """解析单行。不是 Power 日志行则返回 None。"""
    line = line.rstrip("\r\n")
    if not line:
        return None
    m = LINE_RE.match(line)
    if not m:
        return None
    source, body, time = m.group("source"), m.group("body") or "", m.group("time") or ""
    if source not in POWER_SOURCES:
        return None
    body = body.strip()
    if not body:
        return None

    if source == "GameState.DebugPrintEntitiesChosen":
        ev = _parse_ref_line(body, time)
        if ev is not None:
            ev.kind = "chosen"
        return ev
    if source == "GameState.SendChoices":
        return _parse_ref_line(body, time)
    if source == "GameState.DebugPrintEntityChoices":
        ev = _parse_choices(body, time)
        refs = _all_refs(body)
        if refs:
            ev.data["refs"] = refs
        return ev
    if source == "GameState.DebugPrintOptions":
        return Event("options", {"raw": body}, time)

    if body.startswith("CREATE_GAME"):
        return Event("create_game", {}, time)
    if body.startswith("FULL_ENTITY"):
        return _parse_full_entity(body, time)
    if body.startswith("SHOW_ENTITY") or body.startswith("CHANGE_ENTITY"):
        ref = parse_entity_ref(body) or {}
        cm = CARD_ID_RE.search(body)
        kind = "show_entity" if body.startswith("SHOW_ENTITY") else "change_entity"
        return Event(kind, {"entity": ref.get("id"), "ref": ref,
                            "card_id": (cm.group("card") if cm else None) or None}, time)
    if body.startswith("HIDE_ENTITY"):
        ref = parse_entity_ref(body) or {}
        return Event("hide_entity", {"entity": ref.get("id"), "ref": ref}, time)
    if body.startswith("TAG_CHANGE"):
        tm = TAG_VALUE_RE.search(body)
        if not tm:
            return None
        return _tag_event(parse_entity_ref(body), tm.group("tag"), tm.group("val"), time)
    if body.startswith("BLOCK_START"):
        return _parse_block_start(body, time)
    if body.startswith("BLOCK_END"):
        return Event("block_end", {}, time)
    if body.startswith("META_DATA"):
        return Event("meta_data", {"raw": body}, time)

    bt = BARE_TAG_RE.match(body)
    if bt:
        if pending_entity is None:
            return None
        return _tag_event({"id": pending_entity}, bt.group("tag"), bt.group("val"), time)

    em = ENTITY_ID_EQ_RE.search(body)
    if em:
        d: dict[str, Any] = {"entity": int(em.group("id")), "raw": body}
        pm = PLAYER_ID_RE.search(body)
        if pm:
            d["player_id"] = int(pm.group("pid"))
        am = re.search(r"GameAccountId=\[hi=(\d+) lo=(\d+)\]", body)
        if am:
            d["account"] = (int(am.group(1)), int(am.group(2)))
        d["role"] = ("game" if body.startswith("GameEntity")
                     else "player" if body.startswith("Player") else "other")
        return Event("entity", d, time)

    return Event("unknown", {"raw": body}, time)


def _bracket_blocks(text: str) -> list[str]:
    """按括号配平切出所有顶层 [ ... ] 的内容。

    实体引用里 entityName 可能自带方括号
    （entityName=UNKNOWN ENTITY [cardType=INVALID]），正则的非贪婪匹配会
    在内层 ] 处提前截断，导致整行解析失败 —— 手牌读不到、发现选项只出一个，
    根因都是这个。
    """
    blocks: list[str] = []
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "[":
            if depth == 0:
                start = i + 1
            depth += 1
        elif ch == "]":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    blocks.append(text[start:i])
                    start = -1
    return blocks


def _ref_from_block(inner: str) -> dict[str, Any] | None:
    m_id = re.search(r"\bid=(-?\d+)", inner)
    if not m_id:
        return None
    out: dict[str, Any] = {"id": int(m_id.group(1))}
    if m := re.search(r"\bzone=([A-Z_]+)", inner):
        out["zone"] = ZONE_IDS.get(m.group(1).upper(), m.group(1))
    if m := re.search(r"\bzonePos=(-?\d+)", inner):
        out["zone_pos"] = int(m.group(1))
    if m := re.search(r"\bcardId=(\w*)", inner):
        if m.group(1):
            out["card_id"] = m.group(1)
    if m := re.search(r"\bplayer=(\d+)", inner):
        out["controller"] = int(m.group(1))
    if m := re.search(r"entityName=(.*?)(?=\s+id=)", inner, re.S):
        nm = m.group(1).strip()
        if nm and not nm.startswith("UNKNOWN ENTITY"):
            out["name"] = nm
    return out


def _all_refs(body: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for inner in _bracket_blocks(body):
        ref = _ref_from_block(inner)
        if ref is not None:
            out.append(ref)
    return out


def _parse_ref_line(body: str, time: str) -> Event | None:
    refs = _all_refs(body)
    if not refs:
        return None
    return Event("entity_update", {"refs": refs}, time)


def _parse_full_entity(body: str, time: str) -> Event:
    im = ENTITY_ID_RE.search(body)
    cm = CARD_ID_RE.search(body)
    ref = parse_entity_ref(body) or {}
    return Event(
        "full_entity",
        {
            "entity": int(im.group("id")) if im else ref.get("id"),
            "card_id": (cm.group("card") if cm else None) or None,
            "ref": ref,
        },
        time,
    )


def _parse_block_start(body: str, time: str) -> Event:
    d: dict[str, Any] = {"type": None, "entity": None, "ref": None, "target": None, "effect_card_id": None}
    if m := re.search(r"BlockType=(\w+)", body):
        d["type"] = m.group(1)
    ref = parse_entity_ref(body)
    if ref:
        d["ref"] = ref
        d["entity"] = ref.get("id")
    if (tm := re.search(r"Target=(\d+)", body)) and tm.group(1) != "0":
        d["target"] = int(tm.group(1))
    if (em := re.search(r"EffectCardId=(\w*)", body)) and em.group(1):
        d["effect_card_id"] = em.group(1)
    return Event("block_start", d, time)


def _parse_choices(body: str, time: str) -> Event:
    d: dict[str, Any] = {"raw": body, "entity": None, "player": None, "choice_type": None, "count": None}
    if m := re.search(r"id=(\d+)", body):
        d["entity"] = int(m.group(1))
    if m := re.search(r"\bplayer=(\d+)", body):
        d["player"] = int(m.group(1))
    if m := re.search(r"\bPlayer=([^\s\[]+)", body):
        d["player_name"] = m.group(1)
    if m := re.search(r"ChoiceType=(\w+)", body):
        d["choice_type"] = m.group(1)
    if m := re.search(r"Count=(\d+)", body):
        d["count"] = int(m.group(1))
    if m := re.search(r"CountMin=(\d+)\s+CountMax=(\d+)", body):
        d["min"], d["max"] = int(m.group(1)), int(m.group(2))
    d["refs"] = _all_refs(body)
    return Event("choices", d, time)


def parse_text(text: str) -> list[Event]:
    """解析一整段日志文本，自动为裸 tag 行补上下文实体。"""
    events: list[Event] = []
    pending: int | None = None
    for line in text.splitlines():
        ev = parse_line(line, pending)
        if ev is None:
            continue
        if ev.kind in ("full_entity", "entity", "block_start") and ev.data.get("entity"):
            pending = ev.data["entity"]
        events.append(ev)
    return events


def iter_events(lines: Iterable[str]) -> Iterable[Event]:
    """流式版本：适合边读边解析。"""
    pending: int | None = None
    for line in lines:
        ev = parse_line(line, pending)
        if ev is None:
            continue
        if ev.kind in ("full_entity", "entity", "block_start") and ev.data.get("entity"):
            pending = ev.data["entity"]
        yield ev
