"""命令行：离线解析与终端跟随日志。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .advisor import Advisor
from .cli_boot import _db
from .logwatch import PowerLogWatcher, find_power_log
from .state import replay_log_text


def cmd_replay(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.is_file():
        print(f"日志文件不存在：{path}")
        return 2
    db = _db()
    st = replay_log_text(path.read_text(encoding="utf-8", errors="replace"), db)
    advisor = Advisor(db)
    snap = advisor.snapshot(st)
    print(st.summary())
    print(json.dumps(snap, ensure_ascii=False, indent=1))
    return 0

def cmd_log(args: argparse.Namespace) -> int:
    db = _db()
    path = Path(args.log) if args.log else find_power_log()
    if path is None:
        print("没找到 Power.log。请确认游戏装过并至少启动过一次，或手动指定 --log。")
        return 2
    print(f"跟随日志：{path}")
    advisor = Advisor(db)
    watcher = PowerLogWatcher(path, db)
    last = -1
    try:
        while True:
            if watcher.poll() and watcher.version != last:
                last = watcher.version
                snap = advisor.snapshot(watcher.state)
                _print_live(snap)
            import time

            time.sleep(0.4)
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0

def _print_live(snap: dict) -> None:
    print("-" * 70)
    print(f"[{snap['phase']}] T{snap['turn']} 我方{snap['our']['class_cn']} {snap['our']['health']} "
          f"vs 对方{snap['opp']['class_cn']} {snap['opp']['health']}")
    pred = snap.get("prediction") or {}
    if pred:
        print(f"预判：{pred.get('name')}  {pred.get('confidence', 0):.0%}")
        for line in pred.get("evidence", [])[:4]:
            print(f"   · {line}")
    if snap.get("warning"):
        print(f"⚠ {snap['warning']}")
    adv = snap.get("advice") or {}
    for line in adv.get("lines", [])[:2]:
        print(f"建议{line['rank']}（{line['score']}）：{line['title']} —— {line['reason']}")
