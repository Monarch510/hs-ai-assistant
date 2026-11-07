"""命令行：对局统计 / AI 复盘的参数建议汇总与应用。

应用建议是**显式动作**：只在人敲了 `--apply` 时发生，且要过门槛、方向一致、
幅度衰减，并先备份原文件。AI 每局只看一局，样本是 1；不设这些闸就等于拿
单局噪声改全局权重。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from . import learn, memory
from .model import DEFAULT_WEIGHTS
from .paths import POLICY_WEIGHTS_JSON, TAG_WEIGHTS_JSON

# 一次最多把某个参数挪多少：AI 单局只会给 ±0.3，这里再打对折，
# 免得连续几局同方向建议叠加成一次大幅跳变。
_STEP = 0.15
# 一致性低于这个比例（方向摇摆）就不采纳
_MIN_AGREE = 0.75
# 标签权重的合法范围（跟 tag_weights._MIN_W / _MAX_W 对齐）
_MIN_W, _MAX_W = -2.0, 6.0


def cmd_stats(args: argparse.Namespace) -> int:
    data = learn.stats()
    print(json.dumps(data, ensure_ascii=False, indent=1))
    return 0


def _apply_one(path: Path, updates: dict[str, float], label: str) -> None:
    """把 updates 合并进 {"weights": {...}} 文件，保留用户手写的其它键。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    weights = payload.get("weights")
    if not isinstance(weights, dict):
        weights = {}
    merged = {**weights, **updates}
    payload["weights"] = merged
    if path.exists():
        try:
            path.with_suffix(".json.bak").write_bytes(path.read_bytes())
        except OSError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  {label} -> {path}")


def cmd_tunings(args: argparse.Namespace) -> int:
    """把 AI 多局复盘攒下来的参数建议按参数汇总；--apply 才写权重。"""
    rows = memory.summarize_tunings(min_count=args.min_count)
    if not rows:
        print(f"还没有任何参数被至少 {args.min_count} 局反复提到（或还没有复盘记录）。")
        return 0

    print(f"AI 复盘的参数建议（只出现 ≥{args.min_count} 局的参数）：")
    policy_updates: dict[str, float] = {}
    tag_updates: dict[str, float] = {}
    for param in sorted(rows):
        r = rows[param]
        arrow = {"up": "调高", "down": "调低", "mixed": "方向摇摆"}[r["direction"]]
        print(f"  {param:<10} {r['count']:>3} 局  "
              f"平均 {r['mean']:+.3f}  {arrow}（一致性 {r['agree']:.0%}）")
        if r["direction"] == "mixed" or r["agree"] < _MIN_AGREE:
            continue
        step = _STEP if r["mean"] > 0 else -_STEP
        if param in DEFAULT_WEIGHTS:
            policy_updates[param] = step
        else:
            tag_updates[param] = step

    if not (policy_updates or tag_updates):
        print("\n没有方向一致且达标的参数，权重不变。")
        return 0

    if not args.apply:
        print("\n以上只是建议，没有写入任何权重文件。确认后加 --apply。")
        return 0

    # 从当前值出发，而不是从默认值：别把自对弈调优的结果冲掉
    cur_policy = learn.load_weights()
    cur_tag = _load_tag_weights()
    final_policy = {k: round(max(0.0, cur_policy.get(k, 0.0) + v), 3)
                    for k, v in policy_updates.items()}
    final_tag = {k: round(max(_MIN_W, min(_MAX_W, cur_tag.get(k, 0.0) + v)), 3)
                 for k, v in tag_updates.items()}
    print("\n写入（原文件已备份成 *.json.bak）：")
    if final_policy:
        _apply_one(POLICY_WEIGHTS_JSON, final_policy, "policy_weights")
    if final_tag:
        _apply_one(TAG_WEIGHTS_JSON, final_tag, "tag_weights")
    print("重启悬浮窗后生效。")
    return 0


def _load_tag_weights() -> dict[str, float]:
    from .tag_weights import DEFAULT_TAG_WEIGHTS

    try:
        payload = json.loads(TAG_WEIGHTS_JSON.read_text(encoding="utf-8"))
        w = payload.get("weights") or {}
        return {**DEFAULT_TAG_WEIGHTS, **(w if isinstance(w, dict) else {})}
    except (OSError, ValueError):
        return dict(DEFAULT_TAG_WEIGHTS)
