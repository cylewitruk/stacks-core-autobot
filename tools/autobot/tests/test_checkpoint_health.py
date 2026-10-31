"""Checkpoint warnings are visible, idempotent and separate from recovery state."""

from copy import deepcopy
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import controller as controller_module
from checkpoint_health import FAILED, MARKER, RECOVERED, sync_checkpoint_health
from checkpoints import CheckpointError, DIAGNOSTICS
from controller import Controller
from github import ATTENTION_LABEL, GitHub
from test_autonomy import decision
from test_pipeline import CONFIG, FakeAgents, FakeGitHub, FakeWorkspace


class CheckpointHealthTests(unittest.TestCase):
    """Report failures without writing or trusting any replacement checkpoint."""

    def setUp(self):
        """Prepare an idle task so recovery cannot spend another model call."""
        self.config = deepcopy(CONFIG)
        self.gh, self.agents = FakeGitHub(self.config), FakeAgents()
        self.gh.comments = []
        self.agents.qc_reports = [decision('escalate')]
        Controller(self.gh, self.agents, self.config, FakeWorkspace).advance(7)

    def notices(self):
        """Find only the configured App's separate health comments."""
        return [c for c in self.gh.comments if c['body'].startswith(MARKER)
                and c['user']['login'] == self.config['bot_login']]

    def run_main(self):
        """Exercise the production entry point with fake model and GitHub transports."""
        with patch.object(controller_module, 'GitHub', return_value=self.gh), \
                patch.object(controller_module, 'Agents', return_value=self.agents), \
                patch.dict(os.environ, {'AUTOBOT_ISSUE': '7'}):
            controller_module.main()

    def test_checkpoint_failure_reports_once_then_clears_after_idle_recovery(self):
        """Failed reads preserve authoritative state and display a warning until a verified run."""
        before = deepcopy(self.gh.artifacts)
        summary = deepcopy(self.gh.comments[0])
        labels = deepcopy(self.gh.issue['labels'])
        original = self.gh.repo

        def fail_checkpoint(path, method='GET', data=None):
            """Keep health APIs reachable while checkpoint reads are unavailable."""
            if '/artifacts/' in path and method == 'GET':
                raise RuntimeError('private-token-fixture')
            return original(path, method, data)

        for _ in range(2):
            with patch.object(self.gh, 'repo', side_effect=fail_checkpoint), \
                    patch('sys.stderr', new_callable=io.StringIO) as output, self.assertRaises(SystemExit):
                self.run_main()
            self.assertNotIn('private-token-fixture', output.getvalue())
            self.assertEqual(self.gh.artifacts, before)
            self.assertEqual(self.gh.comments[0], summary)
            self.assertEqual(len(self.notices()), 1)
            self.assertEqual(self.notices()[0]['body'], FAILED + '\nDiagnostic: ' + CheckpointError('artifact_request').public_detail() + '\n')
            self.assertEqual(self.gh.issue['labels'], labels + [{'name': ATTENTION_LABEL}])
        self.run_main()
        self.assertEqual(self.gh.artifacts, before)
        self.assertEqual(self.gh.comments[0], summary)
        self.assertEqual(self.gh.issue['labels'], labels)
        self.assertEqual(self.notices()[0]['body'], RECOVERED)
        with patch.object(self.gh, 'repo', wraps=self.gh.repo) as repo:
            self.run_main()
        self.assertFalse([call for call in repo.call_args_list if len(call.args) > 1 and call.args[1] in ('POST', 'PATCH')])
        self.assertEqual(len(self.agents.calls), 1)
        self.assertEqual(self.gh.merges, 0)

    def test_checkpoint_failure_while_recording_another_error_is_reported(self):
        """A failed error-state save also gets a warning without replacing the audit artifact."""
        before = deepcopy(self.gh.artifacts)
        original = Controller.advance

        def fail_after_load(controller, number):
            """Read valid state, then simulate a non-checkpoint failure during execution."""
            original(controller, number)
            raise RuntimeError('private-model-error')

        with patch.object(Controller, 'advance', fail_after_load), \
                patch.object(Controller, 'suspend', side_effect=CheckpointError('write_unconfirmed')), \
                patch('sys.stderr', new_callable=io.StringIO) as output, self.assertRaises(SystemExit):
            self.run_main()
        self.assertIn(CheckpointError('write_unconfirmed').public_detail(), self.notices()[0]['body'])
        self.assertEqual(self.gh.artifacts, before)
        self.assertNotIn('private-', output.getvalue())

    def test_health_channels_fail_independently_without_leaking_errors(self):
        """A failure in one display API cannot prevent the other channel or mask checkpoint failure."""
        before = deepcopy(self.gh.artifacts)
        with patch.object(self.gh, 'sync_attention', side_effect=RuntimeError('private-token-fixture')), \
                patch('sys.stderr', new_callable=io.StringIO) as output:
            sync_checkpoint_health(self.gh, self.config, 7, failed=True)
        self.assertEqual(self.notices()[0]['body'], FAILED)
        self.assertNotIn('private-token-fixture', output.getvalue())
        with patch.object(self.gh, 'pages', side_effect=RuntimeError('private-token-fixture')), \
                patch('sys.stderr', new_callable=io.StringIO) as output:
            sync_checkpoint_health(self.gh, self.config, 7, failed=True)
        self.assertIn({'name': ATTENTION_LABEL}, self.gh.issue['labels'])
        self.assertNotIn('private-token-fixture', output.getvalue())
        with patch.object(self.gh, 'sync_attention', side_effect=RuntimeError('private-token-fixture')), \
                patch.object(self.gh, 'pages', side_effect=RuntimeError('private-token-fixture')), \
                patch('sys.stderr', new_callable=io.StringIO):
            sync_checkpoint_health(self.gh, self.config, 7, failed=True)
        self.assertEqual(self.gh.artifacts, before)

    def test_foreign_notice_is_not_adopted_and_normal_success_creates_no_notice(self):
        """Human comments cannot impersonate health notices; healthy tasks need no extra comment."""
        sync_checkpoint_health(self.gh, self.config, 7, failed=False)
        self.assertFalse(self.notices())
        self.gh.comment(MARKER + '\nHuman message', 'outsider')
        foreign = deepcopy(self.gh.comments[-1])
        sync_checkpoint_health(self.gh, self.config, 7, failed=True)
        self.assertEqual(self.gh.comments[-2], foreign)
        self.assertEqual(len(self.notices()), 1)

    def test_attention_adapter_preserves_stage_enrollment_and_human_labels(self):
        """Only the dedicated health label is added or removed, without replacing other labels."""
        gh = GitHub(self.config['repository'])
        current = [{'name': n} for n in ('autobot', 'bug', 'autobot:stage:plan')]
        with patch.object(gh, 'pages', side_effect=[current, []]), patch.object(gh, 'repo') as repo:
            gh.sync_attention(7, True)
        self.assertEqual(repo.call_args_list[0].args[0:2], ('labels', 'POST'))
        self.assertEqual(repo.call_args_list[1].args, ('issues/7/labels', 'POST', {'labels': [ATTENTION_LABEL]}))
        current.append({'name': ATTENTION_LABEL})
        with patch.object(gh, 'pages', return_value=current), patch.object(gh, 'repo') as repo:
            gh.sync_attention(7, True)
            repo.assert_not_called()
            gh.sync_attention(7, False)
        self.assertEqual(repo.call_args.args, ('issues/7/labels/autobot%3Aattention', 'DELETE'))


    def test_diagnostics_use_fixed_catalog_and_update_the_existing_notice(self):
        """Recognized causes are actionable; arbitrary exception data never reaches the issue."""
        before = deepcopy(self.gh.artifacts)
        for code, detail in DIAGNOSTICS.items():
            with self.subTest(code=code):
                error = CheckpointError(code)
                error.args = ('private-token-fixture',)
                error.__cause__ = RuntimeError('private-provider-fixture')
                sync_checkpoint_health(self.gh, self.config, 7, failed=True, error=error)
                self.assertEqual(len(self.notices()), 1)
                self.assertIn(f'Diagnostic: `{code}`: {detail}', self.notices()[0]['body'])
                self.assertNotIn('private-', self.notices()[0]['body'])
        for error in (CheckpointError('private-token-fixture'), RuntimeError('private-token-fixture')):
            sync_checkpoint_health(self.gh, self.config, 7, failed=True, error=error)
            self.assertNotIn('private-', self.notices()[0]['body'])
        self.assertEqual(self.gh.artifacts, before)
        sync_checkpoint_health(self.gh, self.config, 7, failed=False)
        self.assertEqual(self.notices()[0]['body'], RECOVERED)
