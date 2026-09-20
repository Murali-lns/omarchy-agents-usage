"""Structural checks for the usage summary cards and daily activity chart.

Main.qml and Panel.qml depend on a running Quickshell/Omarchy session, so these
checks pin the source-level contract without loading private runtime records.
"""

from pathlib import Path
import unittest


ROOT = Path(__file__).parent
PANEL_PATH = ROOT / "Panel.qml"


class TestQmlUsageSummary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.panel = PANEL_PATH.read_text(encoding="utf-8")

    @staticmethod
    def block(source: str, start_marker: str, end_marker: str) -> str:
        start = source.index(start_marker)
        end = source.index(end_marker, start)
        return source[start:end]

    def test_today_card_reads_the_authoritative_bucket_with_observed_fallback(self):
        today = self.block(self.panel, "function todayTokenTotal", "\n  // The Last 7 Days card")
        self.assertIn("source.todayTotalTokens", today)
        self.assertIn("recentDays", today)
        self.assertIn("root.todayDate()", today)
        self.assertIn("messageCount", today)
        self.assertIn("return 0", today)

    def test_week_card_sums_observed_daily_buckets_without_prorating(self):
        week = self.block(self.panel, "function weekTokenTotal", "\n  function dayOfMonth")
        self.assertIn("messageCount", week)
        self.assertIn("safeTokenNumber", week)
        self.assertNotIn("totalTokens", week)
        self.assertNotIn("totalPrompts", week)

    def test_summary_cards_and_daily_chart_are_wired(self):
        section = self.block(self.panel, "id: usageSection", "// ---------- Models")
        self.assertIn("root.activeHermesSource", section)
        self.assertIn('title: "Today"', section)
        self.assertIn('title: "Last 7 Days"', section)
        self.assertIn("usageSection.todayTokens", section)
        self.assertIn("usageSection.weekTokens", section)
        self.assertIn('"DAILY ACTIVITY"', section)
        self.assertIn('usageSection.days.length + " days"', section)
        # By date, not by position: a window that stops short of today must
        # not light up the last column.
        self.assertIn('String(modelData.date || "") === root.todayDate()', section)
        self.assertIn("DayColumn {", section)
        self.assertIn("component DayColumn", self.panel)
        self.assertIn("component UsageCard", self.panel)
        self.assertNotIn("component DayRow", self.panel)

    def test_cards_carry_the_observed_subtotal_note(self):
        card = self.block(self.panel, "component UsageCard", "\n  // One column of the daily activity chart")
        self.assertIn("Partial · observed", card)
        self.assertIn('"TOKENS"', card)


if __name__ == "__main__":
    unittest.main()
