"""对手职业被误判成我方职业的回归（m03704）。

真实日志里 "FULL_ENTITY - Updating <玩家名>" 的裸 tag 会漂到上一张牌上，
把 PLAYER_ID / HERO_ENTITY 挂到杂牌实体，两个 pid 都指到同一个 eid；
CREATE_GAME 按 controller 建的 _hero_entities 才是对的。旧 hero() 优先信
玩家实体上的 HERO_ENTITY 还反向覆盖正确映射，于是双方都返回同一个英雄。
"""

from __future__ import annotations

import types
import unittest

from hsai.state import GameState
from hsai.state_types import Entity, T_HERO_ENTITY, T_PLAYER_ID, ZONE_PLAY


def _fake_db():
    """只认两张英雄皮肤牌的卡库，不依赖 data/ 里的真实卡表。"""
    classes = {"HERO_03g": "ROGUE", "HERO_02ai": "SHAMAN"}

    def get(cid):
        cls = classes.get(cid)
        if cls is None:
            return None
        return types.SimpleNamespace(card_class=cls, type="HERO", name=cid,
                                     cost=0, health=30, is_weapon=False,
                                     is_minion=False, has=lambda _k: False)

    return types.SimpleNamespace(get=get)


class HeroOwnerTest(unittest.TestCase):
    def _polluted(self) -> GameState:
        st = GameState(_fake_db())
        st.our_id, st.opp_id = 2, 1
        # 两个 pid 都指到同一张杂牌，且它带着 HERO_ENTITY=74（我方英雄）
        st.player_ids = {1: 31, 2: 31}
        st.entities[31] = Entity(31, card_id="PET_16_2", controller=1,
                                 zone=ZONE_PLAY,
                                 tags={T_PLAYER_ID: 2, T_HERO_ENTITY: 74})
        # CREATE_GAME 按 controller 建的正确映射
        st._hero_entities = {1: 72, 2: 74}
        st.entities[72] = Entity(72, card_id="HERO_03g", controller=1,
                                 zone=ZONE_PLAY, tags={"HEALTH": 30})
        st.entities[74] = Entity(74, card_id="HERO_02ai", controller=2,
                                 zone=ZONE_PLAY, tags={"HEALTH": 30})
        return st

    def test_colliding_player_ids_still_pick_the_right_hero(self) -> None:
        st = self._polluted()
        self.assertEqual(st.hero(1).id, 72, "对手英雄应是 72（潜行者）")
        self.assertEqual(st.hero(2).id, 74, "我方英雄应是 74（萨满祭司）")

    def test_sides_reports_distinct_classes(self) -> None:
        st = self._polluted()
        ours, opp = st.sides()
        self.assertEqual(opp.class_cn, "潜行者")
        self.assertEqual(ours.class_cn, "萨满祭司")
        self.assertNotEqual(ours.class_en, opp.class_en)

    def test_no_mapping_falls_back_to_player_entity_hero_tag(self) -> None:
        # 手工构造的状态没有 _hero_entities，仍要走玩家实体上的 HERO_ENTITY
        st = GameState(_fake_db())
        st.player_ids = {1: 10, 2: 11}
        st._hero_entities = {}
        st.entities[10] = Entity(10, tags={T_PLAYER_ID: 1, T_HERO_ENTITY: 20})
        st.entities[11] = Entity(11, tags={T_PLAYER_ID: 2, T_HERO_ENTITY: 21})
        st.entities[20] = Entity(20, card_id="HERO_03g", controller=1,
                                 zone=ZONE_PLAY, tags={"HEALTH": 30})
        st.entities[21] = Entity(21, card_id="HERO_02ai", controller=2,
                                 zone=ZONE_PLAY, tags={"HEALTH": 30})
        self.assertEqual(st.hero(1).id, 20)
        self.assertEqual(st.hero(2).id, 21)


if __name__ == "__main__":
    unittest.main()
