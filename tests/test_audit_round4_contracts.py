"""补回被删的三条静态契约护栏（round4）。

只扫确认过安全的文件：别的代理正在并行改那些源文件，
扫进去会因别人的正常改动随机挂，护栏反而没人信。
"""

from __future__ import annotations

import pathlib
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent

# 只放确认存在、且没有合法命中的文件：×3 在 mulligan.py 注释 /
# test_cthun_skills.py 文档串里是合法的；ui.py / state.py 正被并行改，policy.py
# 是门面，都不扫 —— 少扫一个文件胜过被别人的正常改动弄挂。
_SAFE_FILES = (
    "README.md",
    "AGENTS.md",
    "hsai/model_effects.py",
    "hsai/policy_actions.py",
    "tests/test_cthun_progress.py",
)

_STALE_X3 = "×" + "3"  # 拆开写，免得本文件自己被 grep 命中


class AuditRound4Contracts(unittest.TestCase):
    def test_snapshot_has_no_source_key(self) -> None:
        """source 只写不读，别再往快照里塞（原 test_audit_fixes_round3）。"""
        from hsai import advisor, cards, state

        db = cards.CardDB([])
        snap = advisor.Advisor(db).snapshot(state.GameState(db))
        self.assertNotIn("source", snap, "source 只写不读，别再往快照里塞")

    def test_no_stale_times_three_wording(self) -> None:
        """旧的三倍口径已废，别再写回文档/引擎（原 test_cthun_skills）。"""
        for name in _SAFE_FILES:
            with self.subTest(name=name):
                src = (REPO / name).read_text(encoding="utf-8")
                self.assertNotIn(_STALE_X3, src, f"{name} 还写着旧的倍数")
                self.assertNotIn("mult = 3", src, f"{name} 还有硬编码的 3 倍")

    def test_source_no_longer_has_identity_expression(self) -> None:
        """恒等表达式是旧 bug，别让模型评估里复活（原 round2_engine）。"""
        src = (REPO / "hsai" / "model_eval.py").read_text(encoding="utf-8")
        self.assertNotIn("(0 if u.divine else 0)", src)


if __name__ == "__main__":
    unittest.main()
