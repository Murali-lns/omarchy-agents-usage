"""Checks for the Claude Code API equiv. card and its Anthropic price list.

The price list is static data shipped with the plugin. A typo in it silently
skews every estimate, so its values are checked against the multipliers
Anthropic documents for prompt caching. Panel.qml depends on a running
Quickshell/Omarchy session, so its wiring is pinned at source level, as the
other QML checks do.
"""

from datetime import date
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).parent
PRICES_PATH = ROOT / "anthropic-prices.js"
PANEL_PATH = ROOT / "Panel.qml"

PRICE_FIELDS = ("input", "cacheWrite5m", "cacheWrite1h", "cacheRead", "output")
MODEL_LINE = re.compile(r'^\s*"(?P<id>[a-z0-9-]+)": \{ (?P<body>[^}]*) \},?$')


def load_prices():
    text = PRICES_PATH.read_text(encoding="utf-8")
    models = {}
    for line in text.splitlines():
        match = MODEL_LINE.match(line)
        if not match:
            continue
        fields = dict(re.findall(r"(\w+): ([0-9.]+)", match.group("body")))
        models[match.group("id")] = {key: float(value) for key, value in fields.items()}
    return text, models


class TestAnthropicPriceList(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text, cls.models = load_prices()

    def test_is_a_static_library_with_provenance(self):
        self.assertTrue(self.text.startswith(".pragma library\n"))
        self.assertIn('var source = "https://platform.claude.com/docs/en/about-claude/pricing"', self.text)
        checked = re.search(r'var checkedAt = "(\d{4}-\d{2}-\d{2})"', self.text)
        self.assertIsNotNone(checked)
        date.fromisoformat(checked.group(1))

    def test_every_model_has_five_positive_prices(self):
        self.assertGreaterEqual(len(self.models), 10)
        for model_id, price in self.models.items():
            with self.subTest(model=model_id):
                self.assertTrue(model_id.startswith("claude-"))
                self.assertNotRegex(model_id, r"-\d{8}$")
                self.assertEqual(sorted(price), sorted(PRICE_FIELDS))
                for field in PRICE_FIELDS:
                    self.assertGreater(price[field], 0)

    def test_cache_writes_follow_the_documented_multipliers(self):
        # 5-minute writes cost 1.25x base input and 1-hour writes 2x, on every model.
        for model_id, price in self.models.items():
            with self.subTest(model=model_id):
                self.assertAlmostEqual(price["cacheWrite5m"], price["input"] * 1.25)
                self.assertAlmostEqual(price["cacheWrite1h"], price["input"] * 2)
                self.assertLess(price["cacheRead"], price["input"])
                self.assertGreater(price["output"], price["input"])

    def test_lists_the_models_claude_code_records_carry(self):
        for model_id in ("claude-opus-5-5", "claude-opus-5", "claude-opus-4-8", "claude-sonnet-5",
                         "claude-sonnet-4-6", "claude-haiku-4-5", "claude-haiku-5-5"):
            self.assertIn(model_id, self.models)


class TestApiCostCard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.panel = PANEL_PATH.read_text(encoding="utf-8")

    @staticmethod
    def block(source: str, start_marker: str, end_marker: str) -> str:
        start = source.index(start_marker)
        end = source.index(end_marker, start)
        return source[start:end]

    def test_prices_are_imported_not_read_at_runtime(self):
        self.assertIn('import "anthropic-prices.js" as AnthropicPrices', self.panel)
        self.assertNotIn("FileView", self.panel)

    def test_estimate_bounds_cache_writes_and_reports_unpriced_tokens(self):
        estimate = self.block(self.panel, "function apiCostEstimate", "\n  function formatUsd")
        self.assertIn("source.modelUsage", estimate)
        self.assertIn("price.cacheWrite5m", estimate)
        self.assertIn("price.cacheWrite1h", estimate)
        self.assertIn("unpricedTokens", estimate)
        self.assertIn("parts.hasBreakdown", estimate)
        lookup = self.block(self.panel, "function anthropicPriceFor", "\n  }")
        self.assertIn(r"/-\d{8}$/", lookup)

    def test_card_is_limited_to_the_claude_code_tab(self):
        section = self.block(self.panel, "id: usageSection", "// ---------- Models")
        self.assertIn('root.provider.providerId === "claude"', section)
        card = self.block(section, "id: apiCostCard", "breakdownMaxWidth")
        self.assertIn('title: "API equiv."', card)
        self.assertIn("est.pricedTokens > 0", card)
        self.assertIn("root.apiCostRows(est)", card)

    def test_hover_rows_cover_buckets_savings_and_price_date(self):
        rows = self.block(self.panel, "function apiCostRows", "\n  // Only speaks up")
        for label in ('"Cache read"', '"Cache write"', '"In"', '"Out"', '"Without cache"', '"Saved"',
                      '"Unpriced"', '"Prices"'):
            self.assertIn(label, rows)
        self.assertIn("AnthropicPrices.checkedAt", rows)


if __name__ == "__main__":
    unittest.main()
