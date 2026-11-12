"""主渲染与分区渲染：建议 / 对战信息 / 牌库剩余 / 预判 / 权重 / 留牌。

顺序按用户拍板的优先级：要动手的块摊开置顶，参考性的块默认折叠靠后。
"""

from __future__ import annotations

import tkinter as tk

from typing import Any

from .ui_text import _weight_summary, hud_row
from .ui_theme import (
    ADVICE_VISIBLE_ROWS, BLUE, DECK_VISIBLE_ROWS, FONT, GOLD, GREEN, MUTED,
    PANEL, RED, TEXT, TEXT_SOFT, YELLOW,
)


def _wheel_dir(event) -> int:
    """滚轮方向：+1 往下、-1 往上、0 忽略。Windows 看 delta，Linux 看 Button num。"""
    num = getattr(event, "num", 0) or 0
    if num == 4:
        return -1
    if num == 5:
        return 1
    delta = getattr(event, "delta", 0) or 0
    return (-1 if delta > 0 else 1) if delta else 0


class Renderer:
    """主渲染与各分区。Overlay 继承它。"""

    def _bind_wheel_tree(self, widget, handler) -> None:
        # Tk 的滚轮事件不会从子控件冒泡到父 Frame：只绑容器时，鼠标停在
        # 铺满宽度的行 Label 上父级收不到，只有缝隙能滚。行每次重画都会重建，
        # 所以在画完之后把整棵子树都绑一遍（顺带覆盖 Linux 的 Button-4/5）。
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            widget.bind(seq, handler)
        for child in widget.winfo_children():
            self._bind_wheel_tree(child, handler)

    def _render(self, snap: dict[str, Any]) -> None:
        self._cur_turn = int(snap.get("turn") or 0)

        # 局面一变：作废旧建议，并把这条建议记进 advice_log.jsonl
        sig = (
            snap.get("turn"),
            snap.get("end_turn_seq"),
            ((snap.get("our") or {}).get("mana")),
            tuple(str(c.get("id") or c.get("name")) for c in ((snap.get("our") or {}).get("hand") or [])),
            tuple(str(m.get("id") or m.get("name")) for m in ((snap.get("our") or {}).get("board") or [])),
            tuple(str(o.get("name")) for o in ((snap.get("choice") or {}).get("options") or [])),
        )
        if sig != getattr(self, "_advice_sig", None):
            self._advice_sig = sig
            self._log_advice(snap)
            self._ai_pick = None
            self._pick_gap = 0.0
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
            owner = "我方" if ours else "对方"
        self.header_label.config(
            text=f"T{turn} · {owner} · vs {opp}    {phase_text}" if turn else f"vs {opp}    {phase_text}"
        )
        self.source_label.config(text="示例" if snap.get("demo") else "", fg=YELLOW if snap.get("demo") else MUTED)
        self._sync_bar("等待对局")

        # 只在「我方回合且对局进行中」展示 AI 决策；否则上一回合的建议会一直挂着，
        # 看起来像“现在该这么打”，而实际上已经轮不到你了。
        if snap.get("phase") != "playing" or not snap.get("is_our_turn"):
            if self._ai_pick or self._ai_text or self._ai_route:
                self._ai_pick = None
                self._pick_gap = 0.0
                self._ai_text = ""
                self._ai_route = None

        mulligan_first = phase == "mulligan"
        mul = snap.get("mulligan") or {}
        if mulligan_first:
            self._render_mulligan(mul)

        # 结束回合阶段比回合归属切换早到：这时 advisor 可能仍认为是我方回合，
        # 建议得先撤掉，否则看起来还像「现在该这么打」。
        turn_ending = bool(snap.get("turn_ending"))
        advice = {} if turn_ending else (snap.get("advice") or {})
        if turn_ending:
            self._strip("回合已结束", fg=MUTED, bg=PANEL)

        # 先看局面再决定怎么打：对战信息放到建议卡上方，别让建议盖住血量/牌库
        inner_b = self._section("对战信息")
        for side, label in ((snap.get("opp") or {}, "对手"), (snap.get("our") or {}, "我方")):
            tk.Label(inner_b, text=hud_row(side, label), bg=PANEL,
                     fg=TEXT if label == "对手" else TEXT_SOFT,
                     font=(FONT, self.font_size - 1), anchor="w", justify="left",
                     wraplength=self.width - 46).pack(fill="x", pady=1)

        # AI 路线 / 发现抉择 / 引擎建议本来是三张卡，合成一张「本回合建议」
        self._render_advice_card(advice)
        if advice.get("lethal"):
            self._render_lethal_card(advice)

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

        self._render_deck(snap.get("deck") or {})

        # 以下都是「参考性」信息，默认折叠，靠后放，别挤掉要动手的块
        self._render_prediction(snap.get("prediction") or {})
        self._render_tag_weights(snap.get("tag_weights"))
        if not mulligan_first:
            self._render_mulligan(mul)
        self._render_passive(snap.get("passive") or {})
        self._render_cthun(snap.get("cthun") or {})

        warning = snap.get("warning") or ""
        if warning:
            self._strip(warning, fg="#e3b341", bg="#2d2614")
        self._request_fit()

    def _render_advice_card(self, advice: dict[str, Any]) -> Any:
        """路线 / 发现抉择 / 引擎建议 / 防对面的牌合成一张卡，固定高度、滚轮翻页。

        每行先收成一个「画法」再按可见窗口重画：内容再长也不会把窗口越撑越高。
        """
        self._advice_rows = []
        lines = advice.get("lines") or []
        route = getattr(self, "_ai_route", None)
        pick = getattr(self, "_ai_pick", None)
        defense = advice.get("defense") or []
        lethal_on = bool(advice.get("lethal"))
        # 局面每次刷新都会整块重画：内容没变就留住滚动位置，别把人弹回顶部
        key = (tuple(l.get("title") for l in lines),
               tuple(s.get("text") for s in (route or [])),
               pick[0] if pick else None, tuple(defense))
        if key != getattr(self, "_advice_key", None):
            self._advice_scroll = 0
        self._advice_key = key
        if not lines and not route and not pick and not lethal_on and not defense:
            return None
        inner = self._section("本回合建议", accent=True)
        rows: list = []
        if route:
            pos_cn = {"left": "最左", "right": "最右"}
            for i, step in enumerate(route[:6], 1):
                label = step["text"]
                if step.get("pos"):
                    p = pos_cn.get(step["pos"], f"第{step['pos']}位")
                    label += f"（放{p}）"
                matched = bool(step.get("matched"))
                tip = "" if matched else (step.get("why") or "需人工")
                rows.append(lambda parent, i=i, label=label, tip=tip, matched=matched:
                            self._advice_route_row(parent, i, label, tip, matched))
        elif pick:
            rows.append(lambda parent, name=pick[0]: tk.Label(
                parent, text=name, bg=PANEL, fg=GOLD,
                font=(FONT, self.font_size + 1, "bold"), anchor="w",
                justify="left", wraplength=self.width - 46).pack(fill="x"))
            rows.append(lambda parent, reason=pick[1]: tk.Label(
                parent, text=reason, bg=PANEL, fg=MUTED,
                font=(FONT, self.font_size - 2), anchor="w", justify="left",
                wraplength=self.width - 46).pack(fill="x"))
            # 分差只在真的算过时显示：0 是初值，9.9/99 是规则化选择或只有一个候选
            gap = getattr(self, "_pick_gap", 0.0) or 0.0
            if 0.0 < gap < 9.0:
                tip = f"与第二名差 {gap:.1f} 分" + ("（咬得紧）" if gap < 1.0 else "")
                rows.append(lambda parent, tip=tip: tk.Label(
                    parent, text=tip, bg=PANEL, fg=BLUE,
                    font=(FONT, self.font_size - 3), anchor="w", justify="left",
                    wraplength=self.width - 46).pack(fill="x"))
        # AI 与引擎不一致时留一行小字对照，别让两个结论看起来是同一件事
        if pick and not lethal_on and lines and (lines[0].get("title") or "") != pick[0]:
            note = "与 AI 决策不一致，供对照" if self._pick_source == "AI" else "引擎独立评估，仅供参考"
            rows.append(lambda parent, note=note: tk.Label(
                parent, text=note, bg=PANEL, fg="#c9a05a",
                font=(FONT, self.font_size - 3), anchor="w").pack(fill="x", pady=(0, 2)))
        for i, line in enumerate(lines[:3]):
            first = i == 0
            rank = line.get("rank", i + 1)
            rows.append(lambda parent, line=line, first=first, rank=rank:
                        self._advice_line_head(parent, line, first, rank))
            if first and line.get("reason"):
                reason = line["reason"]
                rows.append(lambda parent, reason=reason: tk.Label(
                    parent, text=reason, bg=PANEL, fg="#8b949e",
                    font=(FONT, self.font_size - 2), anchor="w", justify="left",
                    wraplength=self.width - 46).pack(fill="x", padx=(24, 0), pady=(2, 0)))
        # 防对面的牌原来挂在卡片外面，一起收进可滚列表
        defense = advice.get("defense") or []
        if defense:
            rows.append(lambda parent: tk.Frame(parent, bg="#30363d", height=1).pack(fill="x", pady=(7, 4)))
            rows.append(lambda parent: tk.Label(
                parent, text="防对面的牌", bg=PANEL, fg="#8b949e",
                font=(FONT, self.font_size - 3), anchor="w").pack(fill="x"))
            for dline in defense:
                rows.append(lambda parent, dline=dline: tk.Label(
                    parent, text=dline, bg=PANEL, fg="#e3b341",
                    font=(FONT, self.font_size - 2), anchor="w", justify="left",
                    wraplength=self.width - 46).pack(fill="x", pady=1))

        self._advice_rows = rows
        row = tk.Frame(inner, bg=PANEL)
        row.pack(fill="x")
        self._advice_list = tk.Frame(row, bg=PANEL)
        self._advice_list.pack(side="left", fill="x", expand=True)
        self._advice_bar = tk.Frame(row, bg="#21262d", width=4)
        self._advice_bar.pack(side="right", fill="y")
        self._draw_advice_rows()
        return inner

    def _advice_route_row(self, parent, i: int, label: str, tip: str, matched: bool) -> None:
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill="x", pady=1)
        tk.Label(row, text=str(i), bg="#21262d", fg=GOLD,
                 font=(FONT, self.font_size - 3, "bold"), width=2).pack(side="left")
        tk.Label(row, text=label, bg=PANEL,
                 fg=TEXT if matched else "#c9a05a",
                 font=(FONT, self.font_size - 1), anchor="w", justify="left",
                 wraplength=self.width - 70).pack(side="left")
        if tip:
            tk.Label(row, text=tip, bg=PANEL, fg="#c9a05a",
                     font=(FONT, self.font_size - 3)).pack(side="right")

    def _advice_line_head(self, parent, line: dict, first: bool, rank: int) -> None:
        head = tk.Frame(parent, bg=PANEL)
        head.pack(fill="x", pady=(0 if first else 4, 0))
        tk.Label(head, text=str(rank), bg="#21262d", fg=GOLD if first else MUTED,
                 font=(FONT, self.font_size - 3, "bold"), width=2).pack(side="left")
        tk.Label(head, text=line.get("title") or "", bg=PANEL, fg=TEXT if first else MUTED,
                 font=(FONT, self.font_size - 1 + (1 if first else 0), "bold" if first else "normal"),
                 anchor="w", justify="left", wraplength=self.width - 70).pack(side="left", padx=(5, 0))
        if first:
            self._disagree_button(head, int(getattr(self, "_cur_turn", 0) or 0))
        score = line.get("score")
        if score:
            tk.Label(head, text=str(score), bg=PANEL, fg=GOLD if first else MUTED,
                     font=(FONT, self.font_size - 2)).pack(side="right")

    def _render_lethal_card(self, advice: dict[str, Any]) -> None:
        """红条和斩杀步骤原来散在两处，合成一张「能斩」卡。"""
        dmg = advice.get("lethal_damage", 0)
        inner = self._section("能斩", display_title=f"能斩 · {dmg} 点", accent=True)
        tk.Label(inner, text=f"可直接斩杀（{dmg} 点伤害）", bg=PANEL, fg="#ffb3ad",
                 font=(FONT, self.font_size - 1, "bold"), anchor="w").pack(fill="x")
        lines = advice.get("lines") or []
        steps = (lines[0].get("actions") if lines else None) or []
        for i, step in enumerate(steps[:8], 1):
            row = tk.Frame(inner, bg=PANEL)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=str(i), bg="#3d1418", fg="#ffb3ad",
                     font=(FONT, self.font_size - 3, "bold"), width=2).pack(side="left")
            tk.Label(row, text=str(step), bg=PANEL, fg=TEXT,
                     font=(FONT, self.font_size - 1), anchor="w", justify="left",
                     wraplength=self.width - 70).pack(side="left")

    def _render_deck(self, deck: dict[str, Any]) -> None:
        """牌库剩余：标题只写张数，明细从高费到低费列名字/数量/抽取率，滚轮翻页。"""
        cards = deck.get("cards") or []
        if not cards:
            return
        left = deck.get("left")
        tail = "未知" if left is None else str(left)
        inner = self._section("牌库剩余", display_title=f"牌库剩余 · {tail} 张")
        # 牌没变就留住滚动位置：日志静止时 _update 每秒都会重画一帧，
        # 每次清零的话刚滚下去就被弹回顶部
        key = tuple((c.get("id"), c.get("count")) for c in cards)
        if key != getattr(self, "_deck_key", None):
            self._deck_scroll = 0
        self._deck_key = key
        self._deck_cards = cards
        self._deck_left = left
        row = tk.Frame(inner, bg=PANEL)
        row.pack(fill="x")
        self._deck_list = tk.Frame(row, bg=PANEL)
        self._deck_list.pack(side="left", fill="x", expand=True)
        self._deck_bar = tk.Frame(row, bg="#21262d", width=4)
        self._deck_bar.pack(side="right", fill="y")
        self._draw_deck_rows()
        # 自动认出的套牌常是残套（Decks.log 只记到部分牌），明细凑不满标题的
        # 真实张数；差多少就明说，不编牌名，也不拿残明细冒充完整牌库
        known = sum(c.get("count", 1) for c in cards)
        unknown = left - known if isinstance(left, int) and left > known else 0
        if unknown:
            tk.Label(inner, text=f"还有 {unknown} 张未识别（明细不完整）",
                     bg=PANEL, fg=MUTED, font=(FONT, self.font_size - 2),
                     anchor="w", justify="left").pack(fill="x", pady=(2, 0))

    def _draw_deck_rows(self) -> None:
        box = getattr(self, "_deck_list", None)
        bar = getattr(self, "_deck_bar", None)
        if box is None or bar is None:
            return
        cards = self._deck_cards
        start = self._deck_scroll
        for c in list(box.winfo_children()):
            c.destroy()
        total_left = getattr(self, "_deck_left", None)
        for c in cards[start:start + DECK_VISIBLE_ROWS]:
            count = c.get("count", 1)
            # 抽取率 = 这张牌还剩几张 ÷ 牌库还剩几张（下一抽命中率）
            rate = (f"{min(1.0, count / total_left):.0%}"
                    if isinstance(total_left, int) and total_left > 0 else "未知")
            line = tk.Frame(box, bg=PANEL)
            line.pack(fill="x", pady=1)
            # 百分比单独贴行末、只留数值；先打包它才不会被长牌名挤出可视区
            tk.Label(line, text=rate, bg=PANEL, fg=MUTED,
                     font=(FONT, self.font_size - 2)).pack(side="right", padx=(0, 2))
            tk.Label(line, text=f"{c.get('cost', '?')}  {c.get('name', '?')} ×{count}",
                     bg=PANEL, fg=TEXT_SOFT, font=(FONT, self.font_size - 2),
                     anchor="w").pack(side="left", fill="x", expand=True)
        for c in list(bar.winfo_children()):
            c.destroy()
        track_h = DECK_VISIBLE_ROWS * 18
        cv = tk.Canvas(bar, width=4, height=track_h, bg="#21262d",
                       highlightthickness=0, bd=0)
        cv.pack(fill="y")
        total = len(cards)
        if total > DECK_VISIBLE_ROWS:
            h = max(8, int(track_h * DECK_VISIBLE_ROWS / total))
            max_start = total - DECK_VISIBLE_ROWS
            top = int((track_h - h) * start / max_start) if max_start else 0
            cv.create_rectangle(0, top, 4, top + h, fill=GOLD, width=0)
        self._bind_wheel_tree(box, self._on_deck_wheel)
        self._bind_wheel_tree(bar, self._on_deck_wheel)

    def _on_deck_wheel(self, event) -> None:
        cards = getattr(self, "_deck_cards", None) or []
        total = len(cards)
        if total <= DECK_VISIBLE_ROWS:
            return
        self._deck_scroll = max(0, min(total - DECK_VISIBLE_ROWS,
                                       self._deck_scroll + _wheel_dir(event)))
        self._draw_deck_rows()

    def _draw_advice_rows(self) -> None:
        box = getattr(self, "_advice_list", None)
        bar = getattr(self, "_advice_bar", None)
        if box is None or bar is None:
            return
        rows = self._advice_rows
        start = self._advice_scroll
        for c in list(box.winfo_children()):
            c.destroy()
        for fn in rows[start:start + ADVICE_VISIBLE_ROWS]:
            fn(box)
        for c in list(bar.winfo_children()):
            c.destroy()
        total = len(rows)
        if total > ADVICE_VISIBLE_ROWS:
            track_h = ADVICE_VISIBLE_ROWS * 18
            cv = tk.Canvas(bar, width=4, height=track_h, bg="#21262d",
                           highlightthickness=0, bd=0)
            cv.pack(fill="y")
            h = max(8, int(track_h * ADVICE_VISIBLE_ROWS / total))
            max_start = total - ADVICE_VISIBLE_ROWS
            top = int((track_h - h) * start / max_start) if max_start else 0
            cv.create_rectangle(0, top, 4, top + h, fill=GOLD, width=0)
        self._bind_wheel_tree(box, self._on_advice_wheel)
        self._bind_wheel_tree(bar, self._on_advice_wheel)

    def _on_advice_wheel(self, event) -> None:
        rows = getattr(self, "_advice_rows", None) or []
        total = len(rows)
        if total <= ADVICE_VISIBLE_ROWS:
            return
        self._advice_scroll = max(0, min(total - ADVICE_VISIBLE_ROWS,
                                         self._advice_scroll + _wheel_dir(event)))
        self._draw_advice_rows()

    def _render_prediction(self, pred: dict[str, Any]) -> None:
        if not pred:
            return
        kicker = "本地样本" if pred.get("source") == "learned" else "核心"
        name = pred.get("name") or "未知"
        conf = float(pred.get("confidence") or 0)
        unknown = name in ("未知卡组", "未知") or conf <= 0
        shown = "未识别（可能是新卡组）" if unknown else name
        # 折叠时那一行也要带上名字和置信度，否则收起来就是一句没信息量的标题
        inner = self._section("对手预判", kicker,
                              display_title=f"对手预判 · {shown} {conf:.0%}")
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
                nm = str(c.get("name") or "未知")
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
            tk.Label(inner, text=f"{label}：{c.get('name') or '未知'}{cost_txt}{tag}",
                     bg=PANEL, fg=MUTED, font=(FONT, self.font_size - 2),
                     anchor="w", justify="left", wraplength=self.width - 46).pack(fill="x", pady=(3 if i == 0 else 1, 0))

    def _render_tag_weights(self, tag_weights: Any) -> None:
        """只显示权重最高的几类和一句解释；快照里没有这个字段就什么都不画。"""
        tops, reasons = _weight_summary(tag_weights)
        if not tops:
            return
        inner = self._section("本回合偏重", "标签权重")
        tk.Label(inner, text=" · ".join(tops), bg=PANEL, fg=GOLD,
                 font=(FONT, self.font_size - 2, "bold"), anchor="w", justify="left",
                 wraplength=self.width - 46).pack(fill="x")
        for line in reasons:
            tk.Label(inner, text=line, bg=PANEL, fg=MUTED,
                     font=(FONT, self.font_size - 3), anchor="w", justify="left",
                     wraplength=self.width - 46).pack(fill="x", pady=(2, 0))

    def _render_passive(self, passive: dict[str, Any]) -> None:
        every = int(passive.get("every") or 0)
        played = int(passive.get("played") or 0)
        if every <= 0:
            return
        left = every - (played % every)
        tip = "下一只就触发" if left == 1 else f"还差 {left} 只触发"
        inner = self._section("被动计数")
        tk.Label(inner, text=f"{passive.get('name') or '被动'} · 已 {played} 只 · {tip}",
                 bg=PANEL, fg=MUTED, font=(FONT, self.font_size - 3), anchor="w").pack(fill="x")

    def _render_cthun(self, cthun: dict[str, Any]) -> None:
        if not cthun.get("count"):
            return
        mark = "（强化已解锁 ×4）" if cthun.get("tripled") else ""
        inner = self._section("克苏恩")
        tk.Label(inner, text=f"克苏恩技能 {cthun.get('count')}/{cthun.get('total')}{mark}",
                 bg=PANEL, fg=GOLD if cthun.get("tripled") else MUTED,
                 font=(FONT, self.font_size - 3), anchor="w").pack(fill="x")

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

    def _disagree_button(self, parent, turn: int) -> None:
        """建议行末尾的「不赞同」标记：点一下标红，再点取消。"""
        marked = turn in self._disagree_turns
        lbl = tk.Label(parent, text="👎" if not marked else "👎✓",
                       bg=PANEL, fg=RED if marked else "#5b636e",
                       cursor="hand2", font=(FONT, self.font_size - 2))
        lbl.pack(side="right", padx=(4, 0))
        lbl.bind("<Button-1>", lambda _e, t=turn: self._toggle_disagree(t))

    def _toggle_disagree(self, turn: int) -> None:
        if not turn:
            return
        if turn in self._disagree_turns:
            self._disagree_turns.discard(turn)
        else:
            self._disagree_turns.add(turn)
        self._last_sig = None      # 下一帧会重画
