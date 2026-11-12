"""上一局复盘展示：读 data/last_review.json 画出来。"""

from __future__ import annotations

import tkinter as tk

import json

from . import cards as cards_mod
from .ui_theme import FONT, GOLD, MUTED, PANEL, TEXT, TEXT_SOFT


class ReviewPanel:
    """上一局复盘。Overlay 继承它。"""

    def _load_last_review(self) -> dict | None:
        try:
            # 这个路径被 tests/_ui_support.py 在 ui 模块上改道到临时目录，
            # 所以必须运行时回查 ui.LAST_REVIEW_JSON，不能拿 import 时的副本
            from . import ui
            path = ui.LAST_REVIEW_JSON
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def _render_last_review(self) -> None:
        """等待对局时展示上一局的复盘。"""
        for child in self.body.winfo_children():
            child.destroy()
        rev = self._last_review or {}
        inner = self._section("上一局", "复盘")
        cn = lambda x: cards_mod.CLASS_CN.get(str(x or ""), str(x or "?"))
        # 对局结束后客户端会清掉英雄信息，职业经常是「未知」——那就不显示
        _our_c = cn(rev.get('our_class'))
        _opp_c = cn(rev.get('opp_class'))
        parts = []
        if "?" not in _our_c and "?" not in _opp_c and "未知" not in (_our_c + _opp_c):
            parts.append(f"{_our_c} vs {_opp_c}")
        if rev.get("result"):
            parts.append("胜" if rev["result"] == "win" else "负")
        if rev.get("turns"):
            parts.append(f"{rev['turns']} 回合")
        head = " · ".join(parts)
        tk.Label(inner, text=head, bg=PANEL, fg=TEXT,
                 font=(FONT, self.font_size, "bold"), anchor="w", justify="left",
                 wraplength=self.width - 46).pack(fill="x", pady=(0, 3))
        rows = [r for r in (list(rev.get("findings") or [])
                            + list(rev.get("issues") or []))
                if str(r).strip() and "未建模" not in str(r)]
        if not rows:
            rows = ["上一局没有可复盘的记录"]
        for line in rows[:5]:
            tk.Label(inner, text=str(line), bg=PANEL, fg=TEXT_SOFT,
                     font=(FONT, self.font_size - 1), anchor="w", justify="left",
                     wraplength=self.width - 46).pack(fill="x", pady=1)
        # 被标「不赞同」的回合，AI 给出的教训 + 规则建议
        for item in (getattr(self, "_feedback_items", []) or [])[:3]:
            tk.Label(inner, text="不赞同复盘：" + str(item.get("lesson") or ""),
                     bg=PANEL, fg="#e3b341", font=(FONT, self.font_size - 2),
                     anchor="w", justify="left", wraplength=self.width - 46).pack(fill="x", pady=(2, 0))
            if item.get("rule"):
                tk.Label(inner, text="→ " + str(item["rule"]), bg=PANEL, fg=MUTED,
                         font=(FONT, self.font_size - 3), anchor="w", justify="left",
                         wraplength=self.width - 46).pack(fill="x")

        lessons = [str(x) for x in (getattr(self, "_ai_lessons", []) or []) if str(x).strip()]
        # 只显示和这局对手职业对得上的教训（教训里会提到职业名）
        opp_cn = str((rev.get("opp_class") and cards_mod.CLASS_CN.get(str(rev.get("opp_class")), "")) or "")
        others = {v for k, v in cards_mod.CLASS_CN.items() if v and v != opp_cn}
        if opp_cn:
            lessons = [x for x in lessons if not any(c in x for c in others)]
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
