"""主渲染与分区渲染：预判 / 标签权重 / 留牌 / 建议。"""

from __future__ import annotations

import json
import tkinter as tk

from typing import Any

from . import cards as cards_mod
from . import logutil
from .tag_rules import TAG_ORDER
from .ui_text import _weight_summary, hud_row
from .ui_theme import (
    BG, BLUE, BORDER, FONT, GOLD, GREEN, MUTED, PANEL, RED, TEXT, TEXT_SOFT,
    WEIGHT_REASON_WORDS, WEIGHT_TOP_N, YELLOW,
)


class Renderer:
    """主渲染与各分区。Overlay 继承它。"""

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

        # 顶部「我方卡组 / 关键牌」条已按需求移除
        # 只在「我方回合且对局进行中」展示 AI 决策；否则上一回合的建议会一直挂着，
        # 看起来像“现在该这么打”，而实际上已经轮不到你了。
        if snap.get("phase") != "playing" or not snap.get("is_our_turn"):
            if self._ai_pick or self._ai_text or self._ai_route:
                self._ai_pick = None
                self._pick_gap = 0.0
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
            mark = "（强化已解锁 ×4）" if cthun.get("tripled") else ""
            tk.Label(self.body, text=f"克苏恩技能 {cthun.get('count')}/{cthun.get('total')}{mark}",
                     bg=BG, fg=GOLD if cthun.get("tripled") else MUTED,
                     font=(FONT, self.font_size - 3), anchor="w").pack(fill="x")

        self._render_prediction(snap.get("prediction") or {})

        # 对战信息：对手在上，双方血量/手牌/牌库/场攻/墓地/奥秘/武器
        def _row(side, label):
            return hud_row(side, label)

        inner_b = self._section("对战信息")
        for side, label in ((snap.get("opp") or {}, "对手"), (snap.get("our") or {}, "我方")):
            tk.Label(inner_b, text=_row(side, label), bg=PANEL,
                     fg=TEXT if label == "对手" else TEXT_SOFT,
                     font=(FONT, self.font_size - 1), anchor="w", justify="left",
                     wraplength=self.width - 46).pack(fill="x", pady=1)
        self._render_tag_weights(snap.get("tag_weights"))
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
            # 分差只在真的算过时显示：0 是初值，9.9/99 是规则化选择或只有一个候选
            gap = getattr(self, "_pick_gap", 0.0) or 0.0
            if 0.0 < gap < 9.0:
                tip = f"与第二名差 {gap:.1f} 分" + ("（咬得紧）" if gap < 1.0 else "")
                tk.Label(inner_pick, text=tip, bg=PANEL, fg=BLUE,
                         font=(FONT, self.font_size - 3), anchor="w", justify="left",
                         wraplength=self.width - 46).pack(fill="x")
        passive = snap.get("passive") or {}
        every = int(passive.get("every") or 0)
        played = int(passive.get("played") or 0)
        if every > 0:
            left = every - (played % every)
            tip = "下一只就触发" if left == 1 else f"还差 {left} 只触发"
            tk.Label(self.body, text=f"{passive.get('name') or '被动'} · 已 {played} 只 · {tip}",
                     bg=BG, fg=MUTED, font=(FONT, self.font_size - 3), anchor="w").pack(fill="x")

        # 结束回合阶段比回合归属切换早到：这时 advisor 可能仍认为是我方回合，
        # 建议得先撤掉，否则看起来还像「现在该这么打」。
        turn_ending = bool(snap.get("turn_ending"))
        advice = {} if turn_ending else (snap.get("advice") or {})
        if turn_ending:
            self._strip("回合已结束", fg=MUTED, bg=PANEL)
        inner_advice = self._render_advice(advice)

        defense = advice.get("defense") or []
        # 只有「本回合建议」这张卡片真的画出来了，才往里加内容
        if defense and inner_advice is not None:
            tk.Frame(inner_advice, bg="#30363d", height=1).pack(fill="x", pady=(7, 4))
            tk.Label(inner_advice, text="防对面的牌", bg=PANEL, fg="#8b949e",
                     font=(FONT, self.font_size - 3), anchor="w").pack(fill="x")
            for line in defense:
                tk.Label(inner_advice, text=line, bg=PANEL, fg="#e3b341",
                         font=(FONT, self.font_size - 2), anchor="w", justify="left",
                         wraplength=self.width - 46).pack(fill="x", pady=1)

        warning = snap.get("warning") or ""
        if warning:
            self._strip(warning, fg="#e3b341", bg="#2d2614")
        self._request_fit()

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

    # --- 本回合偏重（动态标签权重）
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

    def _render_advice(self, advice: dict[str, Any]) -> Any:
        lines = advice.get("lines") or []
        if not lines and not advice.get("lethal"):
            return None
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
            if first:
                self._disagree_button(head, int(getattr(self, "_cur_turn", 0) or 0))
            score = line.get("score")
            if score:
                tk.Label(head, text=str(score), bg=PANEL, fg=GOLD if first else MUTED,
                         font=(FONT, self.font_size - 2)).pack(side="right")
            if first and line.get("reason"):
                tk.Label(inner, text=line["reason"], bg=PANEL, fg="#8b949e", font=(FONT, self.font_size - 2),
                         anchor="w", justify="left", wraplength=self.width - 46).pack(fill="x", padx=(24, 0), pady=(2, 0))
        return inner
