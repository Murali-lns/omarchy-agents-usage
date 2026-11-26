"""Checks for the account manager: Omarchy's `omarchy agent account` commands
driven from the panel.

Main.qml and Panel.qml depend on a running Quickshell/Omarchy session, so the
QML side is pinned at the source level, like the other test_qml_* files. The
one assumption that lives outside this repository, that Omarchy's account
commands work under the supervisor's minimal environment, is exercised for
real when Omarchy is installed.
"""

from pathlib import Path
import os
import re
import subprocess
import unittest


ROOT = Path(__file__).parent
PANEL_PATH = ROOT / "Panel.qml"
MAIN_PATH = ROOT / "Main.qml"
OMARCHY_BIN = Path("/usr/share/omarchy/bin")


def block(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


class TestAccountCommands(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = MAIN_PATH.read_text(encoding="utf-8")
        cls.panel = PANEL_PATH.read_text(encoding="utf-8")

    def test_account_commands_run_under_the_supervisor(self):
        """Use, rename and mode spawn like every collector: supervised, with the
        cleared minimal environment and a deadline that reaps the group."""
        process = block(self.main, "id: accountProcess", "function accountsSupported")
        self.assertIn("clearEnvironment: true", process)
        self.assertIn("environment: root.minimalChildEnv", process)
        pump = block(self.main, "function pumpAccountQueue", "// ------------------------------------------------------------- providers")
        self.assertIn("accountProcess.command = root.supervisedCommand(next.argv)", pump)
        self.assertIn("accountExitGuard.active = true", pump)
        self.assertIn("onTriggered: root.killLeaked(accountProcess, accountExitGuard)", self.main)

    def test_command_arguments_are_validated_before_the_command_line(self):
        run = block(self.main, "function runAccountCommand", "function pumpAccountQueue")
        # Only the three packaged commands, each through the trusted interpreter.
        self.assertIn('if (kind === "use")', run)
        self.assertIn('else if (kind === "rename")', run)
        self.assertIn('else if (kind === "mode")', run)
        self.assertIn("if (!valid ||", run)
        self.assertIn('[root.binBash, root.accountCommandDir + "/omarchy-agent-account-" + kind', run)
        self.assertIn("Number(args[1]) >= 50 && Number(args[1]) <= 100", run)
        self.assertIn("maxQueuedAccountCommands", run)

    def test_account_id_pattern_rejects_option_and_shell_shaped_ids(self):
        pattern = re.search(r"/\^\[A-Za-z0-9\]\[A-Za-z0-9\._-\]\{0,63\}\$/", self.main)
        self.assertIsNotNone(pattern, "isAccountId must keep its anchored slug pattern")
        slug = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
        for good in ("main", "work", "side-2", "a.b_c"):
            self.assertRegex(good, slug)
        for bad in ("--reauth", "-x", "a b", "a;rm", "$(id)", "", "x" * 65):
            self.assertNotRegex(bad, slug)

    def test_rename_label_pattern_rejects_control_characters(self):
        self.assertIn(r"/^[^\u0000-\u001f\u007f]{1,40}$/", self.main)
        label = re.compile(r"^[^\x00-\x1f\x7f]{1,40}$")
        self.assertRegex("Work", label)
        self.assertRegex("Équipe Limops", label)
        for bad in ("", "a\nb", "tab\there", "x" * 41):
            self.assertNotRegex(bad, label)

    def test_record_accounts_are_bounded_and_scoped(self):
        accounts = block(self.main, "function accountList", "function accountSwitchValue")
        self.assertIn("if (!accountsSupported(providerId) || !Array.isArray(raw)) return []", accounts)
        self.assertIn("out.length < root.maxAccounts", accounts)
        self.assertIn("!isAccountId(account.id)", accounts)
        self.assertIn("account.limits.slice(0, 8)", accounts)
        switch = block(self.main, "function accountSwitchValue", "function runAccountCommand")
        self.assertIn("Math.max(50, Math.min(100, threshold))", switch)
        self.assertIn('raw.mode === "auto" ? "auto" : "manual"', switch)
        display = block(self.main, "function displayProvider", "function setting(")
        self.assertIn("accounts: accountList(record.accounts, providerId)", display)
        self.assertIn("accountSwitch: accountSwitchValue(record.accountSwitch)", display)


class TestAccountPanel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.panel = PANEL_PATH.read_text(encoding="utf-8")

    def test_sign_in_opens_a_terminal_with_checked_arguments(self):
        command = block(self.panel, "function signInCommand", "function openSignIn")
        self.assertIn("if (!usage.accountsSupported(providerId)) return \"\"", command)
        self.assertIn('reauthId !== ":primary" && !usage.isAccountId(reauthId)', command)
        self.assertIn('"omarchy-launch-floating-terminal-with-presentation "', command)
        self.assertIn('["omarchy", "agent", "account", "add"]', command)

    def test_accounts_section_needs_two_accounts_and_add_is_always_offered(self):
        section = block(self.panel, "id: accountsSection", "// ---------- Usage ----------")
        self.assertIn("visible: root.accounts.length >= 2", section)
        self.assertIn("AccountRow {", section)
        self.assertIn("root.setAccountSwitch(root.accountSwitch.mode, root.accountSwitch.threshold - 5)", section)
        self.assertIn("root.setAccountSwitch(root.accountSwitch.mode, root.accountSwitch.threshold + 5)", section)
        hero = block(self.panel, "id: headerActions", "// ---------- Provider switch ----------")
        self.assertIn('text: "+ Account"', hero)
        self.assertIn("visible: root.canAddAccount", hero)

    def test_account_text_is_plain(self):
        """Account labels and emails come from a registry file: never rich text."""
        row = block(self.panel, "component AccountRow", "// One harness in the settings list")
        texts = row.count("Text {")
        self.assertGreater(texts, 0)
        self.assertEqual(texts, row.count("textFormat: Text.PlainText"))

    def test_rename_commits_on_enter_and_cancels_on_escape_or_blur(self):
        row = block(self.panel, "component AccountRow", "// One harness in the settings list")
        self.assertIn("onAccepted: accountRow.finishRename(true)", row)
        self.assertIn("onActiveFocusChanged: if (!activeFocus) accountRow.finishRename(false)", row)
        self.assertIn("accountRow.finishRename(false)\n          event.accepted = true", row)


@unittest.skipUnless((OMARCHY_BIN / "omarchy-agent-account-mode").is_file(), "Omarchy not installed")
class TestOmarchyUnderMinimalEnv(unittest.TestCase):
    """The supervisor clears the environment down to HOME and OMARCHY_PATH;
    Omarchy's account commands must still resolve their helpers."""

    def test_read_only_mode_query_succeeds(self):
        env = {"HOME": os.environ["HOME"], "OMARCHY_PATH": "/usr/share/omarchy"}
        result = subprocess.run(
            ["/usr/bin/bash", str(OMARCHY_BIN / "omarchy-agent-account-mode"), "claude"],
            env=env, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"\d+%")


if __name__ == "__main__":
    unittest.main()
