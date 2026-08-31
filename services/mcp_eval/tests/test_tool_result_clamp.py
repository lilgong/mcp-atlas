import os
import unittest
from unittest.mock import patch

from mcp_completion.agent_eval import (
    _call_budget,
    _clamp_tool_result,
    _tool_result_char_limit,
    _turn_result_char_limit,
)
from mcp_completion.schema import ImageContent, TextContent


class ClampToolResultTests(unittest.TestCase):
    def test_default_limits_disable_clamping(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_tool_result_char_limit(), 0)
            self.assertEqual(_turn_result_char_limit(), 0)

    def test_small_result_passes_through_untouched(self):
        content = [TextContent(type="text", text="ok")]
        clamped, dropped, kept = _clamp_tool_result(content, 1000)
        self.assertEqual(dropped, 0)
        self.assertIs(clamped, content)

    def test_oversized_result_is_clipped_to_the_budget(self):
        content = [TextContent(type="text", text="x" * 5000)]
        clamped, dropped, kept = _clamp_tool_result(content, 1000)

        self.assertEqual(dropped, 4000)
        self.assertEqual(len(clamped[0].text), 1000)
        self.assertIn("truncated", clamped[-1].text)
        self.assertIn("5000", clamped[-1].text)

    def test_budget_is_shared_across_multiple_text_parts(self):
        content = [
            TextContent(type="text", text="a" * 800),
            TextContent(type="text", text="b" * 800),
        ]
        clamped, dropped, kept = _clamp_tool_result(content, 1000)

        kept_chars = sum(
            len(part.text)
            for part in clamped[:-1]
            if isinstance(part, TextContent)
        )
        self.assertEqual(kept_chars, 1000)
        self.assertEqual(kept, 1000)
        self.assertEqual(dropped, 600)

    def test_non_text_content_survives_clamping(self):
        content = [
            TextContent(type="text", text="x" * 5000),
            ImageContent(type="image", data="zzz", mimeType="image/png"),
        ]
        clamped, dropped, kept = _clamp_tool_result(content, 100)

        self.assertTrue(
            any(isinstance(part, ImageContent) for part in clamped),
            "image content must not be silently dropped",
        )
        self.assertEqual(dropped, 4900)

    def test_zero_limit_disables_clamping(self):
        content = [TextContent(type="text", text="x" * 5000)]
        clamped, dropped, kept = _clamp_tool_result(content, 0)
        self.assertEqual(dropped, 0)
        self.assertIs(clamped, content)
        self.assertEqual(kept, 5000)


class TurnBudgetTests(unittest.TestCase):
    """A per-call cap alone is defeated by parallel tool calls."""

    def test_parallel_calls_cannot_exceed_the_turn_budget(self):
        # The regression: seven parallel calls each clipped to the per-call
        # limit put 7 x 120k characters into a single turn.
        turn_budget = 150_000
        per_call = 120_000
        total_kept = 0

        for call_index in range(7):
            calls_left = 7 - call_index
            limit = _call_budget(turn_budget, calls_left, per_call)
            _, _, kept = _clamp_tool_result(
                [TextContent(type="text", text="x" * 400_000)], limit
            )
            turn_budget -= kept
            total_kept += kept

        self.assertLessEqual(total_kept, 150_000)

    def test_unspent_share_rolls_forward_to_later_calls(self):
        turn_budget = 1000
        per_call = 1000

        first_limit = _call_budget(turn_budget, 2, per_call)
        _, _, first_kept = _clamp_tool_result(
            [TextContent(type="text", text="x" * 10)], first_limit
        )
        turn_budget -= first_kept

        second_limit = _call_budget(turn_budget, 1, per_call)
        # The small first call leaves nearly the whole budget for the second.
        self.assertEqual(second_limit, 990)

    def test_single_call_is_still_bound_by_the_per_call_cap(self):
        self.assertEqual(_call_budget(150_000, 1, 120_000), 120_000)

    def test_zero_turn_budget_falls_back_to_the_per_call_cap(self):
        self.assertEqual(_call_budget(0, 7, 120_000), 120_000)


if __name__ == "__main__":
    unittest.main()
