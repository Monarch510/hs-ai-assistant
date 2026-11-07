"""命令行：自对弈 + 权重优化 + 真值回填。"""

from __future__ import annotations

import argparse
import json

from . import learn
from .cli_boot import _db
from .meta import MetaDB


def cmd_learn(args: argparse.Namespace) -> int:
    db = _db()
    meta = MetaDB.load(db)
    if args.backfill:
        # 一次性迁移：老行没有 truth，先验校准会把它们全跳过（而且不报错）
        report = learn.backfill_truth(db, meta, dry_run=args.dry_run)
        if not report["total"]:
            print("没有对局记录，不用回填。")
            return 2
        verb = "将回填" if args.dry_run else "已回填"
        print(f"{verb} {report['total']} 局：新补上真值 {report['filled']} 局，"
              f"其中可信 {report['trusted']} 局")
        if args.dry_run:
            print("（--dry-run：没有写盘）")
        elif not report["written"]:
            print("写盘失败，原文件未改动。")
            return 1
        else:
            print(f"备份：{learn.OBSERVATIONS_PATH}.bak")
            print("接着跑 `python -m hsai meta calibrate` 让先验真正吃到这批数据。")
        return 0
    if args.mulligan:
        w, report = learn.optimize_mulligan(db, meta, games=args.games, rounds=args.rounds, seed=args.seed,
                                            progress=print)
        learn.save_mulligan_weights(w)
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=1))
        return 0
    w, report = learn.optimize(db, meta, games=args.games, rounds=args.rounds, seed=args.seed,
                               mulligan_weights=learn.load_mulligan_weights(), progress=print)
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=1))
    # 换一组种子再测一次：训练集上的提升必须能在没见过的局面上站住才值得信
    hold_wr, hold_n = learn.mirror_winrate(
        db, meta, w, dict(learn.DEFAULT_WEIGHTS), games=args.games, seed=args.seed + 977,
    )
    if hold_wr >= 0.52:
        verdict = "站得住，写入权重"
    elif hold_wr >= 0.48:
        verdict = "基本持平（没有可确认的提升）"
    else:
        verdict = "没站住，大概率是过拟合"
    print(f"换种子验证（{hold_n} 局）：{hold_wr:.1%} vs 基准 50% —— {verdict}")
    if hold_wr >= 0.52 or args.force:
        learn.save_weights(w)
        print(f"已写入权重 -> {learn.WEIGHTS_PATH}")
    else:
        print("未写入：验证没过就不改你的权重（确实想写就加 --force）")
    return 0
