"""长期记忆：分标签的教训 + 可检索的历史案例。

为什么要分标签：之前所有教训混在一起，打快攻时会把「对控制留解牌」
的建议也塞进 prompt。教训必须带上「我方职业 / 对手职业 / 对手形态」，
用的时候只取匹配的那些。

案例则是「具体局面 → 当时怎么打 → 结果」，比抽象教训更容易被模仿。
检索不依赖向量，用职业和局面特征打分即可。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .paths import CASES_JSONL, LESSONS_JSONL, OPP_HISTORY_JSONL

LESSONS_PATH = LESSONS_JSONL
CASES_PATH = CASES_JSONL
MAX_LESSONS = 200
OPP_HISTORY_PATH = OPP_HISTORY_JSONL
MAX_OPP_HISTORY = 200
MAX_CASES = 400


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    except OSError:
        return []
    return rows


def _append_jsonl(path: Path, rows: list[dict[str, Any]], cap: int) -> None:
    if not rows:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        all_rows = _read_jsonl(path)
        if len(all_rows) > cap:
            kept = all_rows[-cap:]
            path.write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept),
                encoding="utf-8",
            )
    except OSError:
        pass


def record_opponent(opp_class: str, archetype: str) -> None:
    """记一局遇到的对手卡组形态（用于下次同职业时的先验）。"""
    if not opp_class or not archetype:
        return
    _append_jsonl(OPP_HISTORY_PATH,
                  [{"opp_class": opp_class, "archetype": archetype}],
                  MAX_OPP_HISTORY)


def class_counts(opp_class: str, limit: int = 60) -> dict[str, int]:
    """最近 limit 局里，这个职业各形态各出现了多少次。"""
    rows = _read_jsonl(OPP_HISTORY_PATH)[-limit:]
    out: dict[str, int] = {}
    for row in rows:
        if row.get("opp_class") != opp_class:
            continue
        name = str(row.get("archetype") or "")
        if name:
            out[name] = out.get(name, 0) + 1
    return out


# ---------------------------------------------------------------- 教训
def add_lessons(lessons: list[str], meta: dict[str, Any]) -> int:
    rows = []
    for text in lessons:
        text = str(text).strip()
        if not text:
            continue
        rows.append({
            "lesson": text,
            "our_class": meta.get("our_class") or "",
            "opp_class": meta.get("opp_class") or "",
            "archetype": meta.get("archetype") or "",
            "result": meta.get("result") or "",
        })
    _append_jsonl(LESSONS_PATH, rows, MAX_LESSONS)
    return len(rows)


def _lesson_score(row: dict[str, Any], our_class: str, opp_class: str, archetype: str) -> int:
    score = 0
    if opp_class and row.get("opp_class") == opp_class:
        score += 3
    if our_class and row.get("our_class") == our_class:
        score += 2
    if archetype and row.get("archetype") == archetype:
        score += 2
    if not row.get("opp_class") and not row.get("our_class"):
        score += 1          # 老的通用教训，仍然有点用
    return score


def query_lessons(our_class: str, opp_class: str, archetype: str,
                  limit: int = 2) -> list[str]:
    """取和当前对局最相关的教训（同对手职业优先）。"""
    rows = _read_jsonl(LESSONS_PATH)
    scored = [(_lesson_score(r, our_class, opp_class, archetype), r) for r in rows]
    # 至少要和「对手职业」或「形态」对上；只同职业不算相关
    scored = [x for x in scored if x[0] >= 3]
    scored.sort(key=lambda x: -x[0])
    out: list[str] = []
    for _, row in scored:
        text = str(row.get("lesson") or "").strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 案例
def add_cases(records: list[dict[str, Any]], meta: dict[str, Any]) -> int:
    rows = []
    for rec in records:
        row = dict(rec)
        row.update({
            "our_class": meta.get("our_class") or "",
            "opp_class": meta.get("opp_class") or "",
            "archetype": meta.get("archetype") or "",
            "result": meta.get("result") or "",
        })
        rows.append(row)
    _append_jsonl(CASES_PATH, rows, MAX_CASES)
    return len(rows)


def _case_score(row: dict[str, Any], our_class: str, opp_class: str,
                archetype: str, mana: int, board_n: int) -> float:
    score = 0.0
    if opp_class and row.get("opp_class") == opp_class:
        score += 4
    if our_class and row.get("our_class") == our_class:
        score += 2
    if archetype and row.get("archetype") == archetype:
        score += 3
    if isinstance(row.get("mana"), int):
        score += max(0.0, 1.5 - abs(row["mana"] - mana) * 0.3)
    if isinstance(row.get("our_board_n"), int):
        score += max(0.0, 1.0 - abs(row["our_board_n"] - board_n) * 0.25)
    if row.get("result") == "win":
        score += 0.5
    return score


def query_cases(our_class: str, opp_class: str, archetype: str,
                mana: int, board_n: int, limit: int = 2) -> list[dict[str, Any]]:
    """取最相似的历史决策案例。"""
    rows = _read_jsonl(CASES_PATH)
    scored = [(_case_score(r, our_class, opp_class, archetype, mana, board_n), r) for r in rows]
    scored = [x for x in scored if x[0] >= 3]
    scored.sort(key=lambda x: -x[0])
    return [row for _, row in scored[:limit]]
