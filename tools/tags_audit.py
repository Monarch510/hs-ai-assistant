"""独立审计 11 类卡牌标签：覆盖率、抽样、可疑清单。

为什么审计不 import hsai.tag_rules
----------------------------------
范围口径只有一份，直接用 hsai.tags.in_scope，不自己另算一套；但标签质量必须用
**另一把尺子**量 —— 拿规则自己的判定证明规则对，等于什么都没证明。所以这里按
CONTEXT.md 的 11 类定义独立复核。

输出全部是给人看的：抽样表人工核对，可疑清单交给 Lead 决定要不要返工。

用法：
    python tools/tags_audit.py
    python tools/tags_audit.py --seed 7 --per-class 20 --top 15
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 直接 python tools/tags_audit.py 时 sys.path[0] 是 tools/
    sys.path.insert(0, str(ROOT))

from hsai import paths  # noqa: E402
from hsai.cards import TYPE_CN, Card, CardDB, load  # noqa: E402

# CONTEXT.md 第 3 节定死的口径。改这里等于改验收标准，先问 Lead。
TAG_ORDER = ("单解", "群解", "站场", "增益", "减益", "治疗", "护甲", "资源", "抽牌", "斩杀")
TAG_SET = frozenset(TAG_ORDER)
# 文本里出现这些词却 0 标签，多半是规则漏判；只是「可疑」，不是自动判错。
SIGNAL_WORDS = ("消灭", "伤害", "恢复", "护甲", "召唤", "发现", "获得", "随机", "冻结", "沉默", "抽")
SAMPLE_LIMIT = 10
DEFAULT_SEED = 20261106
DEFAULT_PER_CLASS = 15
DEFAULT_TOP = 10


def tags_path() -> Path:
    """data/card_tags.json：优先用 paths.py 的唯一出处，B 还没加时退回本地推算。"""
    return getattr(paths, "CARD_TAGS_JSON", paths.DATA_DIR / "card_tags.json")


# ---------- 输入归一 ----------

def normalize_rows(payload: dict) -> dict[str, tuple[str, ...]]:
    """rows 可能是 {id: {"tags": [...]}}，手写文件也可能是 {id: [...]}。"""
    rows: dict[str, tuple[str, ...]] = {}
    for cid, value in (payload.get("rows") or {}).items():
        tags = value.get("tags") or () if isinstance(value, dict) else value or ()
        rows[str(cid).upper()] = tuple(str(t) for t in tags)
    return rows


def sort_tags(tags) -> tuple[str, ...]:
    """按 TAG_ORDER 排，非法标签放最后 —— 别用 set 顺序，输出会飘。"""
    return tuple(sorted(
        tags,
        key=lambda t: (TAG_ORDER.index(t) if t in TAG_SET else len(TAG_ORDER), t),
    ))


def clean_tags(tags) -> tuple[str, ...]:
    return tuple(dict.fromkeys(sort_tags(tags)))


def scope_cards(db: CardDB, in_scope) -> list[Card]:
    return [c for c in db.by_id.values() if in_scope(c, db)]


# ---------- 统计 ----------

def type_distribution(cards) -> list[tuple[str, int]]:
    counts = Counter(c.type for c in cards)
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def tag_counts(rows: dict[str, tuple[str, ...]], scope_ids: set[str]) -> list[tuple[str, int]]:
    counts = Counter()
    for cid in scope_ids:
        for tag in set(rows.get(cid, ())):
            if tag in TAG_SET:
                counts[tag] += 1
    return [(tag, counts.get(tag, 0)) for tag in TAG_ORDER]


def top_pairs(rows: dict[str, tuple[str, ...]], scope_ids: set[str],
              top: int) -> list[tuple[tuple[str, str], int]]:
    counts = Counter()
    for cid in scope_ids:
        tags = [t for t in clean_tags(rows.get(cid, ())) if t in TAG_SET]
        for a, b in combinations(tags, 2):
            counts[(a, b)] += 1
    # 同分按组合名兜底，否则输出顺序会随 set 遍历漂
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top]


def coverage_numbers(rows, scope_ids, pending_ids) -> dict:
    total = len(scope_ids)
    tagged = sum(1 for cid in scope_ids if rows.get(cid))
    pending = len(pending_ids & scope_ids)
    untagged_not_pending = sum(
        1 for cid in scope_ids if not rows.get(cid) and cid not in pending_ids)
    return {
        "total": total,
        "tagged": tagged,
        "untagged": total - tagged,
        "pending": pending,
        "untagged_not_pending": untagged_not_pending,
        "coverage_pct": round(100.0 * tagged / total, 1) if total else 0.0,
        "pending_pct": round(100.0 * pending / total, 1) if total else 0.0,
    }


def sample_groups(cards, rows, per_class: int, seed: int) -> list[tuple[str, list[Card]]]:
    """固定 seed 抽样：每类 per_class 张 + 无标签 per_class 张，可复现。"""
    rng = random.Random(seed)
    ordered = sorted(cards, key=lambda c: c.id.upper())
    groups = []
    for tag in TAG_ORDER:
        pool = [c for c in ordered if tag in clean_tags(rows.get(c.id.upper(), ()))]
        groups.append((tag, rng.sample(pool, min(per_class, len(pool)))))
    untagged = [c for c in ordered if not rows.get(c.id.upper())]
    groups.append(("（无标签）", rng.sample(untagged, min(per_class, len(untagged)))))
    return groups


def suspicious_untagged(cards, rows, limit: int = SAMPLE_LIMIT) -> dict:
    """0 标签但文本含信号词：规则可能整条漏了，交给人逐张核。"""
    hits = []
    signals = Counter()
    for c in cards:
        if rows.get(c.id.upper()):
            continue
        text = c.text or ""
        words = [w for w in SIGNAL_WORDS if w in text]
        if words:
            signals.update(words)
            hits.append((c, words))
    hits.sort(key=lambda item: (item[0].type, item[0].id.upper()))
    return {
        "count": len(hits),
        "signals": signals.most_common(),
        "samples": [{"card": _card_row(c, rows), "signals": words} for c, words in hits[:limit]],
    }


def _pending_row(entry: dict, rows, db: CardDB) -> dict:
    cid = str(entry.get("id") or "").upper()
    card = db.get(cid) if cid else None
    return {
        "id": cid or "?",
        "name": entry.get("name") or (card.name if card else "?"),
        "text": entry.get("text") or (card.text if card else "") or "",
        "why": entry.get("why") or "",
        "tags": rows.get(cid, ()),
    }


def pending_anomalies(pending, rows, db: CardDB, limit: int = SAMPLE_LIMIT) -> dict:
    """待定清单自身的问题：已打标还进待定、无文本、查不到牌、重复、缺 why。"""
    entries = [p for p in (pending or []) if isinstance(p, dict)]
    ids = [str(p.get("id") or "").upper() for p in entries]
    dup_ids = sorted(cid for cid, n in Counter(i for i in ids if i).items() if n > 1)
    tagged, blank, unknown, no_why = [], [], [], []
    for entry in entries:
        cid = str(entry.get("id") or "").upper()
        card = db.get(cid) if cid else None
        if cid and rows.get(cid):
            tagged.append(entry)
        if cid and card is None:
            unknown.append(entry)
        text = entry.get("text") or (card.text if card else "") or ""
        if not text.strip():
            blank.append(entry)
        if not str(entry.get("why") or "").strip():
            no_why.append(entry)

    def pack(items):
        return [_pending_row(e, rows, db) for e in items[:limit]]

    # 奥秘牌按 CONTEXT 必须进待定：有标签也照样待定，不算冲突，得跟真冲突分开报
    secret = [e for e in tagged if "奥秘" in str(e.get("why") or "")]
    conflict = [e for e in tagged if "奥秘" not in str(e.get("why") or "")]
    return {
        "total": len(entries),
        "tagged_secret_pending": {"count": len(secret), "samples": pack(secret)},
        "tagged_but_pending": {"count": len(conflict), "samples": pack(conflict)},
        "blank_text": {"count": len(blank), "samples": pack(blank)},
        "unknown_card": {"count": len(unknown), "samples": pack(unknown)},
        "duplicate_ids": {"count": len(dup_ids), "samples": dup_ids[:limit]},
        "missing_why": {"count": len(no_why), "samples": pack(no_why)},
    }


def structural_issues(rows, scope_ids, limit: int = SAMPLE_LIMIT) -> dict:
    """数据文件本身的毛病：越界、非法标签名、重复、没按 TAG_ORDER 排。"""
    outside = sorted(cid for cid in rows if cid not in scope_ids)
    invalid = [(cid, tag) for cid, tags in rows.items() for tag in tags if tag not in TAG_SET]
    duplicated = sorted(cid for cid, tags in rows.items() if len(set(tags)) != len(tags))
    unsorted_rows = sorted(cid for cid, tags in rows.items() if tags and tuple(tags) != clean_tags(tags))
    return {
        "outside_scope": {"count": len(outside), "samples": outside[:limit]},
        "invalid_tags": {"count": len(invalid), "samples": [f"{cid}:{tag}" for cid, tag in invalid[:limit]]},
        "duplicate_tags": {"count": len(duplicated), "samples": duplicated[:limit]},
        "unsorted_tags": {"count": len(unsorted_rows), "samples": unsorted_rows[:limit]},
    }


def collect_audit(payload: dict, db: CardDB, in_scope, *, seed: int = DEFAULT_SEED,
                  per_class: int = DEFAULT_PER_CLASS, top: int = DEFAULT_TOP) -> dict:
    """一组纯数据，format_audit 负责排版；测试直接断言这里。"""
    rows = normalize_rows(payload)
    cards = scope_cards(db, in_scope)
    scope_ids = {c.id.upper() for c in cards}
    pending = payload.get("pending") or []
    pending_ids = {
        str(p.get("id") or "").upper() for p in pending if isinstance(p, dict)
    }
    return {
        "generated": {"seed": seed, "per_class": per_class, "top": top, "pending_total": len(pending)},
        "scope": {"total": len(cards), "by_type": type_distribution(cards)},
        "coverage": coverage_numbers(rows, scope_ids, pending_ids),
        "tag_counts": tag_counts(rows, scope_ids),
        "pairs": top_pairs(rows, scope_ids, top),
        "samples": [(label, [_card_row(c, rows) for c in group])
                    for label, group in sample_groups(cards, rows, per_class, seed)],
        "suspicious": suspicious_untagged(cards, rows),
        "pending": pending_anomalies(pending, rows, db),
        "structural": structural_issues(rows, scope_ids),
    }


# ---------- 排版 ----------

def _card_row(card: Card, rows) -> dict:
    return {
        "id": card.id,
        "name": card.name,
        "cost": card.cost,
        "type": card.type,
        "text": card.text or "",
        "tags": rows.get(card.id.upper(), ()),
    }


def display_text(text: str, limit: int = 120) -> str:
    """去掉标记看正文；太长的截断，人工核对够用。"""
    plain = re.sub(r"<[^>]+>", "", text or "")
    plain = plain.replace("$", "").replace("#", "").replace("@", "").replace("_", " ")
    plain = " ".join(plain.split())
    return plain if len(plain) <= limit else plain[:limit] + "…"


def _fmt_card(row: dict, indent: str = "  ", text_limit: int = 120) -> list[str]:
    tags = ",".join(row["tags"]) or "无标签"
    head = f"{indent}{row['id']} {row['name']} {row['cost']}费 {TYPE_CN.get(row['type'], row['type'])} | {tags}"
    lines = [head]
    text = display_text(row["text"], text_limit)
    if text:
        lines.append(f"{indent}    {text}")
    return lines


def _section_index(n: int, title: str) -> list[str]:
    return ["", f"== {n}. {title} =="]


def format_audit(report: dict) -> str:
    gen = report["generated"]
    lines = [f"卡牌标签审计报告（独立复核，seed={gen['seed']}，每类抽 {gen['per_class']} 张）"]

    lines += _section_index(1, "范围")
    scope = report["scope"]
    lines.append(f"scope 总数：{scope['total']}")
    for type_name, count in scope["by_type"]:
        pct = 100.0 * count / scope["total"] if scope["total"] else 0.0
        lines.append(f"  {TYPE_CN.get(type_name, type_name)}（{type_name}） {count}（{pct:.1f}%）")

    lines += _section_index(2, "11 类计数 / 覆盖率")
    for tag, count in report["tag_counts"]:
        lines.append(f"  {tag} {count}")
    cov = report["coverage"]
    lines.append(f"  有标签覆盖率 {cov['coverage_pct']}%（{cov['tagged']}/{cov['total']}）")
    lines.append(f"  无标签 {cov['untagged']} 张；待定 {cov['pending']} 张（占 scope {cov['pending_pct']}%）")
    if cov["untagged_not_pending"]:
        lines.append(f"  !! 0 标签且不在待定清单：{cov['untagged_not_pending']} 张（既没打标也没留待定）")
    else:
        lines.append("  0 标签且不在待定清单：0 张")

    lines += _section_index(3, f"两两共现 top {gen['top']}")
    if not report["pairs"]:
        lines.append("  （无多标签组合）")
    for (a, b), count in report["pairs"]:
        lines.append(f"  {a}+{b} {count}")

    lines += _section_index(4, "抽样核对（固定 seed，可复现）")
    for label, cards in report["samples"]:
        lines.append(f"[{label}] {len(cards)} 张")
        if not cards:
            lines.append("  （该类无牌，规则可能整条没命中）")
        for row in cards:
            lines += _fmt_card(row)

    suspicious = report["suspicious"]
    lines += _section_index(5, "可疑清单：0 标签但文本含信号词")
    lines.append(f"共 {suspicious['count']} 张")
    if suspicious["signals"]:
        lines.append("信号词分布：" + "、".join(f"{w} {n}" for w, n in suspicious["signals"]))
    for item in suspicious["samples"]:
        lines += _fmt_card(item["card"])
        lines.append(f"      命中：{'、'.join(item['signals'])}")

    pending = report["pending"]
    lines += _section_index(6, "待定清单异常")
    lines.append(f"待定总数：{pending['total']}")
    for key, title in (
        ("tagged_secret_pending", "已打标却仍在待定（奥秘，按口径可接受）"),
        ("tagged_but_pending", "已打标却仍在待定（非奥秘，判定冲突）"),
        ("blank_text", "文本为空还进待定"),
        ("unknown_card", "卡牌库查不到"),
        ("duplicate_ids", "重复 id"),
        ("missing_why", "缺 why（原因不可解释）"),
    ):
        group = pending[key]
        lines.append(f"  {title}：{group['count']}")
        for item in group["samples"]:
            if isinstance(item, str):
                lines.append(f"    {item}")
            else:
                lines.append(f"    {item['id']} {item['name']} | {display_text(item['text'], 80)} | why={item['why'] or '（空）'}")

    structural = report["structural"]
    lines += _section_index(7, "结构问题")
    for key, title in (
        ("outside_scope", "rows 里不在 scope"),
        ("invalid_tags", "非法标签名"),
        ("duplicate_tags", "同一张牌标签重复"),
        ("unsorted_tags", "标签未按 TAG_ORDER 排序"),
    ):
        group = structural[key]
        lines.append(f"  {title}：{group['count']}")
        for sample in group["samples"]:
            lines.append(f"    {sample}")

    lines.append("")
    return "\n".join(lines)


# ---------- CLI ----------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="独立审计 data/card_tags.json 的标签质量")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="抽样随机种子（默认 %(default)s）")
    parser.add_argument("--per-class", type=int, default=DEFAULT_PER_CLASS, help="每类抽样张数（默认 %(default)s）")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP, help="共现组合展示个数（默认 %(default)s）")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # pragma: no cover - 老终端不支持 reconfigure 就算了
        pass

    path = tags_path()
    if not path.exists():
        print(f"没有 {path}：先跑 python -m hsai tags build", file=sys.stderr)
        return 2
    from hsai import tags as tags_mod  # 延迟导入：本工具在 B 的 tags.py 就绪前也能跑测试

    db = load()
    payload = json.loads(path.read_text(encoding="utf-8"))
    report = collect_audit(payload, db, tags_mod.in_scope, seed=args.seed,
                           per_class=args.per_class, top=args.top)
    print(format_audit(report), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
