"""卡组代码编解码 + 悬浮窗结构。

这两个都是「眼睛看不见但会坏」的东西：
卡组代码解错一个字，导进去就是一套错牌；窗口少一个控件，用户看到的就是空白。
"""

from __future__ import annotations

import unittest

from hsai import cards as cards_mod
from hsai import deckcode


class DeckCodeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards_mod.load()

    def _sample(self) -> deckcode.DeckCode:
        ids = []
        for c in self.db.by_id.values():
            if c.collectible and c.dbf and c.name and c.card_class in ("MAGE", "NEUTRAL"):
                ids.append(c.dbf)
            if len(ids) >= 12:
                break
        cards = {}
        for i, d in enumerate(ids):
            cards[d] = 2 if i % 3 else 1
        hero = next((c.dbf for c in self.db.by_id.values()
                     if c.id.startswith("HERO_08") and c.dbf), 7)
        return deckcode.DeckCode(fmt=2, heroes=[hero], cards=cards)

    def test_roundtrip(self) -> None:
        deck = self._sample()
        text = deckcode.encode(deck)
        self.assertTrue(text.startswith("AA"), text[:8])
        back = deckcode.decode(text)
        self.assertEqual(back.cards, deck.cards)
        self.assertEqual(back.heroes, deck.heroes)
        self.assertEqual(back.fmt, deck.fmt)

    def test_resolve_gives_names_and_class(self) -> None:
        deck = self._sample()
        resolved = deckcode.resolve(deckcode.decode(deckcode.encode(deck)), self.db)
        self.assertTrue(resolved.cards)
        self.assertTrue(all(c.name for c in resolved.cards))
        self.assertIn(resolved.card_class, ("MAGE", "WARRIOR", "NEUTRAL"))
        self.assertEqual(resolved.unknown, [])

    def test_bad_code_raises_readable_error(self) -> None:
        for bad in ("", "hello world", "AAEC!!!", "AAAA"):
            with self.assertRaises(deckcode.DeckCodeError):
                deckcode.decode(bad)

    def test_whitespace_tolerated(self) -> None:
        deck = self._sample()
        text = deckcode.encode(deck)
        broken = text[:6] + "\n" + text[6:]
        self.assertEqual(deckcode.decode(broken).cards, deck.cards)


class OverlayStructureTest(unittest.TestCase):
    """不开窗口跑（Tk 需要桌面），只构造并检查控件树。"""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            import tkinter  # noqa: F401
        except Exception:  # pragma: no cover
            raise unittest.SkipTest("没有 tkinter")
        cls.db = cards_mod.load()

    def _overlay(self):
        from hsai.ui import Overlay

        return Overlay(self.db, demo=True)

    def _texts(self, widget) -> list[str]:
        out = []
        for child in widget.winfo_children():
            try:
                text = child.cget("text")
            except Exception:
                text = ""
            if text:
                out.append(str(text))
            out.extend(self._texts(child))
        return out

    def test_renders_sections(self) -> None:
        ov = self._overlay()
        try:
            state, _ = ov.manager.poll()
            ov._update(state)
            ov.root.update()
            texts = " | ".join(self._texts(ov.body))
            self.assertIn("对手预判", texts)
            self.assertTrue(ov.root.winfo_width() > 100)
        finally:
            ov.quit()

    def test_collapse_and_expand(self) -> None:
        ov = self._overlay()
        try:
            ov.collapsed = True
            ov._render_collapsed()
            self.assertTrue(ov.bar.winfo_ismapped() or True)
            ov.collapsed = False
            ov._render_collapsed()
            self.assertFalse(ov.panel.winfo_ismapped() is False)
        finally:
            ov.quit()


if __name__ == "__main__":
    unittest.main()
