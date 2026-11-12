"""learn 的自举环境与先验校准 / 真值回填。

门面 learn.py 从这里 re-export；子模块之间可互相 import，但不 import learn 门面。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .advisor import OBSERVATIONS_PATH, load_observations
from .cards import CardDB
from .infer import DeckPredictor
from .meta import MetaDB, build_from_observations

def build_local_meta(cards: CardDB, *, min_games: int = 3, threshold: float = 0.34,
                     observations_path: Path = OBSERVATIONS_PATH) -> tuple[MetaDB, int]:
    obs = load_observations(observations_path)
    db = build_from_observations(obs, cards, min_games=min_games, threshold=threshold)
    return db, len(obs)

def _row_truth(row: dict[str, Any], predictor) -> tuple[str, bool]:
    """这条记录的（真值原型 id, 是否可信）。

    **有 revealed 就按当前原型库重算一遍**，不直接用存下来的 `truth.arch`：
    原型库会变（重建、导入新卡组、删掉一套牌），而 id 是跟着内容走的 ——
    存的 id 可能已经指向一个不存在的原型。重算得到的是「按今天这套原型，
    这局算谁」，正好是先验该统计的东西。

    只有连 revealed 都没有的老行，才退回读存下来的 truth。
    """
    cls = (row.get("opp_class") or "").upper()
    revealed = row.get("revealed") or []
    if cls and revealed:
        t = predictor.retrospect(cls, revealed)
        return t.archetype_id, t.trusted
    saved = row.get("truth") or {}
    return str(saved.get("arch") or ""), bool(saved.get("trusted"))

def calibrate_priors(meta: MetaDB, cards: CardDB,
                     observations_path: Path = OBSERVATIONS_PATH) -> dict[str, Any]:
    """按「你实际遇到的比例」修正先验：环境是本地化的。

    只统计**真值可信**的局（`truth.trusted`）。以前统计的是 `opp_archetype`，
    那是当时的预测值 —— 预测偏哪套，先验就往哪套偏，越用越偏。
    真值本身按当前原型库重算（见 `_row_truth`），所以重建原型之后不用回填也不会串味。

    返回报告只是把过程说清楚，统计算法与顺序一个字都没改。
    """
    path = Path(observations_path)
    report: dict[str, Any] = {
        "rows": 0, "no_class": 0, "untrusted": 0, "trusted": 0,
        "updated": [], "skipped": [], "total_rows": 0,
    }
    obs = load_observations(observations_path)
    if path.exists():
        # load_observations 会静默丢掉坏行；总行数得单独数，才知道剔了多少
        report["total_rows"] = sum(
            1 for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip())
    if not obs:
        return report
    report["rows"] = len(obs)
    predictor = DeckPredictor(meta, cards)
    counts: dict[str, int] = {}
    by_class: dict[str, int] = {}
    for row in obs:
        cls = (row.get("opp_class") or "").upper()
        if not cls:
            report["no_class"] += 1
            continue
        arch, trusted = _row_truth(row, predictor)
        if not trusted:
            report["untrusted"] += 1
            continue
        report["trusted"] += 1
        by_class[cls] = by_class.get(cls, 0) + 1
        if arch:
            counts[arch] = counts.get(arch, 0) + 1

    for arch in meta:
        total = by_class.get(arch.card_class, 0)
        if total < 5:
            report["skipped"].append({
                "id": arch.id, "name": arch.name, "class": arch.card_class,
                "samples": total, "reason": f"本职业可信样本 {total} < 5",
            })
            continue
        before = arch.prior
        seen = counts.get(arch.id, 0) / total
        # 与**校准前**的先验混合，样本少时不要剧烈跳变。
        # 起点必须是 base_prior 而不是当前 prior —— 否则同一个命令跑三遍，
        # 先验会被逐步「洗」成观测频率（实测 0.76 → 0.87 → 0.91 → 0.92）。
        #
        # 老文件没有 prior_base，这里必须**先把它钉住再改 prior**：
        # 如果等到 save() 时才读 base_prior，读到的已经是覆盖后的值，
        # 下一轮就以校准结果为起点，照样漂移。
        if arch.prior_base is None:
            arch.prior_base = arch.prior
        weight = min(0.7, total / 20)
        arch.prior = (1 - weight) * arch.prior_base + weight * max(seen, 0.02)
        report["updated"].append({
            "id": arch.id, "name": arch.name, "class": arch.card_class,
            "before": before, "after": before, "samples": total, "weight": weight,
        })
    meta.normalize_priors()
    # normalize_priors() 还会再动一次 prior，after 必须是最终值
    for item in report["updated"]:
        current = meta.get(item["id"])
        if current is not None:
            item["after"] = current.prior
    return report

def backfill_truth(cards: CardDB, meta: MetaDB, *,
                   observations_path: Path = OBSERVATIONS_PATH,
                   dry_run: bool = False) -> dict[str, Any]:
    """把历史记录里的 `truth` 按当前原型库补一遍。

    为什么要它：schema 2 之前的行只有一个 `opp_archetype`（预测值），没有 truth。
    先验校准只吃真值，所以那 170 多局历史一局都没进过学习 —— 表现是
    `meta calibrate` 永远输出「修正 0 个原型」，而且不报错。

    只补 truth / unexpected_cards 两个字段，其余原样保留（不重排、不丢字段）。
    写盘前留一份 `.bak`。
    """
    path = Path(observations_path)
    if not path.exists():
        return {"total": 0, "filled": 0, "trusted": 0, "written": False}
    predictor = DeckPredictor(meta, cards)

    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    total = filled = trusted = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            out.append(line)      # 坏行原样留着，不替用户丢数据
            continue
        total += 1
        had = bool(row.get("truth"))
        t = predictor.retrospect((row.get("opp_class") or "").upper(),
                                 row.get("revealed") or [])
        # 算不出归属时保留原 truth，别把人工/老 schema 的真值抹成 None
        if t.archetype_id:
            row["truth"] = t.as_dict()
            row["unexpected_cards"] = list(t.unexplained)
        if t.trusted:
            trusted += 1
        if not had and t.archetype_id:
            filled += 1
        out.append(json.dumps(row, ensure_ascii=False))

    written = False
    if not dry_run:
        backup = path.with_suffix(path.suffix + ".bak")
        try:
            backup.write_bytes(path.read_bytes())
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
            os.replace(tmp, path)
            written = True
        except OSError:
            written = False
    return {"total": total, "filled": filled, "trusted": trusted, "written": written}
