"""命令行：启动悬浮窗。"""

from __future__ import annotations

import argparse
from pathlib import Path

from . import cards as cards_mod


def cmd_ui(args: argparse.Namespace) -> int:
    """启动悬浮窗（无控制台）。这是默认入口。"""
    if getattr(args, "check", False):
        # 给启动脚本用的预检：环境不对就当场报错，而不是启动后什么都没出现
        try:
            import tkinter  # noqa: F401
        except Exception as exc:  # pragma: no cover - 取决于 Python 发行版
            print(f"缺少 tkinter（图形界面组件）：{exc}")
            return 3
        db = cards_mod.load(auto_update=False)
        if not len(db):
            print("卡牌库为空，先运行：python -m hsai cards --update")
            return 4
        print(f"环境检查通过：卡牌库 {len(db)} 张")
        return 0
    from . import ui

    return ui.main(demo=args.demo, log_path=Path(args.log) if args.log else None)
