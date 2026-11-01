"""布局预览：只用假数据画三块区域，不接真实对局。

跑一下会弹出一个 260 宽的窗口，截完图就关掉——用来在改代码前看效果。
"""

from __future__ import annotations

import tkinter as tk

FONT = "Microsoft YaHei UI"
BG = "#14171c"
PANEL = "#1c2128"
TEXT = "#e6e6e6"
MUTED = "#8b949e"
GOLD = "#d4a24a"
WIDTH = 260


def section(parent, title, kicker):
    box = tk.Frame(parent, bg=PANEL)
    box.pack(fill="x", padx=8, pady=(0, 6))
    head = tk.Frame(box, bg=PANEL)
    head.pack(fill="x", padx=6, pady=(5, 2))
    tk.Label(head, text=title, bg=PANEL, fg=GOLD,
             font=(FONT, 9, "bold"), anchor="w").pack(side="left")
    tk.Label(head, text=kicker, bg=PANEL, fg=MUTED,
             font=(FONT, 7), anchor="e").pack(side="right")
    body = tk.Frame(box, bg=PANEL)
    body.pack(fill="x", padx=6, pady=(0, 6))
    return body


def line(parent, text, fg=TEXT, size=9, wrap=True):
    tk.Label(parent, text=text, bg=PANEL, fg=fg, font=(FONT, size),
             anchor="w", justify="left",
             wraplength=WIDTH - 40 if wrap else 0).pack(fill="x", pady=1)


def main() -> None:
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg=BG)
    root.geometry(f"{WIDTH}x620")

    bar = tk.Frame(root, bg=PANEL, height=30)
    bar.pack(fill="x")
    tk.Label(bar, text="T7 · 我方回合 · 手牌 6:4    对局中", bg=PANEL, fg=TEXT,
             font=(FONT, 9, "bold"), anchor="w", padx=8).pack(fill="x", pady=5)

    body = tk.Frame(root, bg=BG)
    body.pack(fill="both", expand=True, pady=(6, 0))

    opp = section(body, "对手信息", "▾ 核心")
    line(opp, "死亡骑士·掘骨恶鬼", GOLD, size=10)
    line(opp, "清场 45% · 单解 38%")
    line(opp, "最可能：暗言术：毁（4费）", MUTED, size=8)

    match = section(body, "对战信息", "▾")
    line(match, "我方 26 血 · 5 费 · 6 张")
    line(match, "对手 20 血 · 4 张")

    mine = section(body, "我的信息", "▾ 决策")
    line(mine, "① 第2位 女巫的学徒 3/4 攻击第1位 食尸鬼")
    line(mine, "② 或者：打出 癫狂元素（3费 2/2）", MUTED)
    line(mine, "克苏恩 3/4 · 战吼还差 2 只", MUTED, size=8)
    line(mine, "对方约 45% 概率持有清场，别铺满", "#c9a05a", size=8)

    root.update()
    root.after(200)
    root.mainloop()


if __name__ == "__main__":
    main()
