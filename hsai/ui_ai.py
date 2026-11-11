"""AI 线程组：局末复盘 / 记录反馈 / 单步建议。

这些方法只通过 self 上的队列式字段与主循环共享状态
（_ai_result / _feedback_items …），所以能安全做成 Mixin。

注意：这里**不再**让 AI 在局中补卡牌效果规则 —— 未建模的牌只记日志，
改由人看过复盘后决定要不要补。"""

from __future__ import annotations

import json

import threading
from datetime import datetime
from typing import Any

from .advisor import board_from_state
from . import ai as ai_mod
from . import feedback as feedback_mod
from . import memory
from . import policy as policy_mod
from .state import GameState

class AiWorkers:
    """AI 线程与规则补充。Overlay 继承它。"""

    @staticmethod
    def _has_options(snap: dict[str, Any]) -> bool:
        our = snap.get("our") or {}
        mana = our.get("mana") or 0
        if any((c.get("cost") or 0) <= mana for c in (our.get("hand") or [])):
            return True
        if any(m.get("can_attack") for m in (our.get("board") or [])):
            return True
        hp = our.get("hero_power")
        # 没读到技能就当没有，别把空快照误判成「有 0 费技能可用」
        if hp and not our.get("hero_power_used") and (hp.get("cost") or 0) <= mana:
            return True
        return False

    def _maybe_ai(self, snap: dict[str, Any], st: GameState | None = None) -> None:
        """每回合最多请求一次 AI 解释，不跟着 320ms 的刷新跑。"""
        if not ai_mod.is_ready():
            return
        if snap.get("turn_ending"):
            # 回合已经结束，算出来的路线到不了手上
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
                self._pick_gap = float(gap)
                self._pick_source = "引擎"
                self._ai_route = None
                self._ai_text = ""
                self._ai_turn = (int(snap.get("turn") or 0), True, choice_sig, None)
                return
            except Exception as exc:
                self._note_soft_failure("choice", exc)

        # 逐回合的 AI 路线默认关闭：一局要调 20 多次，性价比低。
        # 想开就在 data/ai.json 里设 turn_advice=true
        if not choice_sig and not ai_mod.load_config().get("turn_advice"):
            return
        key = (int(snap.get("turn") or 0), True, choice_sig, board_sig)
        if key == self._ai_turn:
            return
        self._ai_turn = key
        self._ai_text = ""
        # 等 1.5 秒再请求：刚抽到的牌要过一会儿才进日志，
        # 立刻发出去会让 AI 基于不完整的手牌做决策
        # 发现/抉择的选项一出现就是完整的，不需要等日志稳定；
        # 用更短的延迟，避免「选项都弹出来了还没有建议」
        delay = 300 if choice_sig else 1500
        self.root.after(delay, lambda: self._ai_fire(key, snap, st))

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
                        except Exception as exc:
                            # 校验失败不影响展示，但要留下痕迹，否则「AI 建议突然变空」查不出来
                            self._log(f"路线校验失败: {exc}")
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
        except Exception as exc:
            self._note_soft_failure("advice", exc)
        text = ""
        self._ai_result = (text, pick, None)

    def _maybe_effects(self, snap: dict[str, Any]) -> None:
        """把本局未建模的牌记进 data/hsai.log，**不调 AI、不改解析行为**。

        以前这里会立刻请求 AI 写正则、验证后写进 effects.json —— 等于运行时
        改掉解析规则，出问题时很难倒查是哪一局、哪条规则改的。现在只留记录：
        未建模的牌会随快照进局末复盘摘要，由人决定要不要补规则。
        """
        for name in (snap.get("unmodelled") or []):
            name = str(name)
            if not name or name in self._effect_done:
                continue
            self._effect_done.add(name)
            self._log(f"未建模的牌：{name}")

    def _feedback_worker(self) -> None:
        """对被标「不赞同」的回合做一次复盘，写进 advice_feedback.jsonl。"""
        cases = []
        for turn in sorted(self._disagree_turns):
            rec = self._advice_by_turn.get(turn) or {}
            if not rec:
                continue
            cases.append({
                "turn": turn,
                "mana": rec.get("mana"),
                "board": rec.get("board"),
                "foe_board": rec.get("foe_board"),
                "hand": rec.get("hand"),
                "advice": rec.get("advice"),
                "played": rec.get("actions") or [],
            })
        if not cases:
            return
        try:
            summary = feedback_mod.build_summary(cases)
            items = feedback_mod.feedback(summary)
            if not items:
                return
            self._feedback_items = list(items)
            rec = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "cases": cases, "items": items}
            # 路径会被测试在 ui 模块上改道，运行时回查
            from . import ui as _ui
            with open(_ui.ADVICE_FEEDBACK_JSONL, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as exc:
            self._log(f"feedback worker failed: {exc}")

    def _ai_review_worker(self, snap: dict[str, Any]) -> None:
        """对局结束：AI 总结教训（lessons.jsonl）+ 参数建议（advice_tuning.jsonl）。

        参数建议只落盘、不自动改权重，攒够局数后由人看过汇总再决定。
        """
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
            lessons, tunings = ai_mod.review(snap, summary, missed_lethal=missed)
            self._ai_lessons = list(lessons or [])
            meta = {
                "our_class": str((snap.get("our") or {}).get("class_en") or ""),
                "opp_class": str((snap.get("opp") or {}).get("class_en") or ""),
                "archetype": str((snap.get("prediction") or {}).get("name") or ""),
                "result": str(snap.get("result") or ""),
            }
            if tunings:
                # 只落盘，不自动改权重：单局样本撑不起全局调整
                memory.add_tunings(tunings, meta)
            if self._disagree_turns:
                threading.Thread(target=self._feedback_worker, daemon=True).start()
            if lessons:
                memory.add_lessons(lessons, meta)
        except Exception as exc:
            self._note_soft_failure("review", exc)

    def _ai_done(self, text: str, pick: tuple[str, str] | None = None,
                 steps: list[dict] | None = None) -> None:
        self._ai_text = text
        self._ai_route = steps
        if steps:
            snap_now = self._last_snap or {}
            self._case_buf.append({
                "turn": snap_now.get("turn") or 0,
                "mana": ((snap_now.get("our") or {}).get("mana")),
                "our_board_n": len(((snap_now.get("our") or {}).get("board") or [])),
                "action": steps[0].get("text") or "",
            })
        if pick is not None:
            self._ai_pick = pick
        self._last_sig = None

    def _flush_cases(self, snap: dict[str, Any]) -> None:
        """对局结束：把本局记下的决策案例一次性入库，供下次相似局面检索。

        这条链路以前是断的 —— `_case_buf` 只进不出、`memory.add_cases` 没有调用方，
        于是 `cases.jsonl` 从未生成，而 `ai.py` 那边一直在读它，永远读到空。
        """
        if self._cases_flushed or not self._case_buf:
            return
        self._cases_flushed = True
        try:
            memory.add_cases(self._case_buf, {
                "our_class": str((snap.get("our") or {}).get("class_en") or ""),
                "opp_class": str((snap.get("opp") or {}).get("class_en") or ""),
                "archetype": str((snap.get("prediction") or {}).get("name") or ""),
                "result": str(snap.get("result") or ""),
            })
            self._case_buf = []
        except Exception as exc:
            self._log(f"案例入库失败: {exc}")

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
                # 同一回合的建议留一份在内存里，局后被标「不赞同」时要拿它对照
                # _update 先写进来的 actions 要留住，所以只补字段不整桶覆盖
                bucket = self._advice_by_turn.setdefault(int(rec["turn"] or 0), {})
                bucket.update({
                    "advice": rec["advice"], "hand": rec["hand"],
                    "board": rec["board"], "foe_board": rec["foe_board"],
                    "mana": rec["mana"],
                })
                # 路径会被测试在 ui 模块上改道，运行时回查
                from . import ui as _ui
                path = _ui.ADVICE_LOG_JSONL
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                rows = path.read_text(encoding="utf-8").splitlines()
                if len(rows) > 400:
                    path.write_text("\n".join(rows[-400:]) + "\n", encoding="utf-8")
            except Exception as exc:
                self._log(f"advice log failed: {exc}")

    def _drain_ai_results(self) -> None:
        """把工作线程算好的结果搬到界面上（只在主线程做）。"""

        res = self._ai_result
        if res is not None:
            self._ai_result = None
            text, pick, steps = res
            self._ai_done(text, pick, steps)
