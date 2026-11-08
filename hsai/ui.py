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
import sys
import time
import tkinter as tk
from tkinter import messagebox
from pathlib import Path
from typing import Any

from .advisor import Advisor
from . import ai as ai_mod
from . import feedback as feedback_mod
from . import logutil
from . import cards as cards_mod
from .cards import CardDB
from .live import SourceManager
from .paths import (
    ADVICE_FEEDBACK_JSONL, ADVICE_LOG_JSONL, DATA_DIR, LAST_REVIEW_JSON, WINDOW_JSON,
)
from .state import GameState
from .ui_ai import AiWorkers
from .ui_render import Renderer
from .ui_review import ReviewPanel
from .ui_widgets import Widgets

from .ui_text import _fmt_weight, _weight_summary, hud_row  # noqa: F401
from .ui_theme import (  # noqa: F401
    BG, BLUE, BORDER, COLLAPSED_H, DEFAULT_WIDTH, FONT, GOLD, GREEN, LAYOUT_VERSION,
    MUTED, PANEL, RED, TEXT, TEXT_SOFT, WEIGHT_REASON_WORDS, WEIGHT_TOP_N, YELLOW,
)
from .ui_state import _as_float, _as_int, _as_set, _load_state, _save_state  # noqa: F401

STATE_PATH = WINDOW_JSON


