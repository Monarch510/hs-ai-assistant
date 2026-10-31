"""自对弈 / 训练相关的模拟引擎。"""

from .engine import GameOutcome, greedy_agent, make_deck, match, random_agent, run_game

__all__ = ["GameOutcome", "greedy_agent", "make_deck", "match", "random_agent", "run_game"]
