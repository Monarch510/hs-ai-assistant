"""卡牌功能标签：范围筛选、规则打标、AI/人工合并、落盘与查询。

为什么单独一层
--------------
`tag_rules` 只回答「一张牌的文本命中哪几类」，不知道哪些牌该打标、数据长什么样、
补标从哪来。这一层负责范围口径、来源合并、稳定排序和读写；查询侧只读缓存，
**不在查询时重建** —— 否则界面每问一次牌都要扫一遍卡牌库。

口径见 `docs/card-tagging/CONTEXT.md`。规则拿不准的进待定，不在这里猜。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .cards import TYPE_CN, Card, CardDB
from .paths import (
    CARD_TAGS_AI_JSON,
    CARD_TAGS_JSON,
    CARD_TAGS_MANUAL_JSON,
    CARD_TAGS_PENDING_JSONL,
)
from .tag_rules import RULES, TAG_ORDER
from .tag_rules import pending_reason as _rule_pending_reason

# 能打出场的牌才值得打功能标签；英雄技能/附魔/法术牌衍生物之类都不是「打出的牌」
SCOPE_TYPES = frozenset({"MINION", "SPELL", "WEAPON", "LOCATION", "HERO"})
# 英雄皮肤和制作人员名单混在卡牌库里，但根本不是对局中的牌
SKIP_SETS = frozenset({"HERO_SKINS", "CREDITS"})


def in_scope(card: Card, db: CardDB) -> bool:
    """这张牌要不要打标签：可打出场、非衍生物、非皮肤/名单。"""
    if card.type not in SCOPE_TYPES:
        return False
    if (card.set or "").upper() in SKIP_SETS:
        return False
    return not db.is_token(card.id)


def classify(card: Card) -> tuple[str, ...]:
    """规则打标，按 TAG_ORDER 排序 —— 顺序固定，输出才不会随 dict 遍历变。"""
    return tuple(tag for tag in TAG_ORDER if RULES[tag](card))


def pending_reason(card: Card) -> str | None:
    """规则拿不准的原因；透传规则层，给待定清单用。"""
    return _rule_pending_reason(card)


# ---------------------------------------------------------------- 构建

def _norm_rows(extra: dict | None) -> dict[str, dict]:
    """补标文件的 key 统一成大写 id，方便和卡牌库对齐。"""
    out: dict[str, dict] = {}
    for key, row in (extra or {}).items():
        if isinstance(row, dict):
            out[str(key).upper()] = row
    return out


def _valid_tags(row: dict) -> tuple[str, ...]:
    """只收固定 11 类里的标签：补标文件写错标签名时丢掉，别污染输出。"""
    return tuple(t for t in (row.get("tags") or ()) if t in TAG_ORDER)


def build(db: CardDB, ai: dict | None = None, manual: dict | None = None) -> dict:
    """全量打标。

    标签取「规则 ∪ AI ∪ 人工」的并集；`src` 记录真正贡献了标签的来源
    （没贡献的来源不计，规则一张牌都没命中时 src 仍是 `rule`）。
    """
    ai_rows = _norm_rows(ai)
    manual_rows = _norm_rows(manual)
    rows: dict[str, dict] = {}
    pending: list[dict] = []
    by_type: dict[str, int] = {}

    # 按 id 排序遍历：JSON 里的键序只由数据决定，不受 set/哈希顺序影响
    for cid in sorted(db.by_id):
        card = db.by_id[cid]
        if not in_scope(card, db):
            continue
        rule_tags = classify(card)
        ai_entry = ai_rows.get(cid) or {}
        manual_entry = manual_rows.get(cid) or {}
        ai_tags = _valid_tags(ai_entry)
        manual_tags = _valid_tags(manual_entry)
        tags = [t for t in TAG_ORDER if t in rule_tags or t in ai_tags or t in manual_tags]

        sources: list[str] = []
        if rule_tags:
            sources.append("rule")
        if ai_tags:
            sources.append("ai")
        if manual_tags:
            sources.append("manual")
        row = {"tags": tags, "src": "+".join(sources) or "rule"}
        # 补标理由：人工优先于 AI（人工是最后一道覆盖）
        note = (manual_entry.get("note") or ai_entry.get("note") or "").strip()
        if note:
            row["note"] = note

        rows[cid] = row
        by_type[card.type] = by_type.get(card.type, 0) + 1
        why = pending_reason(card)
        if why:
            pending.append({
                "id": cid,
                "name": card.name,
                "cost": card.cost,
                "type": card.type,
                "text": card.text,
                "why": why,
            })

    pending.sort(key=lambda p: p["id"])
    return {
        "version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "scope": {"total": len(rows), "by_type": dict(sorted(by_type.items()))},
        "rows": rows,
        "pending": pending,
    }


# ---------------------------------------------------------------- 读写

def _read_json(path: Path) -> dict:
    """缺文件、坏文件都当「没有」：缓存坏了不该让查询/命令直接崩。

    用 utf-8-sig 读：人工/AI 补标文件常在 Windows 编辑器里带 BOM，
    普通 utf-8 会静默解析失败，表现成「补标全没生效」。
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def load_payload(path: Path | None = None) -> dict:
    """读整份标签数据（report/pending 要看 scope 与 pending，不只是 rows）。"""
    return _read_json(Path(path or CARD_TAGS_JSON))


