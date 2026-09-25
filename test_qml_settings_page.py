"""Structural checks for the panel settings view, default tab, and harness switches.

Main.qml and Panel.qml depend on a running Quickshell/Omarchy session, so these
checks pin the source-level contract of the settings page without loading it.
"""

from pathlib import Path
import json
import unittest


ROOT = Path(__file__).parent
PANEL_PATH = ROOT / "Panel.qml"
MAIN_PATH = ROOT / "Main.qml"
MANIFEST_PATH = ROOT / "manifest.json"


class TestQmlSettingsPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.panel = PANEL_PATH.read_text(encoding="utf-8")
        cls.main = MAIN_PATH.read_text(encoding="utf-8")
        cls.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    @staticmethod
    def block(source: str, start_marker: str, end_marker: str) -> str:
        start = source.index(start_marker)
        end = source.index(end_marker, start)
        return source[start:end]

    def test_icon_resolution_tries_png_before_svg(self):
        """Raster marks (Hermes) and vector marks share one candidate chain."""
        resolver = self.block(self.panel, "function iconCandidatesForProvider",
                              "\n  // Nothing to report")
        self.assertIn("colorLuminance(surfaceColor || Color.background) >= 0.5", resolver)
        self.assertLess(resolver.index('"-light.png"'), resolver.index('"-light.svg"'))
        self.assertLess(resolver.index('base + ".png"'), resolver.index('base + ".svg"'))

    def test_settings_view_is_reachable_and_reversible(self):
        """The gear and the `s` key open settings; Esc and Back leave it."""
        self.assertIn('text: "\\uf013"', self.panel)
        self.assertIn("onClicked: root.settingsView = !root.settingsView", self.panel)
        self.assertIn('else if (t === "s" || t === "S") root.settingsView = !root.settingsView',
                      self.panel)
        self.assertIn("onCloseRequested: if (root.settingsView) root.settingsView = false",
                      self.panel)
        self.assertIn("onClicked: root.settingsView = false", self.panel)
        self.assertIn('text: "SETTINGS"', self.panel)
        self.assertIn('text: "HARNESSES"', self.panel)
        self.assertIn('text: "DEFAULT TAB ON OPEN"', self.panel)

    def test_settings_writes_go_through_the_widget_settings_api(self):
        """Persistence must reuse the shell's own inline-entry writer."""
        persist = self.block(self.panel, "function persistSettings",
                             "\n  // The harness list mixes")
        self.assertIn("root.bar.shell.updateEntryInline(root.moduleName, base)", persist)
        self.assertIn("root.settingsOverride = overrides", persist)

    def test_harness_switch_writes_the_providers_map(self):
        toggler = self.block(self.panel, "function setHarnessEnabled",
                             "\n  readonly property string defaultProviderId")
        self.assertIn("updated.enabled = enabled === true", toggler)
        self.assertIn("root.persistSettings({ providers: next })", toggler)
        self.assertIn("root.effectiveSettings.providers", toggler)

    def test_default_provider_is_applied_on_open_and_read_from_settings(self):
        opened = self.block(self.panel, "onOpenedChanged: if (opened) {",
                            "\n  Main {")
        self.assertIn("root.applyDefaultProvider()", opened)
        applier = self.block(self.panel, "function applyDefaultProvider",
                             "\n\n  // Countdowns and")
        self.assertIn("root.selectedProviderId = wanted", applier)
        self.assertIn('String(root.settingValue("defaultProvider", ""))', self.panel)

    def test_disabling_every_harness_cannot_strand_the_settings_view(self):
        """The bar icon must stay reachable while any harness is switched off."""
        self.assertIn("visible: providers.length > 0 || root.hasDisabledHarnesses", self.panel)
        self.assertIn("readonly property bool hasDisabledHarnesses", self.panel)

    def test_main_pins_the_default_provider_first(self):
        pin = self.block(self.main, "// The configured default provider is pinned first",
                         "return result")
        self.assertIn('String(root.setting("defaultProvider", ""))', pin)
        self.assertIn("result.splice(pin, 1)[0]", pin)
        self.assertIn("result.unshift(movedProvider)", pin)

    def test_main_lists_known_provider_ids_for_the_settings_page(self):
        known = self.block(self.main, "readonly property var knownProviderIds",
                           "\n  function providerEnabled")
        self.assertIn("root.isRetiredProviderId(id)", known)
        self.assertIn("out.push(id)", known)

    def test_icon_walk_advances_only_within_the_same_walk(self):
        """A stale deferred step must not skip the next provider's walk.

        The advance is deferred (binding-loop detector) and guarded by the
        candidate key and index captured at error time; without the guard a
        pending step from the previous provider hijacked the next walk and
        stranded the mark on its dead last candidate.
        """
        self.assertIn("var keyAtError = heroMark.candidatesKey", self.panel)
        self.assertIn("if (heroMark.candidatesKey !== keyAtError) return", self.panel)
        self.assertIn("if (heroMark.candidateIndex !== indexAtError) return", self.panel)
        self.assertIn("var keyAtError = candidatesKey", self.panel)
        self.assertIn("if (candidatesKey !== keyAtError) return", self.panel)
        self.assertIn("if (candidateIndex !== indexAtError) return", self.panel)

    def test_manifest_declares_the_default_provider_setting(self):
        defaults = self.manifest["barWidget"]["defaults"]
        self.assertEqual(defaults["defaultProvider"], "")
        schema_keys = [entry.get("key") for entry in self.manifest["barWidget"].get("schema", [])]
        self.assertIn("defaultProvider", schema_keys)


if __name__ == "__main__":
    unittest.main()
