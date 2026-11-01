"""悬浮窗：一个无边框、始终置顶的原生窗口。

为什么是 Tkinter：Python 自带，双击就能跑，不需要 WebView2 运行时、不需要 pip、
不需要 Node。代价是样式能力有限，所以这里全部用 Frame/Label/Canvas 手搭，
配色参考炉石的金+深蓝，字号克制，只放「一眼能看完」的信息。

窗口行为（都是和用户确认过的）：
  · 无边框、始终置顶、按住顶部拖动、位置/透明度/字号记住
  · 右键菜单：透明度、字号、收起、退出
  · F8 全局显隐（用 GetAsyncKeyState 轮询，避免另开消息循环）
  · 不在对局时自动缩成一条 28px 细条
"""

from __future__ import annotations

import ctypes
import json
import sys
from datetime import datetime
import threading
import time
import tkinter as tk
from tkinter import messagebox
from pathlib import Path
from typing import Any

from . import __version__
from .advisor import Advisor, board_from_state
from . import ai as ai_mod
from . import ai_mulligan
from . import effects_ai
from . import memory
from . import model as model_mod
from . import policy as policy_mod
from . import cards as cards_mod
from .cards import DATA_DIR, CardDB
from .live import SourceManager
from .state import GameState

STATE_PATH = DATA_DIR / "window.json"

# 窗口默认宽度（窄一点，贴边时不挡牌桌）
DEFAULT_WIDTH = 260
# 改了布局参数就 +1：老配置文件里的宽度会被忽略，位置照旧保留
LAYOUT_VERSION = 7

# ---------------------------------------------------------------- 配色
# BG 同时是「透明键」：窗口上凡是这个颜色的像素都会被抠掉，露出下面的游戏。
# 所以它只能用在「缝隙」上（root / container / body），卡片必须用 PANEL。
BG = "#0f131a"
PANEL = "#171d26"
PANEL_2 = "#1d2531"
BORDER = "#30363d"
TEXT = "#e6edf3"
TEXT_SOFT = "#c9d1d9"
MUTED = "#8b949e"
GOLD = "#d4a340"
GREEN = "#3fb950"
RED = "#f85149"
YELLOW = "#d29922"
BLUE = "#58a6ff"

FONT = "Microsoft YaHei UI"
COLLAPSED_H = 28


def _load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(data: dict[str, Any]) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