def load(path: Path | None = None) -> dict[str, tuple[str, ...]]:
    """id -> 标签元组；缺文件返回空表。"""
    payload = load_payload(path)
    rows = payload.get("rows") or {}
    out: dict[str, tuple[str, ...]] = {}
    for cid, row in rows.items():
        if isinstance(row, dict):
            out[str(cid).upper()] = tuple(row.get("tags") or ())
    return out


def save(payload: dict, path: Path | None = None) -> Path:
    """写全量标签数据，返回实际路径。"""
    target = Path(path or CARD_TAGS_JSON)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return target


def save_pending(payload: dict, path: Path | None = None) -> Path:
    """写待定清单（JSONL，一行一张牌），返回实际路径。"""
    target = Path(path or CARD_TAGS_PENDING_JSONL)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(item, ensure_ascii=False) for item in payload.get("pending") or []]
    target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return target


def load_source(path: Path) -> dict[str, dict]:
    """读 AI / 人工补标文件的 `rows`；缺文件返回空表。"""
    rows = _read_json(path).get("rows")
    return rows if isinstance(rows, dict) else {}


def card_tags(
    key: str | Card,
    cards: CardDB | None = None,
    path: Path | None = None,
) -> tuple[str, ...]:
    """查标签；只传 id 也能用，未知返回 ()。

    查询只读缓存，不重建 —— 没跑过 `tags build` 就一律返回 ()，由调用方决定要不要 build。
    """
    if isinstance(key, Card):
        cid = key.id
    elif cards is not None:
        found = cards.by_name_or_id(key)
        cid = found.id if found is not None else key
    else:
        cid = key
    return load(path).get((cid or "").upper(), ())


# ---------------------------------------------------------------- 报告

def summary(payload: dict) -> dict:
    """把标签数据数成报告用的数字；纯函数，测试不依赖 data/ 文件。"""
    rows = payload.get("rows") or {}
    tags = {tag: 0 for tag in TAG_ORDER}
    src: dict[str, int] = {}
    untagged = 0
    for row in rows.values():
        row_tags = row.get("tags") or ()
        if not row_tags:
            untagged += 1
        for tag in row_tags:
            if tag in tags:
                tags[tag] += 1
        source = row.get("src") or "?"
        src[source] = src.get(source, 0) + 1
    scope = payload.get("scope") or {}
    return {
        "total": scope.get("total", len(rows)),
        "by_type": scope.get("by_type") or {},
        "tags": tags,
        "pending": len(payload.get("pending") or []),
        "src": dict(sorted(src.items())),
        "untagged": untagged,
    }


def format_report(payload: dict) -> str:
    """渲染覆盖率报告：范围、类型分布、每类张数、待定、来源、无标签。"""
    s = summary(payload)
    names = "、".join(f"{TYPE_CN.get(t, t)} {n}" for t, n in s["by_type"].items())
    lines = [f"范围：{s['total']} 张" + (f"（{names}）" if names else "")]
    lines.append("每类张数：")
    lines.extend(f"  {tag} {s['tags'][tag]}" for tag in TAG_ORDER)
    lines.append(f"未打标签：{s['untagged']} 张")
    lines.append(f"待定：{s['pending']} 张")
    src = "、".join(f"{k} {v}" for k, v in s["src"].items()) or "无"
    lines.append(f"src 分布：{src}")
    return "\n".join(lines)