class Overlay(AiWorkers, ReviewPanel, Widgets, Renderer):
    def __init__(self, cards: CardDB, *, demo: bool = False, log_path: Path | None = None) -> None:
        self.cards = cards
        self.advisor = Advisor(cards)
        self.manager = SourceManager(cards, demo=demo, log_path=log_path)

        saved = _load_state()
        same_layout0 = _as_int(saved.get("layout", 0), 0) == LAYOUT_VERSION
        self.alpha = _as_float(saved.get("alpha", 0.90), 0.90) if same_layout0 else 0.90
        # 背景模式：solid（整窗半透明，看得清） / key（把底色抠成真透明，缝隙能看见游戏）
        self.bg_mode = saved.get("bg_mode", "solid") if same_layout0 else "solid"
        self.font_size = _as_int(saved.get("font_size", 10), 10)
        self.pos = (_as_int(saved.get("x", 40), 40), _as_int(saved.get("y", 60), 60))
        self.collapsed = bool(saved.get("collapsed", False))
        same_layout = _as_int(saved.get("layout", 0), 0) == LAYOUT_VERSION
        self.width = _as_int(saved.get("w", DEFAULT_WIDTH), DEFAULT_WIDTH) if same_layout else DEFAULT_WIDTH
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
        self._pick_gap = 0.0                 # 0 表示这轮还没算过与第二名的分差
        # 工作线程只写这个字段，主线程在 _tick 里取走 —— Tk 不是线程安全的，
        # 从子线程直接调 root.after 有可能静默失败，导致建议永远不出现
        self._ai_result: tuple | None = None
        self._last_snap: dict[str, Any] | None = None   # 最近一帧快照，记案例时要取回合/法力
        self._case_buf: list[dict] = []          # 本局的决策案例，结束时一次性入库
        self._cases_flushed = False
        self._ai_reviewed = False
        # 折叠状态的区块标题（存在 window.json 的 collapsed_sections 里）。
        # 不能和窗口自身的 collapsed 共用一个键：那个是布尔，这里读成列表，
        # 上次退出时窗口是收起的就会 set(True) 崩在启动阶段。
        self._collapsed: set[str] = _as_set(saved.get("collapsed_sections"))
        self._ai_conv = ai_mod.Conversation(ai_mod.ROUTE_SYSTEM, keep=ai_mod.KEEP_TURNS)
        self._last_review = self._load_last_review()
        self._last_review_shown = False
        self._last_state: GameState | None = None
        self._last_game_ts = ""
        self._ai_lessons: list[str] = []
        # 被点了「不赞同」的回合（局后交给 AI 分析）
        self._disagree_turns: set[int] = set()
        # 重启后也要看得到上次点 👎 的复盘：写盘的地方在 _feedback_worker
        self._feedback_items: list[dict[str, str]] = feedback_mod.load_recent(
            path=ADVICE_FEEDBACK_JSONL)
        self._advice_by_turn: dict[int, dict[str, Any]] = {}
        self._effect_done: set[str] = set()      # 已处理过的牌名
        self._ai_turn: tuple[int, bool] | None = None
        self._fail_streak = 0         # 连续渲染失败次数
        self._render_broken = False   # 是否已切到错误提示
        self._soft_failures: int = 0          # 被兜住的异常次数（不致命但说明有东西坏了）
        self._soft_failure_kinds: dict[str, int] = {}
        self._render_collapsed()      # 必须有这一步：panel 才会真正 pack 上去
        self._show_placeholder()
        self._apply_geometry()
        # 有上一局记录就直接展示复盘（等待对局时用）
        if getattr(self, "_last_review", None):
            self.collapsed = False
            self._render_last_review()
            self._last_review_shown = True
        self.root.deiconify()
        self.root.after(50, self._tick)

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

    # 只绑顶栏是不够的：用户会直接按住面板正文拖，那样没反应。
    # 所以用 bind_all 接管整个窗口的左键拖动，命中「关闭按钮」时放行。
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
        # 合并写：_toggle_section 存的 collapsed_sections 不能被这里覆盖掉
        data = _load_state()
        data.update({
            "x": self.pos[0], "y": self.pos[1], "w": self.width, "layout": LAYOUT_VERSION,
            "alpha": self.alpha, "font_size": self.font_size, "collapsed": self.collapsed,
            "bg_mode": self.bg_mode,
        })
        _save_state(data)

    def _popup_menu(self, event: tk.Event) -> None:
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

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
                self._soft_failures = 0
                self._soft_failure_kinds.clear()
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
        # 新局判定：日志 watcher 每局重建 GameState，回放循环也会换对象；
        # 回放里原地的 create_game 靠 _create_ts 变化兜住。不能只挂 mulligan——
        # 半途接入或日志静止时拿不到留牌阶段，第二局就不会重置，复盘线程不再起。
        game_ts = str(getattr(st, "_create_ts", "") or "")
        new_game = self._last_state is not st or game_ts != self._last_game_ts
        self._last_state = st
        self._last_game_ts = game_ts

        self._last_render = time.time()
        snap = self.advisor.snapshot(st)
        snap["demo"] = self.manager.is_demo
        self._last_snap = snap
        self._cur_turn = int(snap.get("turn") or 0)
        _acts = snap.get("actions") or []
        if _acts and self._cur_turn:
            rec = self._advice_by_turn.setdefault(self._cur_turn, {})
            rec["actions"] = list(_acts)
        if new_game:
            self._ai_lessons = []                # 上一局的 AI 教训不能带到这局
            self._disagree_turns = set()          # 不赞同标记也只属于一局
            self._advice_by_turn = {}
            self._effect_done.clear()
            self._case_buf = []
            self._cases_flushed = False
            self._ai_reviewed = False             # 复盘标志按局清零，否则第二局不再起复盘线程
            self._ai_conv = ai_mod.Conversation(ai_mod.ROUTE_SYSTEM, keep=ai_mod.KEEP_TURNS)
            self._ai_result = None               # 上局还没送到的 AI 结果别贴到新局
            self._ai_turn = None                 # 「本回合已问过」也只属于一局
            self._last_review_shown = False      # 局末回到待机时重画一次复盘

        phase = snap.get("phase")
        if phase == "game_over":
            self._game_over_since = self._game_over_since or time.time()
            self._flush_cases(snap)     # 案例入库不依赖 AI，AI 关着也要记
        else:
            self._game_over_since = None

        idle = phase in (None, "idle") or (
            phase == "game_over" and self._game_over_since and time.time() - self._game_over_since > 8
        )
        self._sync_bar("等待对局" if idle else "对局中")
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
            self._sync_bar("等待对局" if idle else "对局中")
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
            snap.get("end_turn_seq"), snap.get("turn_ending"),
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
            # SECRET 区三类：任务进度、奥秘数变化都要触发重绘，否则面板停在旧值
            tuple(
                (len(s.get("secrets") or []),
                 tuple((q.get("name"), q.get("progress"), q.get("total"))
                       for q in (s.get("quests") or [])),
                 tuple(o.get("name") for o in (s.get("ongoing") or [])))
                for s in (snap.get("our") or {}, snap.get("opp") or {})
            ),
        )

    def _log(self, text: str) -> None:
        logutil.log(text)

    def _note_soft_failure(self, kind: str, exc: BaseException) -> None:
        """兜住异常时记一笔：面板要能看出「有东西坏了」，不能静默。"""
        self._soft_failures += 1
        self._soft_failure_kinds[kind] = self._soft_failure_kinds.get(kind, 0) + 1
        self._log(f"soft failure [{kind}]: {type(exc).__name__}: {exc}")

    def _sync_bar(self, text: str) -> None:
        """状态栏带上被兜住的异常数：别让面板停在上一帧还看着正常。"""
        if self._render_broken:
            return                      # 渲染崩了有红条，别把它冲掉
        if self._soft_failures:
            self.bar_label.config(text=f"{text} · 异常 {self._soft_failures}", fg=YELLOW)
        else:
            self.bar_label.config(text=text)

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
