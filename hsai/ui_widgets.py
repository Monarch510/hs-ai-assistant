"""通用小部件与窗口几何：区块、分隔线、折叠、自适应高度。"""

from __future__ import annotations

import tkinter as tk

from .ui_state import _load_state, _save_state
from .ui_theme import (
    BG, BORDER, COLLAPSED_H, FONT, GOLD, MUTED, PANEL, TEXT_SOFT, YELLOW,
)


class Widgets:
    """区块/分隔线/折叠/几何。Overlay 继承它。"""

    # --- 通用小部件
    def _toggle_section(self, title: str) -> None:
        """点标题折叠/展开；状态存 window.json，重开窗口还记得。"""
        if title in self._collapsed:
            self._collapsed.discard(title)
        else:
            self._collapsed.add(title)
        data = _load_state()
        data["collapsed_sections"] = sorted(self._collapsed)
        _save_state(data)
        self._last_sig = None      # 强制重画
        if getattr(self, "_last_state", None) is not None:
            self._render(self._last_state)

    def _section(self, title: str, kicker: str = "") -> tk.Frame:
        """一张带标题的卡片。

        标题必须和内容在同一个 PANEL 底色的框里 —— 背景改成透明后，
        浮在 BG 上的标题会直接压在游戏画面上，亮色场景里根本看不清。
        """
        wrap = tk.Frame(self.body, bg=BG)
        wrap.pack(fill="x", pady=(6, 0))
        card = tk.Frame(wrap, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="x")
        head = tk.Frame(card, bg=PANEL)
        head.pack(fill="x", padx=9, pady=(7, 0))
        folded = title in self._collapsed
        mark = "▸ " if folded else "▾ "
        lbl = tk.Label(head, text=mark + title, bg=PANEL, fg=GOLD, cursor="hand2",
                       font=(FONT, self.font_size - 1, "bold"), anchor="w")
        lbl.pack(side="left")
        lbl.bind("<Button-1>", lambda _e, t=title: self._toggle_section(t))
        if kicker and kicker not in ("核心", "决策"):
            # 「核心」「决策」这类后缀没信息量，不显示
            tk.Label(head, text=kicker, bg=PANEL, fg="#5b636e",
                     font=(FONT, self.font_size - 3), anchor="e").pack(side="right")
        inner = tk.Frame(card, bg=PANEL)
        if not folded:
            inner.pack(fill="x", padx=9, pady=(4, 7))
        return inner

    def _strip(self, text: str, *, fg: str, bg: str) -> None:
        bar = tk.Frame(self.body, bg=bg, highlightthickness=1, highlightbackground=bg)
        bar.pack(fill="x", pady=(6, 0))
        tk.Label(bar, text=text, bg=bg, fg=fg, font=(FONT, self.font_size - 1),
                 anchor="w", justify="left", wraplength=self.width - 40, padx=8, pady=5).pack(fill="x")

    def _render_collapsed(self) -> None:
        if self.collapsed:
            self.panel.pack_forget()
            self.bar.pack(fill="both", expand=True)
        else:
            self.bar.pack_forget()
            self.panel.pack(fill="both", expand=True)
            self._last_sig = None
        # 立刻先按当前状态设一次尺寸，再用延迟 fit 修正内容高度 ——
        # 只靠延迟 fit 的话，收起时会残留上一次展开的高度
        self._apply_geometry()
        self._request_fit()

    # ------------------------------------------------------------ 渲染
    def _show_placeholder(self) -> None:
        """窗口一出现就要有字，不能等第一条日志 —— 否则看起来就是个空框。"""
        self.header_label.config(text="炉石 AI 助手 · 正在读取日志")
        self.source_label.config(text="示例" if self.manager.is_demo else "", fg=YELLOW)
        for child in self.body.winfo_children():
            child.destroy()

        if getattr(self, "_render_broken", False):
            box = tk.Frame(self.body, bg="#3d1418")
            box.pack(fill="x", pady=(0, 6))
            tk.Label(box, text="⚠ 面板出错，建议可能不更新",
                     bg="#3d1418", fg="#ffb3ad",
                     font=(FONT, self.font_size - 1, "bold"), anchor="w",
                     padx=6, pady=3).pack(fill="x")
            if getattr(self, "_last_error", ""):
                tk.Label(box, text=str(self._last_error)[:80], bg="#3d1418",
                         fg="#e0a0a0", font=(FONT, self.font_size - 3),
                         anchor="w", justify="left", wraplength=self.width - 24,
                         padx=6).pack(fill="x")
        tk.Label(self.body, text="等待对局开始…", bg=BG, fg=MUTED,
                 font=(FONT, self.font_size), anchor="w").pack(fill="x", pady=6)
        parent = self.manager.name.split("：")[0]
        tk.Label(self.body, text=f"数据源：{parent}（{self.manager.name.split(chr(65306))[-1][:48]}）",
                 bg=BG, fg="#5b636e", font=(FONT, self.font_size - 2), anchor="w",
                 justify="left", wraplength=self.width - 40).pack(fill="x")

    def _request_fit(self) -> None:
        """把「量高度/设尺寸」推迟到这一轮事件处理完之后。

        直接同步量会读到上一帧的布局（Tk 的 pack 要等 idle 才生效），
        表现就是窗口高度忽大忽小、内容少的反而更高。
        """
        if self._fit_pending:
            return
        self._fit_pending = True
        try:
            self.root.after_idle(self._do_fit)
        except tk.TclError:
            self._fit_pending = False

    def _do_fit(self) -> None:
        """量高度并落定。

        Tk 的尺寸传播要等一次完整的事件循环（update_idletasks 不够，
        带 wraplength 的 Label 需要真正排一次版），所以这里主动 update()，
        并用 _in_fit 挡住重入的定时器回调。
        """
        self._fit_pending = False
        self._in_fit = True
        try:
            # 只用 update_idletasks：在定时回调里调 update() 会重入事件循环，
            # 有可能把 Tk 留在「几何改了但还没重绘」的状态里。
            self.root.update_idletasks()
            self._apply_geometry()
        except tk.TclError:
            pass
        finally:
            self._in_fit = False

    def _apply_geometry(self) -> None:
        """顶部锚定地重排窗口：顶边固定，只往下长。

        这里有个 Tk 的坑：直接读 body.winfo_reqheight() 只会「越变越大」——
        窗口当前尺寸会把 pack 的请求尺寸撑住，内容减少后也缩不回来。
        所以先把它压到 1px 高，逼 Tk 重新计算内容本来的高度，再设最终尺寸。
        """
        try:
            self.root.update_idletasks()
            # 真正可靠的是 panel 的请求高度：root/container 用了
            # pack(fill=both, expand=True)，它们的 reqheight 被压成 1，
            # 只有 panel 会老实报「内容本来需要多高」。
            if self.collapsed:
                h = COLLAPSED_H + 2
            else:
                h = max(96, self.panel.winfo_reqheight() + 2)
            x, y = self.pos
            # 兜底：内容太高时别让它长到屏幕外面（只在这一步动 y，不动锚点）
            screen_h = self.root.winfo_screenheight()
            if y + h > screen_h - 6:
                y = max(0, screen_h - 6 - h)
            self.root.geometry(f"{self.width}x{h}+{x}+{y}")
        except tk.TclError:
            pass
