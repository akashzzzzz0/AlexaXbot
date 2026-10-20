"""Focused regression tests for ALEXA's UNO rules and turn mechanics."""
import unittest

from bot.handlers import uno


class UnoRulesTests(unittest.TestCase):
    def test_lobby_board_renders_before_game_has_started(self):
        # /uno creates a lobby before dealing cards. The board must have a
        # placeholder top card so rendering does not crash on an empty discard.
        game = {
            "id": "abc123",
            "players": [{"id": 1, "name": "Creator"}],
            "hands": {},
            "discard": [("R", "0")],
            "color": "R",
            "started": False,
            "current": 0,
        }
        board = uno._board_text(game, "Join the game")
        self.assertIn("ALEXA UNO", board)
        self.assertIn("Red 0", board)
        self.assertIn("Players joined", board)

    def test_deck_has_standard_108_cards(self):
        deck = uno._make_deck()
        self.assertEqual(len(deck), 108)
        for color in uno.COLORS:
            self.assertEqual(deck.count((color, "0")), 1)
            for number in map(str, range(1, 10)):
                self.assertEqual(deck.count((color, number)), 2)
            for action in ("Skip", "Reverse", "+2"):
                self.assertEqual(deck.count((color, action)), 2)
        self.assertEqual(deck.count(("W", "Wild")), 4)
        self.assertEqual(deck.count(("W", "+4")), 4)

    def test_card_is_legal_by_colour_or_value(self):
        game = {
            "discard": [("R", "5")],
            "color": "R",
            "hands": {1: []},
        }
        self.assertTrue(uno._legal(game, ("R", "9"), 1))
        self.assertTrue(uno._legal(game, ("B", "5"), 1))
        self.assertFalse(uno._legal(game, ("B", "9"), 1))
        self.assertTrue(uno._legal(game, ("W", "Wild"), 1))

    def test_wild_draw_four_requires_no_current_colour_card(self):
        game = {
            "discard": [("R", "5")],
            "color": "R",
            "hands": {1: [("R", "2"), ("B", "8")]},
        }
        self.assertFalse(uno._legal(game, ("W", "+4"), 1))
        game["hands"][1] = [("B", "8"), ("Y", "Skip")]
        self.assertTrue(uno._legal(game, ("W", "+4"), 1))

    def test_turn_advance_respects_direction_and_wraps(self):
        game = {
            "players": [{"id": 1}, {"id": 2}, {"id": 3}],
            "current": 0,
            "direction": 1,
        }
        uno._advance(game)
        self.assertEqual(game["current"], 1)
        game["direction"] = -1
        uno._advance(game)
        self.assertEqual(game["current"], 0)
        uno._advance(game)
        self.assertEqual(game["current"], 2)

    def test_draw_recycles_discard_pile_but_keeps_top_card(self):
        game = {
            "deck": [],
            "discard": [("R", "2"), ("G", "4"), ("B", "7")],
            "players": [{"id": 1}],
            "current": 0,
            "hands": {1: []},
        }
        uno._draw_for(game, 1, 1)
        self.assertEqual(game["discard"], [("B", "7")])
        self.assertEqual(len(game["hands"][1]), 1)
        self.assertIn(game["hands"][1][0], [("R", "2"), ("G", "4")])


if __name__ == "__main__":
    unittest.main()