class Overlay:
    def __init__(self, cards: CardDB, *, demo: bool = False, log_path: Path | None = None) -> None:
        self.cards = cards
        self.advisor = Advisor(cards)
        self.manager = SourceManager(cards, demo=demo, log_path=log_path)

        saved = _load_state()
        same_layout0 = int(saved.get("layout", 0)) == LAYOUT_VERSION
        self.alpha = float(saved.get("alpha", 0.90)) if same_layout0 else 0.90
        # 背景模式：solid（整窗半透明，看得清） / key（把底色抠成真透明，缝隙能看见游戏）
        self.bg_mode = saved.get("bg_mode", "solid") if same_layout0 else "solid"
        self.font_size = int(saved.get("font_size", 10))
        self.pos = (int(saved.get("x", 40)), int(saved.get("y", 60)))
        self.collapsed = bool(saved.get("collapsed", False))
        same_layout = int(saved.get("layout", 0)) == LAYOUT_VERSION
        self.width = int(saved.get("w", DEFAULT_WIDTH)) if same_layout else DEFAULT_WIDTH
        self._last_sig: tuple | None = None
        self._game_over_since: float | None = None
        self._f8_down = False
        self._dragging = False
        self._fit_pending = False
        self._in_fit = False

        self.root = tk.Tk()
        self.root.withdraw()  # 先藏起来，摆好位置再显示，避免闪一下
        self.root.title("炉石 AI 助手")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", self.alpha)
        self.root.configure(bg=BG)
        # 把这个颜色抠成透明：游戏画面能从缝隙里透出来。
        # 个别环境不支持就退回「只有整体半透明」，不报错。
        self._apply_bg_mode()

        self._build_shell()
        self.collapsed = True         # 启动先收成细条，检测到对局再展开
        self._ai_text = ""
        self._ai_pick: tuple[str, str] | None = None
        self._ai_route: list[dict] | None = None
        self._pick_source = "AI"
        self._hint_busy = False
        self._hint_result: tuple | None = None          # 这条建议是 AI 给的还是引擎给的
        # 工作线程只写这个字段，主线程在 _tick 里取走 —— Tk 不是线程安全的，
        # 从子线程直接调 root.after 有可能静默失败，导致建议永远不出现
        self._ai_result: tuple | None = None
        self._case_buf: list[dict] = []          # 本局的决策案例，结束时一次性入库
        self._cases_flushed = False
        self._ai_reviewed = False
        self._ai_mulligan: list[dict] | None = None
        self._ai_mulligan_done = False
        self._ai_conv = ai_mod.Conversation(ai_mod.ROUTE_SYSTEM, keep=ai_mod.KEEP_TURNS)
        self._last_review = self._load_last_review()
        self._rule_report: list[str] = []
        self._ai_lessons: list[str] = []
        self._effect_done: set[str] = set()      # 已处理过的牌名
        self._effect_report: list[str] = []      # 本局的规则补充结果
        self._effect_busy = False
        self._effect_result: list[str] | None = None
        self._ai_busy = False
        self._ai_turn: tuple[int, bool] | None = None
        self._fail_streak = 0         # 连续渲染失败次数
        self._render_broken = False   # 是否已切到错误提示
        self._render_collapsed()      # 必须有这一步：panel 才会真正 pack 上去
        self._show_placeholder()
        self._apply_geometry()
        # 有上一局记录就直接展示复盘（等待对局时用）
        if getattr(self, "_last_review", None):
            self.collapsed = False
            self._render_last_review()
        self.root.deiconify()
        self.root.after(50, self._tick)

    # ------------------------------------------------------------ 骨架
    def _build_shell(self) -> None:
        self.container = tk.Frame(self.root, bg=BG)
        self.container.pack(fill="both", expand=True)

        # 细条（不在对局时）—— 细条上也要有关闭，否则缩起来就关不掉了
        self.bar = tk.Frame(self.container, bg=PANEL, height=COLLAPSED_H)
        # 顺序很重要：先 pack 右侧控件（关闭、提示），最后 pack 会扩展的文字，
        # 否则 expand=True 的文字会把右侧控件挤成 0 宽 —— 关闭按钮就“消失”了
        self.bar_close, self.bar_close_label = self._make_close(self.bar, side="left")
        self.bar_hint = tk.Label(self.bar, text="F8", bg=PANEL, fg="#5b636e", font=(FONT, 8), padx=8)
        self.bar_hint.pack(side="right")
        self.bar_label = tk.Label(self.bar, text="等待对局", bg=PANEL, fg=MUTED,
                                  font=(FONT, 9), anchor="w", padx=8)
        self.bar_label.pack(side="left", fill="both", expand=True)

        # 完整面板
        self.panel = tk.Frame(self.container, bg=BG)

        self.header = tk.Frame(self.panel, bg=PANEL, height=30)
        self.header.pack(fill="x")
        # pack 顺序 = 占位顺序：先放左上的关闭按钮，再放贴右的来源，最后才是
        # 会 expand 的标题（它必须最后 pack，否则会把别的控件挤没）
        self.close_btn, self.close_btn_label = self._make_close(self.header, side="left")
        self.source_label = tk.Label(self.header, text="", bg=PANEL, fg="#5b636e", font=(FONT, 8), padx=6)
        self.source_label.pack(side="right")
        self.header_label = tk.Label(self.header, text="", bg=PANEL, fg=TEXT, font=(FONT, 10, "bold"),
                                     anchor="w", padx=8)
        self.header_label.pack(side="left", fill="both", expand=True)

        self.body = tk.Frame(self.panel, bg=BG, padx=10, pady=8)
        self.body.pack(fill="both", expand=True)


        self.root.bind("<Button-3>", self._popup_menu)
        self.container.bind("<Button-3>", self._popup_menu)
        self._build_menu()
        self._bind_drag_everywhere()


    def _make_close(self, parent: tk.Widget, side: str = "right") -> tuple[tk.Frame, tk.Label]:
        """做一个「一看就是关闭按钮」的方块：深红底 + 白叉，悬停变亮。

        之前用的是裸的 × 字符，跟深色背景几乎融在一起 —— 用户找不到。
        """
        frame = tk.Frame(parent, bg="#5a1f24", cursor="hand2")
        pad = (6, 0) if side == "left" else (0, 6)
        frame.pack(side=side, padx=pad, pady=5)
        label = tk.Label(frame, text="\u00d7", bg="#5a1f24", fg="#ffd7d3",
                         font=(FONT, 10, "bold"), width=2, height=1, cursor="hand2")
        label.pack(padx=1, pady=0)

        def on_enter(_e=None):
            frame.configure(bg="#8b2b31")
            label.configure(bg="#8b2b31", fg="#ffffff")

        def on_leave(_e=None):
            frame.configure(bg="#5a1f24")
            label.configure(bg="#5a1f24", fg="#ffd7d3")

        for w in (frame, label):
            w.bind("<Button-1>", lambda _e: self.quit())
            w.bind("<Enter>", on_enter)
            w.bind("<Leave>", on_leave)
        return frame, label

    def _apply_bg_mode(self) -> None:
        """切换背景：solid = 整窗半透明；key = 把 BG 抠成透明（缝隙能看见游戏）。"""
        try:
            if self.bg_mode == "key":
                self.root.attributes("-transparentcolor", BG)
            else:
                self.root.attributes("-transparentcolor", "")
        except tk.TclError:
            self.bg_mode = "solid"

    def _toggle_bg_mode(self) -> None:
        self.bg_mode = "key" if self.bg_mode == "solid" else "solid"
        self._apply_bg_mode()
        self._persist()

    def _build_menu(self) -> None:
        self.menu = tk.Menu(self.root, tearoff=0, bg=PANEL, fg=TEXT,
                            activebackground=BLUE, activeforeground="#ffffff",
                            font=(FONT, 9), bd=0)
        self.menu.add_command(label="透明度 +", command=lambda: self._bump_alpha(+0.05))
        self.menu.add_command(label="透明度 −", command=lambda: self._bump_alpha(-0.05))
        self.menu.add_separator()
        self.menu.add_command(label="字号 +", command=lambda: self._bump_font(+1))
        self.menu.add_command(label="字号 −", command=lambda: self._bump_font(-1))
        self.menu.add_separator()
        self.menu.add_command(label="收起 / 展开", command=self._toggle_collapse)
        self.menu.add_checkbutton(label="缝隙透出游戏", command=self._toggle_bg_mode,
                                  variable=tk.BooleanVar(value=self.bg_mode == "key"))
        self.menu.add_command(label="退出", command=self.quit)

    # ------------------------------------------------------------ 拖动
    # 只绑顶栏是不够的：用户会直接按住面板正文拖，那样没反应。
    # 所以用 bind_all 接管整个窗口的左键拖动，命中「关闭按钮」时放行。
    def _bind_drag(self, widget: tk.Widget) -> None:
        widget.bind("<ButtonPress-1>", self._drag_start)
        widget.bind("<B1-Motion>", self._drag_move)
        widget.bind("<ButtonRelease-1>", self._drag_end)

    def _bind_drag_everywhere(self) -> None:
        self.root.bind_all("<ButtonPress-1>", self._drag_start, add="+")
        self.root.bind_all("<B1-Motion>", self._drag_move, add="+")
        self.root.bind_all("<ButtonRelease-1>", self._drag_end, add="+")

    def _is_interactive(self, widget: tk.Widget | None) -> bool:
        """这些控件要保留自己的点击行为，不能被拖动吃掉。"""
        return widget in (self.close_btn, self.close_btn_label,
                          self.bar_close, self.bar_close_label, self.menu)

    def _drag_start(self, event: tk.Event) -> None:
        if self._is_interactive(getattr(event, "widget", None)):
            return
        self._dragging = True
        try:
            self._drag_dx = event.x_root - self.root.winfo_x()
            self._drag_dy = event.y_root - self.root.winfo_y()
        except tk.TclError:
            self._dragging = False
            self._drag_dx = self._drag_dy = 0

    def _drag_move(self, event: tk.Event) -> None:
        if not self._dragging:
            return
        try:
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            x = max(-self.width + 60, min(event.x_root - self._drag_dx, sw - 60))
            y = max(-10, min(event.y_root - self._drag_dy, sh - COLLAPSED_H))
            self.pos = (x, y)          # 拖动过程中同步位置，避免下一次刷新把窗口弹回去
            self._apply_geometry()
        except tk.TclError:
            self._dragging = False

    def _drag_end(self, _event: tk.Event) -> None:
        if self._dragging:
            self._dragging = False
            try:
                self.pos = (self.root.winfo_x(), self.root.winfo_y())
            except tk.TclError:
                pass
            self._persist()

    # ------------------------------------------------------------ 设置
    def _bump_alpha(self, delta: float) -> None:
        self.alpha = round(max(0.35, min(1.0, self.alpha + delta)), 2)
        self.root.attributes("-alpha", self.alpha)
        self._persist()

    def _bump_font(self, delta: int) -> None:
        self.font_size = max(8, min(14, self.font_size + delta))
        self._last_sig = None
        self._persist()

    def _toggle_collapse(self) -> None:
        self.collapsed = not self.collapsed
        self._render_collapsed()
        self._persist()

    def _persist(self) -> None:
        _save_state({
            "x": self.pos[0], "y": self.pos[1], "w": self.width, "layout": LAYOUT_VERSION,
            "alpha": self.alpha, "font_size": self.font_size, "collapsed": self.collapsed,
            "bg_mode": self.bg_mode,
        })

    def _popup_menu(self, event: tk.Event) -> None:
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    # ------------------------------------------------------------ 主循环
    def _tick(self) -> None:
        if self._in_fit:            # 正在量尺寸，这一拍跳过，避免递归
            self.root.after(320, self._tick)
            return
        try:
            self._check_hotkey()
            # 游戏有时会把自己顶到最前，定期重申一次置顶（很便宜）
            self._topmost_ticks = getattr(self, "_topmost_ticks", 0) + 1
            if self._topmost_ticks % 6 == 0:
                try:
                    self.root.attributes("-topmost", True)
                except tk.TclError:
                    pass
            state, switched = self.manager.poll()
            if state is not None or switched:
                self._update(state or GameState(self.cards))
            elif getattr(self, "_last_state", None) is not None and (
                time.time() - getattr(self, "_last_render", 0) > 1.0
            ):
                # 日志停了也要继续走：否则「对局结束 8 秒后收起」这类
                # 靠时间推进的逻辑永远不会触发，面板就停在「对局中」
                self._update(self._last_state)
            self._drain_ai_results()
            self._fail_streak = 0
            if self._render_broken:
                # 下一帧画成功了就恢复正常
                self._render_broken = False
                self.bar_label.config(fg=MUTED)
                self._last_sig = None
        except Exception as exc:  # 单帧出错不能让窗口崩掉
            import traceback as _tb
            self._log(f"tick error: {exc}\n" + _tb.format_exc(limit=15))
            self._fail_streak = getattr(self, "_fail_streak", 0) + 1
            # 连续失败说明画面已经冻住了：与其让你以为在打，不如直接说坏了
            self._last_error = str(exc)
            if self._fail_streak >= 3 and not getattr(self, "_render_broken", False):
                self._render_broken = True
                self._show_render_error()
        self.root.after(320, self._tick)

    def _drain_ai_results(self) -> None:
        """把工作线程算好的结果搬到界面上（只在主线程做）。"""

        if self._effect_result is not None:
            self._effect_report.extend(self._effect_result)
            self._effect_result = None

        res = self._ai_result
        if res is not None:
            self._ai_result = None
            text, pick, steps = res
            self._ai_done(text, pick, steps)
        rows = getattr(self, "_ai_mulligan_rows", None)
        if rows is not None:
            self._ai_mulligan_rows = None
            self._ai_mulligan_done_cb(rows)

    def _show_render_error(self) -> None:
        """画面卡死时把细条变成错误提示，并按 F8 可恢复。"""
        try:
            self.bar_label.config(text="渲染出错，按 F8 重载", fg=RED)
            if not self.collapsed:
                if getattr(self, "_last_review", None):
                    self.collapsed = False
                    self._render_last_review()
                else:
                    self.collapsed = True
                    self._render_collapsed()
            self.bar_label.config(text="渲染出错，按 F8 重载", fg=RED)
            self._log("连续渲染失败，已切到错误提示")
        except Exception:
            pass

    def _check_hotkey(self) -> None:
        try:
            pressed = bool(ctypes.windll.user32.GetAsyncKeyState(0x77) & 0x8000)  # VK_F8
        except Exception:
            return
        if pressed and not self._f8_down:
            self._f8_down = True
            if getattr(self, "_render_broken", False):
                # F8 当作「重载」：清掉失败计数，强制下一帧重画
                self._render_broken = False
                self._fail_streak = 0
                self._last_sig = None
                self.bar_label.config(text="等待对局", fg=MUTED)
            elif self.root.state() == "withdrawn":
                self.root.deiconify()
                self.root.attributes("-topmost", True)
            else:
                self.root.withdraw()
        elif not pressed:
            self._f8_down = False

    def _update(self, st: GameState) -> None:
        self._last_state = st
        self._last_review_shown = False

        self._last_render = time.time()
        snap = self.advisor.snapshot(st)
        snap["demo"] = self.manager.is_demo
        snap["source"] = self.manager.name
        if snap.get("phase") == "mulligan" and (snap.get("turn") or 0) <= 1:
            model_mod.clear_overrides()          # 新对局清掉上一局的临时修正
            self._effect_done.clear()
            self._effect_report = []
            self._rule_report = []
            self._case_buf = []
            self._cases_flushed = False
            self._ai_conv = ai_mod.Conversation(ai_mod.ROUTE_SYSTEM, keep=ai_mod.KEEP_TURNS)

        phase = snap.get("phase")
        if phase == "game_over":
            self._game_over_since = self._game_over_since or time.time()
        else:
            self._game_over_since = None

        idle = phase in (None, "idle") or (
            phase == "game_over" and self._game_over_since and time.time() - self._game_over_since > 8
        )
        if idle:
            if self._last_review:
                if not self._last_review_shown:
                    self._last_review_shown = True
                    self._render_last_review()
                return
            if not self.collapsed:
                self.collapsed = True
                self._render_collapsed()
            return
        if not idle and self.collapsed:
            self.collapsed = False
            self._render_collapsed()      # 必须重排：否则细条不下架、面板不 pack
            self._last_sig = None
        if self.collapsed:
            self.bar_label.config(text="等待对局" if idle else "对局中")
            return

        sig = self._signature(snap)
        if sig == self._last_sig:
            return
        self._last_sig = sig
        self._render(snap)

    def _signature(self, snap: dict[str, Any]) -> tuple:
        pred = snap.get("prediction") or {}
        adv = snap.get("advice") or {}
        mul = snap.get("mulligan") or {}
        return (
            self.font_size,
            snap.get("phase"), snap.get("turn"), snap.get("is_our_turn"),
            pred.get("name"), round(float(pred.get("confidence") or 0), 2),
            round(float(pred.get("aoe_risk") or 0), 2),
            round(float(pred.get("removal_risk") or 0), 2),
            tuple(c.get("name") for c in (pred.get("likely_hand") or [])[:2]),
            tuple((c.get("name"), round(float(c.get("confidence") or 0), 2))
                  for c in (pred.get("candidates") or [])[:3]),
            tuple(pred.get("evidence") or [])[:2],
            tuple(line.get("title") for line in (adv.get("lines") or [])),
            adv.get("lethal"),
            snap.get("warning"),
            tuple(r.get("name") for r in (mul.get("advice") or [])),
            (snap.get("opp") or {}).get("class_cn"),
            tuple(snap.get("review") or ()),
            (snap.get("our") or {}).get("mana"),
            tuple(h.get("id") for h in ((snap.get("our") or {}).get("hand") or [])),
            tuple((f.get("name"), f.get("summary")) for f in (snap.get("feed") or [])),
            tuple(m.get("id") for m in ((snap.get("our") or {}).get("board") or [])),
            len(((snap.get("opp") or {}).get("board") or [])),
            (snap.get("opp") or {}).get("health"),
            (snap.get("deck") or {}).get("left"),
            tuple(c.get("name") for c in ((snap.get("deck") or {}).get("key_cards") or [])),
        )

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

    def _load_last_review(self) -> dict | None:
        try:
            path = DATA_DIR / "last_review.json"
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        except (OSError, ValueError):
            return None

    def _render_last_review(self) -> None:
        """等待对局时展示上一局的复盘。"""
        for child in self.body.winfo_children():
            child.destroy()
        rev = self._last_review or {}
        inner = self._section("上一局", "复盘")
        cn = lambda x: cards_mod.CLASS_CN.get(str(x or ""), str(x or "?"))
        parts = [f"{cn(rev.get('our_class'))} vs {cn(rev.get('opp_class'))}"]
        if rev.get("result"):
            parts.append("胜" if rev["result"] == "win" else "负")
        if rev.get("turns"):
            parts.append(f"{rev['turns']} 回合")
        head = " · ".join(parts)
        tk.Label(inner, text=head, bg=PANEL, fg=TEXT,
                 font=(FONT, self.font_size, "bold"), anchor="w", justify="left",
                 wraplength=self.width - 46).pack(fill="x", pady=(0, 3))
        rows = [r for r in (list(rev.get("findings") or [])
                            + list(rev.get("issues") or [])
                            + list(getattr(self, "_rule_report", []) or []))
                if str(r).strip() and "未建模" not in str(r)]
        if not rows:
            rows = ["上一局没有可复盘的记录"]
        for line in rows[:5]:
            tk.Label(inner, text=str(line), bg=PANEL, fg=TEXT_SOFT,
                     font=(FONT, self.font_size - 1), anchor="w", justify="left",
                     wraplength=self.width - 46).pack(fill="x", pady=1)
        lessons = [str(x) for x in (getattr(self, "_ai_lessons", []) or []) if str(x).strip()]
        if lessons:
            tk.Label(inner, text="AI 复盘", bg=PANEL, fg=GOLD,
                     font=(FONT, self.font_size - 2, "bold"), anchor="w").pack(fill="x", pady=(5, 1))
            for line in lessons[:3]:
                tk.Label(inner, text=line, bg=PANEL, fg=TEXT_SOFT,
                         font=(FONT, self.font_size - 2), anchor="w", justify="left",
                         wraplength=self.width - 46).pack(fill="x")

        tk.Label(inner, text="开一局后这里会自动切换到对局信息", bg=PANEL, fg="#5b636e",
                 font=(FONT, self.font_size - 3), anchor="w").pack(fill="x", pady=(6, 0))
        self.collapsed = False
        self._render_collapsed()      # 必须调用：它负责把 panel 真正 pack 上去

    # ------------------------------------------------------------ 是否值得问 AI
    @staticmethod
    def _has_options(snap: dict[str, Any]) -> bool:
        our = snap.get("our") or {}
        mana = our.get("mana") or 0
        if any((c.get("cost") or 0) <= mana for c in (our.get("hand") or [])):
            return True
        if any(m.get("can_attack") for m in (our.get("board") or [])):
            return True
        hp = our.get("hero_power") or {}
        if not our.get("hero_power_used") and (hp.get("cost") or 0) <= mana:
            return True
        return False

    def _maybe_ai(self, snap: dict[str, Any], st: GameState | None = None) -> None:
        """每回合最多请求一次 AI 解释，不跟着 320ms 的刷新跑。"""
        if not ai_mod.is_ready():
            return
        if snap.get("phase") == "mulligan":
            # 留牌交给引擎（mulligan.py 的权重表），不再调用 AI
            return
        if snap.get("phase") == "game_over":
            # 这一局的复盘已经写入文件，重新读一遍，
            # 否则等待界面会一直显示程序启动时读到的那一份
            self._last_review = self._load_last_review()
            self._last_review_shown = False
            if not self._ai_reviewed:
                self._ai_reviewed = True
                threading.Thread(target=self._ai_review_worker, args=(snap,),
                                 daemon=True).start()
            return
        # 只在我方回合请求：对手回合的场面会被他自己的出牌改掉，
        # 提前算出来的建议到你这回合就过期了，白白消耗 token。
        if not (snap.get("turn_known") and snap.get("is_our_turn")):
            return
        choice = snap.get("choice") or {}
        choice_sig = tuple(str(o.get("name")) for o in (choice.get("options") or []))
        # 局面签名：手牌/场面/法力一变（你打了一张牌、换了一只怪、用了一张法术），
        # 就当作「下一步该干什么」重新问一次，而不是整回合只算一次
        our = snap.get("our") or {}
        board_sig = (
            our.get("mana"),
            tuple(sorted(str(c.get("id") or c.get("name")) for c in (our.get("hand") or []))),
            tuple(sorted(str(m.get("id") or m.get("name")) for m in (our.get("board") or []))),
            our.get("health"),
        )
        # 没费、没牌可出、随从都打过 —— 这回合只能结束，问 AI 纯属浪费 token
        if not choice_sig and not self._has_options(snap):
            return
        if choice_sig and st is not None:
            # 发现/抉择完全由引擎打分决定，不调用 AI
            try:
                board_now = board_from_state(st)
                auto = getattr(self.advisor, "_auto_deck", None)
                deck_cards = None
                if auto is not None and getattr(auto, "cards", None):
                    deck_cards = [self.cards.get(cid) for cid in auto.cards]
                idx, why, gap = policy_mod.pick_choice(
                    choice.get("options") or [], board_now, self.cards, 0,
                    board_now.sides[0].mana, deck_cards=deck_cards,
                    tripled=bool((snap.get("cthun") or {}).get("tripled")))
                self._ai_pick = (str((choice.get("options") or [{}])[idx].get("name")), why)
                self._pick_source = "引擎"
                self._ai_route = None
                self._ai_text = ""
                self._ai_turn = (int(snap.get("turn") or 0), True, choice_sig, None)
                return
            except Exception:
                pass

        # 逐回合的 AI 路线默认关闭：一局要调 20 多次，性价比低。
        # 想开就在 data/ai.json 里设 turn_advice=true
        if not choice_sig and not ai_mod.load_config().get("turn_advice"):
            return
        key = (int(snap.get("turn") or 0), True, choice_sig, board_sig)
        if key == self._ai_turn:
            return
        self._ai_turn = key
        self._ai_text = ""
        self._ai_busy = True
        # 等 1.5 秒再请求：刚抽到的牌要过一会儿才进日志，
        # 立刻发出去会让 AI 基于不完整的手牌做决策
        # 发现/抉择的选项一出现就是完整的，不需要等日志稳定；
        # 用更短的延迟，避免「选项都弹出来了还没有建议」
        delay = 300 if choice_sig else 1500
        self.root.after(delay, lambda: self._ai_fire(key, snap, st))

    def _ai_mulligan_done_cb(self, rows: list[dict]) -> None:
        self._ai_mulligan = rows or None
        self._last_sig = None

    def _ai_fire(self, key: tuple, snap: dict[str, Any],
                 st: GameState | None = None) -> None:
        if self._ai_turn != key:
            return          # 期间回合已经变了，这次请求作废
        threading.Thread(target=self._ai_worker, args=(snap, st), daemon=True).start()

    def _ai_worker(self, snap: dict[str, Any], st: GameState | None = None) -> None:
        pick = None
        text = ""
        # 先要决策（它才影响你怎么打），解释随后补 —— 不要串行等两倍时间
        try:
            # 能斩杀时不问 AI：斩杀线路是确定性的，不能让模型推翻
            choice = snap.get("choice") or {}
            options = [str(o.get("name")) for o in (choice.get("options") or [])]
            actions: list[str] = []
            if len(options) >= 2:
                # 发现/抉择期间只问「选哪个」，不出整回合路线 ——
                # 选项只能选一个，列成多步会让人以为要全打出去
                got = ai_mod.decide_and_explain(snap, options)
                if got is not None:
                    pick = (options[got["pick"]], got["reason"])
                    self._ai_result = (got["explain"], pick, None)
                return
            elif st is not None and not (snap.get("advice") or {}).get("lethal"):
                board = board_from_state(st)
                actions = [a.desc for a in policy_mod.legal_actions(board, 0, self.cards)]
            else:
                actions = []
            if actions:
                # 要一整回合的路线（含站位），不是单步
                route = ai_mod.plan_route(snap, actions, conv=self._ai_conv)
                if route is not None:
                    text = route["explain"]
                    steps = route["steps"]
                    # 按顺序模拟一遍：费用不够的步骤要标出来，
                    # 否则会出现「3+3+3 费但只有 5 费」这种排不下来的路线
                    if st is not None:
                        try:
                            board_now = board_from_state(st)
                            checked = policy_mod.validate_sequence(
                                board_now, [x["text"] for x in steps], self.cards)
                            for step, chk in zip(steps, checked):
                                if not chk["ok"]:
                                    step["matched"] = False
                                    step["why"] = chk["why"]
                        except Exception:
                            pass
                    pick = (steps[0]["text"], route["reason"]) if steps else None
                    self._ai_result = (text, pick, steps)
                    return
                # 路线解析失败就退回「单步建议」，别让面板什么都不显示
                self._log("plan_route 返回空，退回单步决策")
                got = ai_mod.decide_and_explain(snap, actions)
                if got is not None:
                    pick = (actions[got["pick"]], got["reason"])
                    self._ai_result = (got["explain"], pick, None)
                else:
                    self._log("decide_and_explain 也失败，本次无 AI 建议")
                    self._ai_result = ("", None, None)
                return
        except Exception:
            pass
        text = ""
        self._ai_result = (text, pick, None)

    def _maybe_effects(self, snap: dict[str, Any]) -> None:
        """发现新的未建模牌就立刻补规则，不等对局结束。"""
        names = [str(n) for n in (snap.get("unmodelled") or [])
                 if str(n) and str(n) not in self._effect_done]
        if not names or self._effect_busy:
            return
        self._effect_busy = True
        batch = names[:6]
        # 不在这里标记「已处理」——失败要能重试
        threading.Thread(target=self._effects_worker,
                         args=(batch,), daemon=True).start()

    def _effects_worker(self, names: list[str]) -> None:
        try:
            self._effect_result = self._auto_effects({"unmodelled": names})
            # 只有真正跑完（无论通过/被拒）才算处理过；抛异常则下一帧重试
            self._effect_done.update(names)
        except Exception as exc:
            self._log(f"effects worker failed: {exc}")
            self._effect_result = []
        self._effect_busy = False

    def _auto_effects(self, snap: dict[str, Any]) -> list[str]:
        """对局结束：把本局未建模的牌交给 AI 补规则，验证通过才入库。"""
        names = list(snap.get("unmodelled") or [])[:4]
        if not names:
            return []
        items = []
        for name in names:
            card = next((c for c in self.cards.by_id.values() if c.name == name), None)
            if card is not None:
                items.append({"name": card.name, "type": card.type, "text": card.text or ""})
        if not items:
            return []
        rules = ai_mod.effects_rules(items)
        report: list[str] = []
        for rule in rules:
            targets = [cid for cid, c in self.cards.by_id.items() if c.name == rule.get("name")]
            if not targets:
                continue
            ok, why = effects_ai.validate_rule(self.cards, rule, targets)
            if ok and effects_ai.commit_rule(rule):
                report.append(f"已自动补规则：{rule['name']}（{why}）")
            else:
                detail = f"规则被拒：{rule['name']} — {why}"
                self._log(detail)          # 写进 data/hsai.log，方便事后查
                report.append(detail[:80])
        return report[:3]

    def _ai_review_worker(self, snap: dict[str, Any]) -> None:
        """对局结束：让 AI 总结教训，写进本地 lessons.jsonl（不进仓库）。"""
        try:
            our = (snap.get("our") or {}).get("class_cn") or "?"
            opp = (snap.get("opp") or {}).get("class_cn") or "?"
            deck = (snap.get("deck") or {}).get("name") or "未知"
            arch = (snap.get("prediction") or {}).get("name") or "未知"
            findings = "；".join(snap.get("review") or []) or "无机器检测到的失误"
            issues = "；".join(snap.get("issues") or []) or "无"
            turns = len(getattr(self.advisor, "_game_turns", []) or [])
            summary = (
                f"对局：我方 {our}（卡组 {deck}）vs 对手 {opp}（预判 {arch}），"
                f"共 {snap.get('turn')} 回合，记录了 {turns} 个我方回合。"
                f"机器复盘：{findings}。"
                f"引擎自身问题：{issues}。"
                "引擎这局按「抢血/解场」两种模式决策：能更快打死对方就打脸，"
                "否则只做不亏的交换。请只针对「打法选择」给建议，不要编造伤害数字。"
            )
            missed = any("有斩杀没斩" in str(x) for x in (snap.get("review") or []))
            lessons = ai_mod.review(snap, summary, missed_lethal=missed)
            self._rule_report = list(self._effect_report)
            self._ai_lessons = list(lessons or [])
            if lessons:
                memory.add_lessons(lessons, {
                    "our_class": str((snap.get("our") or {}).get("class_en") or ""),
                    "opp_class": str((snap.get("opp") or {}).get("class_en") or ""),
                    "archetype": str((snap.get("prediction") or {}).get("name") or ""),
                    "result": "win" if (snap.get("review") and False) else "",
                })
        except Exception:
            pass

    def _ai_done(self, text: str, pick: tuple[str, str] | None = None,
                 steps: list[dict] | None = None) -> None:
        self._ai_busy = False
        self._ai_text = text
        self._ai_route = steps
        if steps:
            self._case_buf.append({
                "turn": self._last_snap.get("turn") if getattr(self, "_last_snap", None) else 0,
                "mana": ((self._last_snap or {}).get("our") or {}).get("mana"),
                "our_board_n": len(((self._last_snap or {}).get("our") or {}).get("board") or []),
                "action": steps[0].get("text") or "",
            })
        if pick is not None:
            self._ai_pick = pick
        self._last_sig = None

    def _log_advice(self, snap: dict[str, Any]) -> None:
            """把每一条给出去的建记录到 data/advice_log.jsonl，方便事后对账。

            只在我们回合、且有建议时记，避免刷屏。
            """
            try:
                lines = (snap.get("advice") or {}).get("lines") or []
                if not lines:
                    return
                our = snap.get("our") or {}
                opp = snap.get("opp") or {}
                rec = {
                    "ts": datetime.now().strftime("%H:%M:%S"),
                    "turn": snap.get("turn"),
                    "mana": our.get("mana"),
                    "hand": [f"{c.get('name')}({c.get('cost')})" for c in (our.get("hand") or [])],
                    "board": [f"{m.get('name')} {m.get('atk')}/{m.get('health')}" for m in (our.get("board") or [])],
                    "foe_board": [f"{m.get('name')} {m.get('atk')}/{m.get('health')}" for m in (opp.get("board") or [])],
                    "advice": [l.get("title") for l in lines],
                    "reason": [l.get("reason") for l in lines],
                }
                path = DATA_DIR / "advice_log.jsonl"
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                # 超过 400 行就只留最近 400
                rows = path.read_text(encoding="utf-8").splitlines()
                if len(rows) > 400:
                    path.write_text("\n".join(rows[-400:]) + "\n", encoding="utf-8")
            except Exception as exc:
                self._log(f"advice log failed: {exc}")

    def _render(self, snap: dict[str, Any]) -> None:

        # 局面一变：作废旧建议，并把这条建议记进 advice_log.jsonl
        sig = (
            snap.get("turn"),
            ((snap.get("our") or {}).get("mana")),
            tuple(str(c.get("id") or c.get("name")) for c in ((snap.get("our") or {}).get("hand") or [])),
            tuple(str(m.get("id") or m.get("name")) for m in ((snap.get("our") or {}).get("board") or [])),
            tuple(str(o.get("name")) for o in ((snap.get("choice") or {}).get("options") or [])),
        )
        if sig != getattr(self, "_advice_sig", None):
            self._advice_sig = sig
            self._log_advice(snap)
            self._ai_pick = None
            self._ai_route = None
            self._ai_text = ""

        self._maybe_effects(snap)
        self._drain_ai_results()
        self._maybe_ai(snap, getattr(self, "_last_state", None))
        for child in self.body.winfo_children():
            child.destroy()

        phase = snap.get("phase")
        turn = snap.get("turn") or 0
        ours = snap.get("is_our_turn")
        opp = (snap.get("opp") or {}).get("class_cn") or "?"
        phase_text = {"mulligan": "留牌", "playing": "对局中", "game_over": "已结束"}.get(phase, "待机")
        if not snap.get("turn_known", True):
            owner = "回合判断中"
        else:
            owner = "我方回合" if ours else "对方回合"
        self.header_label.config(
            text=f"T{turn} · {owner} · vs {opp}    {phase_text}" if turn else f"vs {opp}    {phase_text}"
        )
        self.source_label.config(text="示例" if snap.get("demo") else "", fg=YELLOW if snap.get("demo") else MUTED)
        self.bar_label.config(text="等待对局")

        # 顶部「我方卡组 / 关键牌」条已按需求移除
        # 只在「我方回合且对局进行中」展示 AI 决策；否则上一回合的建议会一直挂着，
        # 看起来像“现在该这么打”，而实际上已经轮不到你了。
        if snap.get("phase") != "playing" or not snap.get("is_our_turn"):
            if self._ai_pick or self._ai_text or self._ai_route:
                self._ai_pick = None
                self._ai_text = ""
                self._ai_route = None
                self._ai_busy = False

        choice = snap.get("choice") or {}
        options = choice.get("options") or []
        if len(options) >= 2:
            inner_c = self._section("待你选择", "发现" if choice.get("type") == "GENERAL" else str(choice.get("type")))
            for i, opt in enumerate(options[:4], 1):
                row = tk.Frame(inner_c, bg=PANEL)
                row.pack(fill="x", pady=1)
                chosen = bool(self._ai_pick) and self._ai_pick[0] == str(opt.get("name"))
                tk.Label(row, text=str(i), bg="#21262d", fg=GOLD if chosen else MUTED,
                         font=(FONT, self.font_size - 3, "bold"), width=2).pack(side="left")
                tk.Label(row, text=str(opt.get("name")), bg=PANEL, fg=GOLD if chosen else TEXT,
                         font=(FONT, self.font_size - 1, "bold" if chosen else "normal"),
                         anchor="w").pack(side="left")
                if chosen:
                    tk.Label(row, text="← AI", bg=PANEL, fg=GOLD,
                             font=(FONT, self.font_size - 3)).pack(side="right")
            if self._ai_pick:
                tk.Label(inner_c, text=self._ai_pick[1], bg=PANEL, fg=MUTED,
                         font=(FONT, self.font_size - 2), anchor="w", justify="left",
                         wraplength=self.width - 46).pack(fill="x", pady=(2, 0))

        cthun = snap.get("cthun") or {}
        if cthun.get("count"):
            mark = "（强化已解锁 ×3）" if cthun.get("tripled") else ""
            tk.Label(self.body, text=f"克苏恩技能 {cthun.get('count')}/{cthun.get('total')}{mark}",
                     bg=BG, fg=GOLD if cthun.get("tripled") else MUTED,
                     font=(FONT, self.font_size - 3), anchor="w").pack(fill="x")

        self._render_prediction(snap.get("prediction") or {})

        # 对战信息：对手在上，双方血量/手牌/牌库/场攻
        def _row(side, label):
            hp = side.get("health")
            armor = side.get("armor") or 0
            live = f"{hp + armor} 血"
            if armor:
                live += f"（{hp}+{armor}甲）"
            board = [m for m in (side.get("board") or []) if m.get("atk")]
            atk = sum(int(m.get("atk") or 0) for m in board)
            weapon = (side.get("weapon") or {}).get("atk") or 0
            return (f"{label} {live} · 手牌 {side.get('hand_count')} · "
                    f"牌库 {side.get('deck_count')} · 场攻 {atk + weapon}")

        inner_b = self._section("对战信息")
        for side, label in ((snap.get("opp") or {}, "对手"), (snap.get("our") or {}, "我方")):
            tk.Label(inner_b, text=_row(side, label), bg=PANEL,
                     fg=TEXT if label == "对手" else TEXT_SOFT,
                     font=(FONT, self.font_size - 1), anchor="w", justify="left",
                     wraplength=self.width - 46).pack(fill="x", pady=1)
        if snap.get("phase") != "mulligan":
            self._ai_mulligan = None      # 留牌阶段结束，这块不再显示

        if self._ai_mulligan:
            inner_m = self._section("AI 留牌", "模型建议")
            for row in self._ai_mulligan[:6]:
                color = "#3fb950" if row["keep"] else "#d29922"
                text = "留" if row["keep"] else "换"
                line = tk.Frame(inner_m, bg=PANEL)
                line.pack(fill="x", pady=1)
                tk.Label(line, text=text, bg=PANEL, fg=color,
                         font=(FONT, self.font_size - 2, "bold"), width=2).pack(side="left")
                tk.Label(line, text=str(row["name"]), bg=PANEL, fg=TEXT,
                         font=(FONT, self.font_size - 1), anchor="w").pack(side="left")
                tk.Label(line, text=row["reason"], bg=PANEL, fg=MUTED,
                         font=(FONT, self.font_size - 3), anchor="w",
                         wraplength=self.width - 110, justify="left").pack(side="left", padx=(4, 0))
        else:
            self._render_mulligan(snap.get("mulligan") or {})
        if self._ai_route:
            inner_r = self._section("AI 路线", "这回合怎么打")
            pos_cn = {"left": "最左", "right": "最右"}
            for i, step in enumerate(self._ai_route[:6], 1):
                row = tk.Frame(inner_r, bg=PANEL)
                row.pack(fill="x", pady=1)
                tk.Label(row, text=str(i), bg="#21262d", fg=GOLD,
                         font=(FONT, self.font_size - 3, "bold"), width=2).pack(side="left")
                label = step["text"]
                if step.get("pos"):
                    p = pos_cn.get(step["pos"], f"第{step['pos']}位")
                    label += f"（放{p}）"
                tk.Label(row, text=label, bg=PANEL,
                         fg=TEXT if step.get("matched") else "#c9a05a",
                         font=(FONT, self.font_size - 1), anchor="w", justify="left",
                         wraplength=self.width - 70).pack(side="left")
                if not step.get("matched"):
                    tip = step.get("why") or "需人工"
                    tk.Label(row, text=tip, bg=PANEL, fg="#c9a05a",
                             font=(FONT, self.font_size - 3)).pack(side="right")
        elif self._ai_pick:
            from_engine = self._pick_source != "AI"
            inner_pick = self._section(
                "引擎推荐" if from_engine else "AI 决策",
                "发现/抉择" if from_engine else "模型选择")
            tk.Label(inner_pick, text=self._ai_pick[0], bg=PANEL, fg=GOLD,
                     font=(FONT, self.font_size + 1, "bold"), anchor="w",
                     justify="left", wraplength=self.width - 46).pack(fill="x")
            tk.Label(inner_pick, text=self._ai_pick[1], bg=PANEL, fg=MUTED,
                     font=(FONT, self.font_size - 2), anchor="w", justify="left",
                     wraplength=self.width - 46).pack(fill="x")
        self._render_advice(snap.get("advice") or {})

        defense = (snap.get("advice") or {}).get("defense") or []
        # 只有「本回合建议」这张卡片真的画出来了，才往里加内容
        if defense and "inner" in locals():
            tk.Frame(inner, bg="#30363d", height=1).pack(fill="x", pady=(7, 4))
            tk.Label(inner, text="防对面的牌", bg=PANEL, fg="#8b949e",
                     font=(FONT, self.font_size - 3), anchor="w").pack(fill="x")
            for line in defense:
                tk.Label(inner, text=line, bg=PANEL, fg="#e3b341",
                         font=(FONT, self.font_size - 2), anchor="w", justify="left",
                         wraplength=self.width - 46).pack(fill="x", pady=1)

        warning = snap.get("warning") or ""
        if warning:
            self._strip(warning, fg="#e3b341", bg="#2d2614")
        self._request_fit()

    # --- 通用小部件
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
        tk.Label(head, text=title, bg=PANEL, fg=GOLD,
                 font=(FONT, self.font_size - 1, "bold"), anchor="w").pack(side="left")
        if kicker and kicker not in ("核心", "决策"):
            # 「核心」「决策」这类后缀没信息量，不显示
            tk.Label(head, text=kicker, bg=PANEL, fg="#5b636e",
                     font=(FONT, self.font_size - 3), anchor="e").pack(side="right")
        inner = tk.Frame(card, bg=PANEL)
        inner.pack(fill="x", padx=9, pady=(4, 7))
        return inner

    def _strip(self, text: str, *, fg: str, bg: str) -> None:
        bar = tk.Frame(self.body, bg=bg, highlightthickness=1, highlightbackground=bg)
        bar.pack(fill="x", pady=(6, 0))
        tk.Label(bar, text=text, bg=bg, fg=fg, font=(FONT, self.font_size - 1),
                 anchor="w", justify="left", wraplength=self.width - 40, padx=8, pady=5).pack(fill="x")

    def _render_deck(self, deck: dict[str, Any]) -> None:
        """我方卡组一行：剩几张、哪些关键牌还没抽到。"""
        if not deck or not deck.get("left"):
            return
        name = deck.get("name") or "我的卡组"
        key = "、".join(f"{c['name']}×{c['count']}" for c in (deck.get("key_cards") or [])[:2])
        tag = "（自动识别）" if deck.get("source") == "auto" else ""
        text = f"我方 {name}{tag} · 剩 {deck.get('left')} 张" + (f" · 关键牌 {key}" if key else "")
        tk.Label(self.body, text=text, bg=PANEL_2, fg=TEXT_SOFT,
                 font=(FONT, self.font_size - 2), anchor="w", justify="left",
                 wraplength=self.width - 30, padx=8, pady=4).pack(fill="x", pady=(4, 0))

    def _render_mine(self, our: dict[str, Any]) -> None:
        """我方手牌 + 场面：让用户能核对「工具看到的」和他「实际看到的」是否一致。"""
        hand = our.get("hand") or []
        if hand:
            inner = self._section("我的手牌", f"{len(hand)} 张")
            for c in hand:
                row = tk.Frame(inner, bg=PANEL)
                row.pack(fill="x", pady=1)
                tk.Label(row, text=str(c.get("cost") if c.get("cost") is not None else "?"),
                         bg="#21262d", fg=BLUE, font=(FONT, self.font_size - 3),
                         width=2).pack(side="left", padx=(0, 5))
                tk.Label(row, text=c.get("name") or "?", bg=PANEL, fg=TEXT,
                         font=(FONT, self.font_size - 1), anchor="w",
                         width=9 if self.width < 280 else 12).pack(side="left")
                text = (c.get("text") or "")
                if text and c.get("type_cn") != "随从":
                    tk.Label(row, text=text[:14], bg=PANEL, fg="#5b636e",
                             font=(FONT, self.font_size - 3), anchor="w").pack(side="left")

        board = [m for m in (our.get("board") or []) if m.get("atk") is not None]
        if board:
            inner = self._section("我的场面", f"{len(board)} 个")
            for m in board:
                row = tk.Frame(inner, bg=PANEL)
                row.pack(fill="x", pady=1)
                stat = f"{m.get('atk')}/{m.get('health')}"
                tk.Label(row, text=stat, bg=PANEL,
                         fg=GOLD if (m.get("atk") or 0) > (m.get("health") or 0) else TEXT_SOFT,
                         font=(FONT, self.font_size - 1, "bold"), width=5, anchor="w").pack(side="left")
                tk.Label(row, text=m.get("name") or "?", bg=PANEL, fg=TEXT,
                         font=(FONT, self.font_size - 1), anchor="w",
                         width=9 if self.width < 280 else 12).pack(side="left")
                if m.get("can_attack"):
                    tag, color = "可攻", GREEN
                elif m.get("exhausted"):
                    tag, color = "睡", "#5b636e"
                else:
                    tag, color = "—", "#5b636e"
                tk.Label(row, text=tag, bg=PANEL, fg=color,
                         font=(FONT, self.font_size - 3)).pack(side="right")

    # --- 预判
    def _render_prediction(self, pred: dict[str, Any]) -> None:
        if not pred:
            return
        kicker = "本地样本" if pred.get("source") == "learned" else "核心"
        inner = self._section("对手预判", kicker)
        name = pred.get("name") or "未知"
        conf = float(pred.get("confidence") or 0)
        unknown = name in ("未知卡组", "未知") or conf <= 0
        tone = MUTED if unknown else (GOLD if conf >= 0.6 else TEXT_SOFT)

        row = tk.Frame(inner, bg=PANEL)
        row.pack(fill="x")
        tk.Label(row, text=name if not unknown else "未识别（可能是新卡组）", bg=PANEL, fg=tone,
                 font=(FONT, self.font_size + 3, "bold"), anchor="w", justify="left",
                 wraplength=int(self.width * 0.62)).pack(side="left")
        if not unknown:
            tk.Label(row, text=f"{conf:.0%}", bg=PANEL, fg=tone,
                     font=(FONT, self.font_size + 1, "bold")).pack(side="right")

        if not unknown:
            bar = tk.Canvas(inner, height=4, bg="#21262d", highlightthickness=0, bd=0)
            bar.pack(fill="x", pady=(5, 0))
            width = self.width - 40
            bar.create_rectangle(0, 0, int(width * max(0.0, min(1.0, conf))), 4,
                                 fill=GOLD if conf >= 0.6 else BLUE, width=0)

        cands = pred.get("candidates") or []
        # 置信度低才展开候选：看得清的时候别占地方
        if len(cands) > 1 and conf < 0.60:
            tk.Label(inner, text="候选", bg=PANEL, fg="#5b636e",
                     font=(FONT, self.font_size - 3), anchor="w").pack(fill="x", pady=(6, 1))
            bar_w = max(40, self.width - 150)
            for i, c in enumerate(cands[:3]):
                row = tk.Frame(inner, bg=PANEL)
                row.pack(fill="x", pady=1)
                top1 = i == 0
                nm = str(c.get("name") or "?")
                if len(nm) > 10:
                    nm = nm[:9] + "…"
                tk.Label(row, text=nm, bg=PANEL,
                         fg=GOLD if top1 else MUTED,
                         font=(FONT, self.font_size - 2, "bold" if top1 else "normal"),
                         width=11 if self.width >= 280 else 7, anchor="w").pack(side="left")
                conf = float(c.get("confidence") or 0)
                cv = tk.Canvas(row, height=5, width=bar_w, bg="#21262d",
                               highlightthickness=0, bd=0)
                cv.pack(side="left", padx=4)
                cv.create_rectangle(0, 0, max(1, int(bar_w * conf)), 5,
                                    fill=GOLD if top1 else "#3d444d", width=0)
                tk.Label(row, text=f"{conf:.0%}", bg=PANEL,
                         fg=TEXT_SOFT if top1 else MUTED,
                         font=(FONT, self.font_size - 3)).pack(side="right")

        # 他这回合的威胁：一句话汇总 + 两张最可能的牌
        aoe = float(pred.get("aoe_risk") or 0)
        rem = float(pred.get("removal_risk") or 0)
        approx = "≈" if pred.get("estimated") else ""
        if aoe > 0.02 or rem > 0.02:
            risk = tk.Frame(inner, bg=PANEL)
            risk.pack(fill="x", pady=(6, 0))
            tk.Label(risk, text=f"{approx}清场 {aoe:.0%} · 单解 {rem:.0%}", bg=PANEL,
                     fg="#e3b341" if aoe >= 0.4 else TEXT_SOFT,
                     font=(FONT, self.font_size - 2, "bold"), anchor="w").pack(side="left")
        hand = pred.get("likely_hand") or []
        for i, c in enumerate(hand[:2]):
            label = "最可能" if i == 0 else "其次"
            cost = c.get("cost")
            cost_txt = f"（{cost} 费）" if cost is not None else ""
            imp = float(c.get("impact") or 1.0)
            tag = " ·清场" if imp >= 1.6 else (" ·单解" if imp >= 1.3 else "")
            tk.Label(inner, text=f"{label}：{c.get('name')}{cost_txt}{tag}",
                     bg=PANEL, fg=MUTED, font=(FONT, self.font_size - 2),
                     anchor="w", justify="left", wraplength=self.width - 46).pack(fill="x", pady=(3 if i == 0 else 1, 0))

    # --- 留牌
    def _render_mulligan(self, mul: dict[str, Any]) -> None:
        rows = mul.get("advice") or []
        if not rows:
            return
        inner = self._section("留牌建议", "起手")
        for r in rows:
            line = tk.Frame(inner, bg=PANEL)
            line.pack(fill="x", pady=1)
            keep = r.get("action") == "keep"
            tk.Label(line, text="留" if keep else "换", bg=PANEL, fg=GREEN if keep else RED,
                     font=(FONT, self.font_size - 1, "bold"), width=2, anchor="w").pack(side="left")
            tk.Label(line, text=f"{r.get('cost', '?')}", bg="#21262d", fg=BLUE, font=(FONT, self.font_size - 3),
                     width=2).pack(side="left", padx=(2, 5))
            tk.Label(line, text=r.get("name") or "?", bg=PANEL, fg=TEXT, font=(FONT, self.font_size - 1),
                     anchor="w").pack(side="left")
            tk.Label(line, text=str(r.get("score", "")), bg=PANEL, fg=MUTED,
                     font=(FONT, self.font_size - 2)).pack(side="right")
            if r.get("reason"):
                tk.Label(inner, text=r["reason"], bg=PANEL, fg="#6e7681", font=(FONT, self.font_size - 3),
                         anchor="w", justify="left", wraplength=self.width - 46).pack(fill="x", padx=(28, 0))

    # --- 建议
    def _render_advice(self, advice: dict[str, Any]) -> None:
        lines = advice.get("lines") or []
        if not lines and not advice.get("lethal"):
            return
        # 注意：这个方法只拿到 advice，没有 snap
        lethal_on = bool((advice or {}).get("lethal"))
        ref_mode = bool(getattr(self, "_ai_pick", None)) and not lethal_on
        inner = self._section("我的信息",
                              "参考" if ref_mode else ("斩杀" if lethal_on else "决策"))
        if ref_mode and self._ai_pick and (lines[0].get("title") if lines else "") != self._ai_pick[0]:
            tk.Label(inner, text="与 AI 决策不一致，供对照" if self._pick_source == "AI"
                     else "引擎独立评估，仅供参考", bg=PANEL, fg="#c9a05a",
                     font=(FONT, self.font_size - 3), anchor="w").pack(fill="x", pady=(0, 2))
        if advice.get("lethal"):
            self._strip(f"可直接斩杀（{advice.get('lethal_damage', 0)} 点伤害）", fg="#ffb3ad", bg="#3d1418")
            # 斩杀必须给到可执行的顺序，否则「能斩」这句话没用
            steps = (lines[0].get("actions") if lines else None) or []
            if steps:
                inner_s = self._section("斩杀步骤", "按顺序打")
                for i, step in enumerate(steps[:8], 1):
                    row = tk.Frame(inner_s, bg=PANEL)
                    row.pack(fill="x", pady=1)
                    tk.Label(row, text=str(i), bg="#3d1418", fg="#ffb3ad",
                             font=(FONT, self.font_size - 3, "bold"), width=2).pack(side="left")
                    tk.Label(row, text=str(step), bg=PANEL, fg=TEXT,
                             font=(FONT, self.font_size - 1), anchor="w", justify="left",
                             wraplength=self.width - 70).pack(side="left")
        for i, line in enumerate(lines[:3]):
            first = i == 0
            head = tk.Frame(inner, bg=PANEL)
            head.pack(fill="x", pady=(4 if i else 0, 0))
            tk.Label(head, text=str(line.get("rank", i + 1)), bg="#21262d", fg=GOLD if first else MUTED,
                     font=(FONT, self.font_size - 3, "bold"), width=2).pack(side="left")
            tk.Label(head, text=line.get("title") or "", bg=PANEL, fg=TEXT if first else MUTED,
                     font=(FONT, self.font_size - 1 + (1 if first else 0), "bold" if first else "normal"),
                     anchor="w", justify="left", wraplength=self.width - 70).pack(side="left", padx=(5, 0))
            score = line.get("score")
            if score:
                tk.Label(head, text=str(score), bg=PANEL, fg=GOLD if first else MUTED,
                         font=(FONT, self.font_size - 2)).pack(side="right")
            if first and line.get("reason"):
                tk.Label(inner, text=line["reason"], bg=PANEL, fg="#8b949e", font=(FONT, self.font_size - 2),
                         anchor="w", justify="left", wraplength=self.width - 46).pack(fill="x", padx=(24, 0), pady=(2, 0))

    # ------------------------------------------------------------ 收尾
    def _log(self, text: str) -> None:
        try:
            from .cli import LOG_PATH

            with LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(f"[{time.strftime('%H:%M:%S')}] {text}\n")
        except Exception:
            pass

    def quit(self) -> None:
        if getattr(self, "_quitting", False):
            return
        self._quitting = True
        try:
            self.pos = (self.root.winfo_x(), self.root.winfo_y())
        except tk.TclError:
            pass
        self._persist()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self) -> None:
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.root.mainloop()


