"""Structural checks for the QML model-token period wiring.

Main.qml and Panel.qml depend on a running Quickshell/Omarchy session, so these
checks pin the source-level contract without loading private runtime records.
"""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).parent
MAIN_PATH = ROOT / "Main.qml"
PANEL_PATH = ROOT / "Panel.qml"


class TestQmlModelPeriods(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = MAIN_PATH.read_text(encoding="utf-8")
        cls.panel = PANEL_PATH.read_text(encoding="utf-8")

    @staticmethod
    def block(source: str, start_marker: str, end_marker: str) -> str:
        start = source.index(start_marker)
        end = source.index(end_marker, start)
        return source[start:end]

    def test_period_options_are_exactly_the_four_public_choices(self):
        pairs = re.findall(
            r'\{\s*id:\s*"(today|7d|month|all)"\s*,\s*label:\s*"([^"]+)"\s*\}',
            self.panel,
        )
        self.assertEqual(
            pairs,
            [("today", "Today"), ("7d", "7 days"), ("month", "1 month"), ("all", "All time")],
        )
        self.assertIn('property string selectedModelPeriodId: "today"', self.panel)

    def test_sidecar_is_watched_and_never_becomes_a_provider(self):
        self.assertIn('".model-history.json"', self.main)
        self.assertRegex(
            self.main,
            r'if\s*\(name\s*===\s*"\.model-history\.json"\)\s*continue',
        )
        sidecar = self.block(self.main, "id: modelHistoryFile", "\n  }")
        self.assertIn("path: root.modelHistoryPath", sidecar)
        self.assertIn("watchChanges: true", sidecar)
        self.assertIn("onFileChanged: reload()", sidecar)
        self.assertIn("parseModelHistory(text())", sidecar)

    def test_history_collector_starts_only_after_all_primary_collectors(self):
        pending = self.block(self.main, "function checkPendingUpdate()", "\n  function hermesWanted")
        self.assertIn("modelHistoryProcess.running", pending)
        self.assertRegex(
            pending,
            r"!updateProcess\.running\s*&&\s*!hermesProcess\.running\s*&&\s*!antigravityProcess\.running",
        )
        self.assertIn("modelHistoryRequested", pending)
        self.assertLess(pending.index("modelHistoryProcess.command ="), pending.index("modelHistoryProcess.running = true"))
        self.assertIn("model-history-collector.py", self.main)
        self.assertIn("modelHistoryRequested = true", self.main)

    def test_model_rows_are_not_silently_limited_to_four_and_shares_are_safe(self):
        model_rows = self.block(self.panel, "function modelRows(p)", "\n  function modelTooltip")
        self.assertNotIn("slice(0, 4)", model_rows)
        self.assertIn("return rows", model_rows)
        self.assertIn("function modelShare", self.panel)
        share = self.block(self.panel, "function modelShare", "\n  // Only speaks up")
        self.assertIn("isFinite", share)
        self.assertIn("root.modelShare(modelData, root.models)", self.panel)

    def test_models_have_explicit_unavailable_copy_and_antigravity_never_uses_quota(self):
        for phrase in (
            "documented Antigravity CLI",
            "quota/credit status",
            "historical model-token usage",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.panel)
        source = self.block(self.panel, "function modelUsageSourceFor", "\n  function modelRows")
        self.assertIn('providerId === "hermes"', source)
        self.assertIn("modelUsage/modelUsageByPeriod", source)
        self.assertIn("quota/credit status", source)
        self.assertIn("Per-model token source unavailable", self.panel)

    def test_period_data_propagates_and_hermes_routes_use_provider_route_keys(self):
        self.assertIn("modelUsageByPeriod", self.main)
        display = self.block(self.main, "function displayProvider(record)", "\n  function setting")
        snapshot = self.block(self.main, "function providerSnapshot(record)", "\n  function localSnapshot")
        aggregate = self.block(self.main, "function aggregateSnapshots(snapshots)", "\n  // Snapshots keep")
        self.assertIn("modelUsageByPeriod", display)
        self.assertIn("modelUsageByPeriod", snapshot)
        self.assertIn("modelUsageByPeriod", aggregate)
        self.assertIn("modelHistoryPeriodsFor(String(record.id))", snapshot)
        self.assertRegex(self.main, r'String\(providerId\)\s*\+\s*":"\s*\+\s*String\(route\.id\)')


if __name__ == "__main__":
    unittest.main()
