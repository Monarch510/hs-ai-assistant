"""命令行：自对弈。"""

from __future__ import annotations

import argparse

from . import sim
from .cli_boot import _db
from .meta import MetaDB


def cmd_sim(args: argparse.Namespace) -> int:
    if args.games < 1:
        print("局数必须是正整数")
        return 2
    db = _db()
    meta = MetaDB.load(db)
    a = meta.get(args.a) or next((x for x in meta if x.name == args.a), None)
    b = meta.get(args.b) or next((x for x in meta if x.name == args.b), None)
    if a is None or b is None:
        print("找不到这两个原型。用 `python -m hsai meta list` 查看可用 id。")
        return 2
    if args.trace:
        import random

        rng = random.Random(args.seed)
        out = sim.run_game(sim.make_deck(a, db, rng), sim.make_deck(b, db, random.Random(args.seed + 1)),
                           db, seed=args.seed, trace=True, max_turns=args.max_turns,
                           class_a=a.card_class, class_b=b.card_class)
        print("\n".join(out.log))
        print(f"\n结果：{'A 胜' if out.winner == 0 else 'B 胜' if out.winner == 1 else '平/超时'}，{out.turns} 回合")
        return 0
    result = sim.match(a, b, db, games=args.games, seed=args.seed)
    print(f"{a.name} vs {b.name}：{result['games']} 局")
    print(f"  A {result['wins'][0]} 胜 / B {result['wins'][1]} 胜 / 平 {result['draws']}")
    print(f"  A 胜率 {result['winrate_a']:.1%}")
    return 0
