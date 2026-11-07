"""命令行：实战复盘。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def cmd_review(args: argparse.Namespace) -> int:
    from . import review

    games = review.load_games()
    if not games:
        print("还没有对局记录。打完一局（并且胜负已判定）再来看。")
        return 2
    for g in games[-args.n:]:
        print(f"{g.get('ts','')}  {g.get('result','')}  {g.get('our_class','')} vs {g.get('opp_class','')}  "
              f"{len(g.get('turns') or [])} 个我方回合")
        for line in review.summarize(g):
            print("   -", line)
        print()
    return 0
