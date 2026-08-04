import unittest

from mcp_completion.agent_eval import _clamp_tool_result
from mcp_completion.schema import ImageContent, TextContent


class ClampToolResultTests(unittest.TestCase):
    def test_small_result_passes_through_untouched(self):
        content = [TextContent(type="text", text="ok")]
        clamped, dropped = _clamp_tool_result(content, 1000)
        self.assertEqual(dropped, 0)
        self.assertIs(clamped, content)

    def test_oversized_result_is_clipped_to_the_budget(self):
        content = [TextContent(type="text", text="x" * 5000)]
        clamped, dropped = _clamp_tool_result(content, 1000)

        self.assertEqual(dropped, 4000)
        self.assertEqual(len(clamped[0].text), 1000)
        self.assertIn("truncated", clamped[-1].text)
        self.assertIn("5000", clamped[-1].text)

    def test_budget_is_shared_across_multiple_text_parts(self):
        content = [
            TextContent(type="text", text="a" * 800),
            TextContent(type="text", text="b" * 800),
        ]
        clamped, dropped = _clamp_tool_result(content, 1000)

        kept = sum(
            len(part.text)
            for part in clamped[:-1]
            if isinstance(part, TextContent)
        )
        self.assertEqual(kept, 1000)
        self.assertEqual(dropped, 600)

    def test_non_text_content_survives_clamping(self):
        content = [
            TextContent(type="text", text="x" * 5000),
            ImageContent(type="image", data="zzz", mimeType="image/png"),
        ]
        clamped, dropped = _clamp_tool_result(content, 100)

        self.assertTrue(
            any(isinstance(part, ImageContent) for part in clamped),
            "image content must not be silently dropped",
        )
        self.assertEqual(dropped, 4900)

    def test_zero_limit_disables_clamping(self):
        content = [TextContent(type="text", text="x" * 5000)]
        clamped, dropped = _clamp_tool_result(content, 0)
        self.assertEqual(dropped, 0)
        self.assertIs(clamped, content)


if __name__ == "__main__":
    unittest.main()
