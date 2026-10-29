"""Safety contracts for the manual local pilot; no live service calls."""

from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pilot
from test_pipeline import CONFIG
from policy import Blocked


class PilotTests(unittest.TestCase):
    """Keep credentials private, tokens scoped and manual execution fail-closed."""

    def test_private_file_and_no_overwrite(self):
        """Credentials are mode 600 and cannot overwrite an existing file."""
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "private" / "credentials.json"
            pilot.write_private(target, {"CODEX_API_KEY": "fixture"})
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(pilot.private_file(target))["CODEX_API_KEY"], "fixture")
            with self.assertRaises(FileExistsError):
                pilot.write_private(target, {})
            target.chmod(0o644)
            with self.assertRaises(Blocked):
                pilot.private_file(target)

    def test_private_file_rejects_symlink_and_repository(self):
        """Do not follow credential links or write credential material into Git."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            pilot.write_private(path / "secret", {})
            (path / "link").symlink_to(path / "secret")
            with self.assertRaises(OSError):
                pilot.private_file(path / "link")
            with patch.object(pilot, "ROOT", path):
                with self.assertRaises(Blocked):
                    pilot.private_file(path / "secret")
                with self.assertRaises(Blocked):
                    pilot.write_private(path / "new", {})

    def test_capture_requires_real_terminal_before_prompt(self):
        """Never let getpass fall back to echoed input in an unattended shell."""
        with patch.object(sys.stdin, "isatty", return_value=False), patch.object(pilot.getpass, "getpass") as prompt:
            with self.assertRaises(Blocked):
                pilot.capture_secrets("/unused")
            prompt.assert_not_called()

    def test_credentials_allowlist_and_exactly_one_claude_method(self):
        """Personal GitHub tokens and ambiguous provider credentials are rejected."""
        valid = {"CODEX_API_KEY": "fixture", "CLAUDE_CODE_OAUTH_TOKEN": "fixture"}
        for data, accepted in ((valid, True), ({**valid, "GH_TOKEN": "owner"}, False),
                               ({**valid, "ANTHROPIC_API_KEY": "another"}, False)):
            with self.subTest(accepted=accepted), patch.object(pilot, "private_file", return_value=json.dumps(data).encode()):
                if accepted:
                    self.assertEqual(pilot.read_secrets("/unused", CONFIG), valid)
                else:
                    with self.assertRaises(Blocked):
                        pilot.read_secrets("/unused", CONFIG)

    def test_preflight_cannot_enable_merge_or_schedules(self):
        """Multiple independent merge/schedule guards refuse unsafe pilot settings."""
        config = deepcopy(CONFIG)
        config["merge_enabled"] = True
        def owner(path):
            """Supply unsafe settings without any live requests."""
            if "/branches/" in path:
                return {"commit": {"sha": "a" * 40}}
            if path.split("?")[0].endswith("variables"):
                return {"variables": [{"name": "AUTOBOT_ENABLED", "value": "true"}]}
            if "/actions/runs" in path:
                return {"workflow_runs": []}
            return {"allow_auto_merge": True}
        settings = {"app_id": 1, "private_key_path": "/fixture", "credentials_file": "/fixture"}
        with patch.object(pilot, "private_file", return_value=b"{}"), patch.object(pilot, "read_secrets", return_value={}), \
                patch.object(pilot, "owner_api", side_effect=owner), \
                patch.object(pilot.subprocess, "run") as run, patch.object(pilot.subprocess, "check_output", side_effect=["a" * 40, b""]):
            run.return_value.returncode = 0
            problems = pilot.preflight(settings, config)
        self.assertIn("merge_enabled must be false", problems)
        self.assertIn("GitHub auto-merge must remain disabled", problems)
        self.assertIn("scheduled Autobot execution must remain explicitly disabled", problems)

    def test_blocked_preflight_never_mints_token(self):
        """An unready checkout cannot enter the write-capable controller."""
        with tempfile.TemporaryDirectory() as directory, patch.object(pilot, "DIRECTORY", Path(directory)), \
                patch.object(pilot, "preflight", return_value=["not ready"]), patch.object(pilot, "installation_token") as mint:
            with self.assertRaises(Blocked):
                pilot.advance({}, CONFIG, 7)
            mint.assert_not_called()

    def test_temporary_token_revoked_after_controller_failure(self):
        """Limit credential lifetime even when the controller returns failure."""
        with tempfile.TemporaryDirectory() as directory, patch.object(pilot, "DIRECTORY", Path(directory)), \
                patch.object(pilot, "preflight", return_value=[]), patch.object(pilot, "read_secrets", return_value={"CODEX_API_KEY": "fixture"}), \
                patch.object(pilot, "installation_token", return_value="temporary"), patch.object(pilot, "revoke") as revoke, \
                patch.object(pilot.subprocess, "run") as run:
            run.return_value.returncode = 1
            result = pilot.advance({"credentials_file": "/fixture", "agent_image": "image", "execution_image": "image"}, CONFIG, 7)
            self.assertEqual(result, 1)
            environment = run.call_args.kwargs["env"]
            self.assertEqual(environment["AUTOBOT_ISSUE"], "7")
            self.assertEqual(environment["GH_TOKEN"], "temporary")
            revoke.assert_called_once_with("temporary")

    def test_app_token_narrows_repository_and_revokes_wrong_scope(self):
        """Request one repository and reject a response granting broader access."""
        config = deepcopy(CONFIG)
        responses = [{"slug": config["bot_login"].removesuffix("[bot]")},
                     {"id": 42, "repository_selection": "selected", "account": {"login": "cylewitruk"}},
                     {"token": "temporary", "permissions": {**pilot.PERMISSIONS, "metadata": "read"}},
                     {"total_count": 2, "repositories": [{"full_name": config["repository"]}, {"full_name": "another/repo"}]}]
        with patch.object(pilot, "private_file", return_value=b"fixture"), patch.object(pilot.subprocess, "run") as run, \
                patch.object(pilot, "api", side_effect=responses) as api, patch.object(pilot, "revoke") as revoke:
            run.return_value.returncode = 0
            run.return_value.stdout = b"signature"
            with self.assertRaises(Blocked):
                pilot.installation_token({"app_id": 1, "private_key_path": "/fixture"}, config)
            self.assertEqual(api.call_args_list[2].args[3]["repositories"], ["stacks-core-autobot"])
            self.assertEqual(api.call_args_list[2].args[3]["permissions"], pilot.PERMISSIONS)
            revoke.assert_called_once_with("temporary")
