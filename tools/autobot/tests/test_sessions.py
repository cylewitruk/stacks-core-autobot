"""Retained sessions are scoped, explicit, recoverable, and never approval authority."""

from copy import deepcopy
import json
import os
import subprocess
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from agent_entry import command_for
from agents import Agents
from policy import Blocked, SessionFailure
from session_entry import SessionTurn
from sessions import prepare, SessionVolume
from test_pipeline import CONFIG


class SessionTests(unittest.TestCase):
    """Verify provider-independent lifecycle and provider-specific resume arguments."""

    def state(self):
        """Return enough authoritative identity to bind one cooperative loop."""
        return {"issue": 7, "base": "a" * 40, "requirements": {"body": "Improve guide"},
                "bundle_id": "bundle", "agent_image": "sha256:" + "a" * 64,
                "config_digest": "config", "plan_id": "plan", "design_reopens": 0}

    def test_scope_isolates_roles_issues_models_bases_and_reopened_designs(self):
        """A matching role resumes; changed authority or a fresh review cannot reuse it."""
        state = self.state()
        first = prepare(state, CONFIG, "claude", "plan", {})
        state["sessions"][first["role"]]["turn"] = 2
        self.assertEqual(prepare(state, CONFIG, "claude", "plan", {})["turn"], 2)
        for key, value in (("issue", 8), ("base", "b" * 40), ("requirements", {}),
                           ("bundle_id", "other"), ("agent_image", "other"),
                           ("config_digest", "other"), ("design_reopens", 1)):
            changed = {**deepcopy(state), key: value}
            fresh = prepare(changed, CONFIG, "claude", "plan", {})
            self.assertNotEqual(fresh["scope"], first["scope"])
            self.assertEqual(fresh["turn"], 0)
        reviewer = {"reviewer_identity": {"model": "claude-sonnet-4-6"}}
        self.assertNotEqual(prepare(state, CONFIG, "claude", "plan_qc", reviewer)["scope"], first["scope"])
        model = deepcopy(CONFIG)
        model["models"]["claude"] = "different-model"
        self.assertNotEqual(prepare(state, model, "claude", "plan", {})["scope"], first["scope"])
        for stage in ("intake", "local_review", "review"):
            self.assertIsNone(prepare(state, CONFIG, "claude", stage, {}))
        self.assertIsNone(prepare(state, CONFIG, "claude", "plan_qc", {"review_phase": "fresh"}))

    def test_provider_resume_uses_exact_identity_and_keeps_tools_restricted(self):
        """All configured models resume only their own explicit session; no latest-session lookup."""
        identifier = "12345678-1234-1234-1234-123456789abc"
        for provider in ("claude", "codex", "copilot"):
            request = {"provider": provider, "model": "chosen-model", "schema": {},
                       "session": {"scope": "a" * 64, "turn": 1},
                       "session_id": identifier, "resume_id": identifier}
            command = command_for(request, "schema", "output")
            self.assertNotIn("--last", command)
            self.assertNotIn("--continue", command)
            self.assertNotIn("--ephemeral", command)
            self.assertNotIn("--no-session-persistence", command)
            self.assertEqual(command[command.index("--model") + 1], "chosen-model")
            if provider == "codex":
                self.assertEqual(command[:3], ["codex", "exec", "resume"])
                self.assertIn('sandbox_mode="read-only"', command)
                self.assertIn("features.shell_tool=false", command)
                self.assertIn(identifier, command)
            elif provider == "claude":
                self.assertIn("--strict-mcp-config", command)
                self.assertEqual(command[command.index("--resume") + 1], identifier)
            else:
                self.assertIn("--resume=" + identifier, command)
                self.assertIn("--no-remote", command)
                self.assertIn("--disable-builtin-mcps", command)

    def prompt(self, **context):
        """Represent the stable instruction prefix and replaceable untrusted input."""
        return json.dumps({"instructions": "same", "response_schema": {},
                           "repository_snapshot": {"commit": "a" * 40}, "untrusted_context": context})

    def test_completed_turn_replays_and_followup_contains_only_context_delta(self):
        """A lost successful response replays locally; a revision appends changed keys."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = {"scope": "a" * 64, "turn": 0}
            first_prompt = self.prompt(plan="v1", stable="unchanged", obsolete=True)
            turn = SessionTurn(session, first_prompt, root)
            self.assertEqual(json.loads(turn.start()), json.loads(first_prompt))
            turn.finish({"decision": "revise"}, turn.identifier)
            turn.close()
            replay = SessionTurn(session, first_prompt, root)
            self.assertEqual(replay.cached, {"decision": "revise"})
            replay.close()
            revised = SessionTurn({**session, "turn": 1}, self.prompt(plan="v2", stable="unchanged"), root)
            delta = json.loads(revised.start())
            self.assertEqual(delta["context_update"], {"plan": "v2"})
            self.assertEqual(delta["removed_context_keys"], ["obsolete"])
            self.assertNotIn("instructions", delta)
            revised.finish({"decision": "approve"}, revised.identifier)
            revised.close()

    def test_ambiguous_turn_and_missing_transcript_fail_closed(self):
        """A partial provider turn is never silently replayed as a fresh request."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = {"scope": "a" * 64, "turn": 0}
            prompt = self.prompt(plan="v1")
            turn = SessionTurn(session, prompt, root)
            turn.start()
            turn.close()
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                SessionTurn(session, prompt, root)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "missing"):
                SessionTurn({"scope": "b" * 64, "turn": 1}, self.prompt(), Path(directory))

    def test_volume_setup_never_receives_provider_or_github_secrets(self):
        """Only named private storage reaches preparation; a missing resumed volume blocks."""
        with patch.dict(os.environ, {"GH_TOKEN": "owner", "CODEX_API_KEY": "provider"}), \
                patch("sessions.subprocess.run") as run:
            run.return_value.returncode = 0
            volume = SessionVolume("sha256:" + "a" * 64, {"scope": "b" * 64, "turn": 0})
            volume.ensure()
            for call in run.call_args_list:
                self.assertNotIn("GH_TOKEN", call.kwargs["env"])
                self.assertNotIn("CODEX_API_KEY", call.kwargs["env"])
            self.assertIn("--network=none", run.call_args.args[0])
            self.assertNotIn("type=bind", volume.mount())
            run.return_value.returncode = 1
            with self.assertRaises(SessionFailure):
                SessionVolume(volume.image, {"scope": "b" * 64, "turn": 1}).ensure()

    def test_failed_provider_transport_rotates_only_retained_sessions(self):
        """Nonzero exits, malformed output and timeouts signal recovery without exposing stderr."""
        environment = {"AUTOBOT_AGENT_IMAGE": "sha256:" + "a" * 64, "CODEX_API_KEY": "fixture", "PATH": "/usr/bin"}
        for retained in (False, True):
            for failure in (subprocess.CompletedProcess([], 1, "", "private-fixture"),
                            subprocess.CompletedProcess([], 0, "not-json", "private-fixture"),
                            subprocess.TimeoutExpired("provider", 600, output="private-fixture")):
                with self.subTest(retained=retained, failure=type(failure).__name__), \
                        patch.dict(os.environ, environment, clear=True), patch("agents.Snapshot"), \
                        patch("agents.SessionVolume"), patch("agents.subprocess.run") as run:
                    agents = Agents(CONFIG, archive_loader=lambda _: b"archive")
                    context = {"bundle_id": agents.bundle.identity, "repository_commit": "a" * 40}
                    if retained:
                        context["controller_session"] = {"scope": "b" * 64, "turn": 1}
                    run.side_effect = [failure, subprocess.CompletedProcess([], 0)]
                    expected = SessionFailure if retained else (Blocked, json.JSONDecodeError, subprocess.TimeoutExpired)
                    with self.assertRaises(expected) as raised:
                        agents.ask("codex", "plan", context)
                    if retained:
                        self.assertNotIn("private-fixture", str(raised.exception))
                    self.assertEqual(run.call_args.args[0][:3], ["docker", "rm", "-f"])

    @unittest.skipUnless(os.environ.get("AUTOBOT_TEST_AGENT_IMAGE"), "requires pinned agent Docker image")
    def test_private_volume_can_be_reopened_after_nonroot_ownership(self):
        """A second container can reuse a prepared volume without changing existing workloads."""
        scope = uuid.uuid4().hex + uuid.uuid4().hex
        volume = SessionVolume(os.environ["AUTOBOT_TEST_AGENT_IMAGE"], {"scope": scope, "turn": 0})
        try:
            volume.ensure()
            volume.session["turn"] = 1
            volume.ensure()
            result = volume.run(["run", "--rm", "--network=none", "--read-only", "--cap-drop=ALL",
                                 "--mount", volume.mount(), "--entrypoint", "python3", volume.image,
                                 "-c", "import os; open('/retained/probe', 'w').write('private'); print(os.getuid())"])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), b"10001")
        finally:
            volume.run(["volume", "rm", volume.name])