_MUTEX_NAME = "Local\\HearthstoneAIAssistantOverlay"
ERROR_ALREADY_EXISTS = 183


def _acquire_single_instance() -> tuple[bool, int | None]:
    """用命名互斥体保证只有一个悬浮窗。

    没有这道锁的话，双击两次就会开两个窗口（实测确实会发生），
    而且两个窗口都写同一个 window.json，位置互相打架。
    """
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
        if not handle:
            return True, None
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            return False, handle
        return True, handle
    except Exception:
        return True, None


def _focus_existing() -> bool:
    """把已经在跑的那个窗口提到最前。"""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, "炉石 AI 助手")
        if not hwnd:
            return False
        user32.ShowWindow(hwnd, 9)          # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def main(*, demo: bool = False, log_path: Path | None = None) -> int:
    ok, mutex = _acquire_single_instance()
    if not ok:
        if not _focus_existing():
            try:
                messagebox.showinfo("炉石 AI 助手", "助手已经在运行了，屏幕上应该能找到那个窗口。\n"
                                                 "如果看不见，按 F8 试试。")
            except Exception:
                pass
        return 0

    cards = cards_mod.load()
    if not len(cards):
        try:
            messagebox.showerror("炉石 AI 助手", "卡牌库为空。请先运行：\n\npython -m hsai cards --update")
        except Exception:
            pass
        return 2
    try:
        overlay = Overlay(cards, demo=demo, log_path=log_path)
        overlay._mutex = mutex  # 持有到进程结束
        overlay.run()
    except Exception as exc:  # 无控制台启动时，至少让用户看到原因
        try:
            messagebox.showerror("炉石 AI 助手 启动失败", f"{exc}\n\n详细日志：data\\hsai.log")
        except Exception:
            print(exc, file=sys.stderr)
        return 1
    return 0
