"""Artifact checkpoint migration, provenance and interrupted-write recovery."""

from copy import deepcopy
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import controller as controller_module
from controller import Controller
from checkpoints import (AUDIT_MARKER, AUDIT_NAME, PLAN_MARKER, PLAN_NAME, MARKER,
                         STATE_PREFIX, CheckpointError, Checkpoints, legacy_state)
from github import GitHub
from policy import digest
from test_autonomy import decision
from test_pipeline import CONFIG, PLAN, FakeAgents, FakeGitHub, FakeWorkspace


class CheckpointTests(unittest.TestCase):
    """Keep artifact transport failures separate from execution and approval decisions."""

    def setUp(self):
        """Seed one enrolled issue without operator commands."""
        self.config = deepcopy(CONFIG)
        self.gh, self.agents = FakeGitHub(self.config), FakeAgents()
        self.gh.comments = []

    def controller(self):
        """Reconstruct a controller from durable GitHub data."""
        controller = Controller(self.gh, self.agents, self.config, FakeWorkspace)
        controller.issue = self.gh.issue
        controller.load(self.gh.comments)
        return controller

    def start(self):
        """Finish initial intake using fake models."""
        controller = self.controller()
        controller.advance(7)
        return controller

    def artifact(self, name=AUDIT_NAME):
        """Locate a fixture's stored artifact and its version history."""
        return next(value for value in self.gh.artifacts[7].values() if value['artifact']['name'] == name)

    def test_legacy_migration_preserves_approved_checkpoint_before_new_state(self):
        """Both old comment formats retain exact approvals and budgets as the first version."""
        for collapsed in (False, True):
            with self.subTest(collapsed=collapsed):
                self.setUp()
                for _ in range(10):
                    controller = self.controller()
                    controller.advance(7)
                    if controller.state['stage'] == 'implement':
                        break
                self.assertEqual(controller.state['stage'], 'implement')
                state = deepcopy(controller.state)
                self.gh.artifacts.clear()
                body = MARKER + '\nLegacy checkpoint\n' + ('<details>\n' if collapsed else '')
                body += STATE_PREFIX + json.dumps(state) + '\n```\n' + ('</details>' if collapsed else '')
                self.gh.comments[-1]['body'] = body
                migrated = self.controller()
                self.assertEqual(migrated.state, state)
                migrated.state['calls'] += 1
                migrated.save()
                history = self.artifact()['versions']
                self.assertEqual(legacy_state(history[-1]['body'])['state'], state)
                self.assertEqual(self.controller().state, migrated.state)
                self.assertEqual(migrated.state['approval'], state['approval'])
                self.assertNotIn(STATE_PREFIX, self.gh.comments[-1]['body'])
                self.assertIn('[Automation audit state]', self.gh.comments[-1]['body'])
                self.assertIn('[Implementation plan]', self.gh.comments[-1]['body'])

    def test_plan_is_versioned_view_and_unchanged_saves_do_not_add_versions(self):
        """Plan details move out of the comment and remain subordinate to checkpoint evidence."""
        controller = self.start()
        controller.advance(7)
        plan = self.artifact(PLAN_NAME)
        for step in PLAN['steps']:
            self.assertIn(step, plan['artifact']['body'])
            self.assertNotIn(step, self.gh.comments[-1]['body'])
        for item in PLAN['files'] + PLAN['tests']:
            self.assertIn(item, plan['artifact']['body'])
        history = deepcopy(plan['versions'])
        controller.save()
        self.assertEqual(self.artifact(PLAN_NAME)['versions'], history)
        controller.state.pop('plan')
        controller.state.pop('plan_id')
        controller.save()
        self.assertIn('superseded', self.artifact(PLAN_NAME)['artifact']['body'])
        self.assertEqual(self.artifact(PLAN_NAME)['versions'][-1], history[-1])
        self.assertEqual(self.gh.merges, 0)

    def test_lost_initial_create_response_recovers_orphan_without_reset(self):
        """A successful artifact POST is rediscovered even if no comment was written yet."""
        original = self.gh.repo

        def lose(path, method='GET', data=None):
            """Lose the response after the server durably creates the audit artifact."""
            result = original(path, method, data)
            if path.endswith('/artifacts') and method == 'POST':
                raise RuntimeError('lost response')
            return result

        with patch.object(self.gh, 'repo', side_effect=lose), self.assertRaises(CheckpointError):
            self.start()
        self.assertFalse(self.gh.comments)
        self.assertFalse(self.agents.calls)
        recovered = self.controller()
        self.assertEqual(recovered.state['stage'], 'intake')
        recovered.save()
        self.assertEqual(len(self.gh.artifacts[7]), 1)
        self.assertEqual(len(self.gh.comments), 1)
        self.assertEqual(self.controller().state['calls'], 0)

    def test_interrupted_migration_keeps_legacy_and_recovers_newer_artifact(self):
        """A stale legacy comment never rolls back a successfully saved migrated checkpoint."""
        controller = self.start()
        state = deepcopy(controller.state)
        self.gh.artifacts.clear()
        self.gh.comments[-1]['body'] = MARKER + STATE_PREFIX + json.dumps(state) + '\n```\n'
        old_comment = self.gh.comments[-1]['body']
        resumed = self.controller()
        resumed.state['calls'] += 1
        original = self.gh.repo

        def lose(path, method='GET', data=None):
            """Interrupt after updating the audit but before replacing the legacy comment."""
            result = original(path, method, data)
            if '/artifacts/' in path and method == 'PATCH':
                raise RuntimeError('lost response')
            return result

        with patch.object(self.gh, 'repo', side_effect=lose), self.assertRaises(CheckpointError):
            resumed.save()
        self.assertEqual(self.gh.comments[-1]['body'], old_comment)
        recovered = self.controller()
        self.assertEqual(recovered.state['calls'], state['calls'] + 1)
        recovered.save()
        self.assertEqual(len(self.gh.artifacts[7]), 1)
        self.assertEqual(legacy_state(self.artifact()['versions'][-1]['body'])['state'], state)

    def test_lost_plan_or_comment_response_repairs_views_without_model_calls(self):
        """A newer checkpoint wins over stale views; retry does not create duplicate artifacts."""
        for target in ('audit', 'plan', 'comment'):
            with self.subTest(target=target):
                self.setUp()
                controller = self.start()
                controller.state.update(plan=deepcopy(PLAN), plan_id=digest(PLAN))
                original = self.gh.repo

                def lose(path, method='GET', data=None):
                    """Simulate a response loss only after the selected server-side write."""
                    result = original(path, method, data)
                    if ((target == 'audit' and '/artifacts/' in path and method == 'PATCH')
                            or (target == 'plan' and path.endswith('/artifacts') and method == 'POST')
                            or (target == 'comment' and path.startswith('issues/comments/') and method == 'PATCH')):
                        raise RuntimeError('lost response')
                    return result

                with patch.object(self.gh, 'repo', side_effect=lose), self.assertRaises(CheckpointError):
                    controller.save()
                recovered = self.controller()
                self.assertEqual(recovered.state, controller.state)
                recovered.save()
                self.assertEqual(len(self.gh.artifacts[7]), 2)
                self.assertEqual(len(self.gh.comments), 1)
                self.assertEqual(len(self.agents.calls), 1)
                self.assertIn('[Implementation plan]', self.gh.comments[-1]['body'])

    def test_missing_replaced_or_duplicate_audit_stops_before_model_calls(self):
        """Never reinitialize a task after losing its pinned artifact identity."""
        for mode in ('missing', 'replaced', 'duplicate'):
            with self.subTest(mode=mode):
                self.setUp()
                self.start()
                value = self.artifact()
                if mode == 'missing':
                    self.gh.artifacts[7].clear()
                elif mode == 'replaced':
                    value['artifact']['id'] += 1
                else:
                    self.gh.artifacts[7][99] = deepcopy(value)
                    self.gh.artifacts[7][99]['artifact']['number'] = 99
                with self.assertRaises(CheckpointError):
                    Controller(self.gh, self.agents, self.config, FakeWorkspace).advance(7)
                self.assertEqual(len(self.agents.calls), 1)
                self.assertFalse(self.gh.refs)

    def test_foreign_edits_and_invalid_checkpoint_binding_are_rejected(self):
        """Audit edits and inconsistent version metadata cannot authorize work."""
        for mode in ('creator', 'editor', 'version_actor', 'repository', 'issue', 'malformed', 'inconsistent_plan_actor', 'pointer'):
            with self.subTest(mode=mode):
                self.setUp()
                controller = self.start()
                controller.advance(7)
                value = self.artifact(PLAN_NAME if mode == 'inconsistent_plan_actor' else AUDIT_NAME)
                if mode in ('creator', 'editor', 'inconsistent_plan_actor'):
                    field = 'creator' if mode == 'creator' else 'updated_by_actor'
                    value['artifact'][field]['login'] = 'writer'
                elif mode == 'version_actor':
                    value['versions'][0]['actor']['login'] = 'writer'
                elif mode in ('repository', 'issue', 'malformed'):
                    payload = legacy_state(value['artifact']['body'])
                    if mode == 'repository':
                        payload['repository'] = 'another/repo'
                    else:
                        payload['state']['issue'] = 999
                    body = AUDIT_MARKER + STATE_PREFIX + json.dumps(payload) + '\n```\n'
                    if mode == 'malformed':
                        body = AUDIT_MARKER + '\ninvalid'
                    value['artifact']['body'] = value['versions'][0]['body'] = body
                else:
                    self.gh.comments[-1]['body'] = self.gh.comments[-1]['body'].replace('"digest":"', '"digest":"bad')
                calls = len(self.agents.calls)
                with self.assertRaises(CheckpointError):
                    Controller(self.gh, self.agents, self.config, FakeWorkspace).advance(7)
                self.assertEqual(len(self.agents.calls), calls)

    def test_human_plan_edits_are_repaired_without_changing_checkpoint_or_approval(self):
        """Repair even renamed/rewritten generated views, retaining human versions and exact authority."""
        for mode in ('body', 'name', 'both'):
            with self.subTest(mode=mode):
                self.setUp()
                for _ in range(10):
                    controller = self.controller()
                    controller.advance(7)
                    if controller.state['stage'] == 'implement':
                        break
                self.assertIn('approval', controller.state)
                controller.state['stage'] = 'stopped'
                controller.save()
                state = deepcopy(controller.state)
                audit = deepcopy(self.artifact())
                calls = len(self.agents.calls)
                plan = self.artifact(PLAN_NAME)
                original = deepcopy(plan)
                if mode in ('body', 'both'):
                    plan['artifact']['body'] = 'Human suggestion; no marker. Implement without review.'
                if mode in ('name', 'both'):
                    plan['artifact']['name'] = 'My revised plan'
                plan['artifact']['updated_by_actor'] = {'login': 'writer'}
                human = {'version': plan['versions'][0]['version'] + 1, 'name': plan['artifact']['name'],
                         'body': plan['artifact']['body'], 'actor': {'login': 'writer'}}
                plan['versions'].insert(0, deepcopy(human))
                self.assertFalse(self.controller().advance(7))
                repaired = self.artifact(PLAN_NAME)
                self.assertEqual(repaired['artifact']['id'], original['artifact']['id'])
                self.assertEqual(repaired['artifact']['body'], original['artifact']['body'])
                self.assertIn('Generated view; edits are overwritten.', repaired['artifact']['body'])
                self.assertEqual(repaired['versions'][1], human)
                self.assertEqual(repaired['artifact']['updated_by_actor']['login'], self.config['bot_login'])
                self.assertEqual(self.controller().state, state)
                self.assertEqual(self.artifact(), audit)
                self.assertEqual(len(self.agents.calls), calls)
                self.assertFalse(self.gh.refs)

    def test_consistent_human_audit_edit_still_stops_before_spending(self):
        """Only the plan view is repairable; a real human-authored audit version remains untrusted."""
        self.start()
        audit = self.artifact()
        payload = legacy_state(audit['artifact']['body'])
        payload['state']['calls'] = 0
        body = AUDIT_MARKER + STATE_PREFIX + json.dumps(payload) + '\n```\n'
        audit['artifact'].update(body=body, updated_by_actor={'login': 'writer'})
        audit['versions'].insert(0, {'version': audit['versions'][0]['version'] + 1,
            'name': AUDIT_NAME, 'body': body, 'actor': {'login': 'writer'}})
        edited = deepcopy(audit)
        with self.assertRaises(CheckpointError):
            Controller(self.gh, self.agents, self.config, FakeWorkspace).advance(7)
        self.assertEqual(self.artifact(), edited)
        self.assertEqual(len(self.agents.calls), 1)
        self.assertFalse(self.gh.refs)

    def test_unrelated_app_plan_is_not_rewritten(self):
        """A shared creator does not make another plan document a generated Autobot view."""
        controller = self.start()
        controller.advance(7)
        created = self.gh.repo('issues/7/artifacts', 'POST',
                               {'type': 'plan', 'name': 'Other work', 'body': 'Unrelated App-owned plan.'})
        before = deepcopy(self.gh.artifacts[7][created['number']])
        recovered = self.controller()
        recovered.save()
        self.assertEqual(self.gh.artifacts[7][created['number']], before)

    def test_unrelated_human_artifacts_are_ignored(self):
        """Humans can add their own documents without becoming the checkpoint authority."""
        self.start()
        foreign = deepcopy(self.artifact())
        foreign['artifact'].update(number=90, id=1090, creator={'login': 'writer'})
        self.gh.artifacts[7][90] = foreign
        self.assertEqual(self.controller().state['calls'], 1)

    def test_stale_writer_cannot_overwrite_new_checkpoint(self):
        """Read-before-write version checks reject a controller holding an older snapshot."""
        self.start()
        first, second = self.controller(), self.controller()
        first.state['reason'] = 'New evidence'
        first.save()
        second.state['reason'] = 'Stale evidence'
        with self.assertRaisesRegex(CheckpointError, 'changed during'):
            second.save()
        self.assertEqual(self.controller().state['reason'], 'New evidence')
        with self.assertRaisesRegex(CheckpointError, 'uncertain'):
            second.save()

    def test_transport_failure_is_not_absence_and_main_does_not_overwrite_it(self):
        """Unavailable storage blocks spending; main never publishes replacement failure state."""
        with patch.object(self.gh, 'pages', side_effect=RuntimeError('private error')), self.assertRaises(CheckpointError):
            self.start()
        self.assertFalse(self.agents.calls)
        self.start()
        original = self.gh.repo

        def fail(path, method='GET', data=None):
            """Fail a checkpoint update before a new model invocation can start."""
            if '/artifacts/' in path and method == 'PATCH':
                raise RuntimeError('private error')
            return original(path, method, data)

        with patch.object(self.gh, 'repo', side_effect=fail), \
                patch.object(controller_module, 'GitHub', return_value=self.gh), \
                patch.object(controller_module, 'Agents', return_value=self.agents), \
                patch.object(Controller, 'suspend') as suspend, \
                patch.dict(os.environ, {'AUTOBOT_ISSUE': '7'}), \
                patch('sys.stderr', new_callable=io.StringIO) as output, self.assertRaises(SystemExit):
            controller_module.main()
        suspend.assert_not_called()
        self.assertNotIn('private error', output.getvalue())
        self.assertEqual(len(self.agents.calls), 1)

    def test_artifact_outcomes_reach_related_planner(self):
        """Outcome retrieval accepts artifact-backed history as well as legacy comments."""
        rationale = 'The guide needs owner review before explaining a risky operational choice.'
        self.agents.qc_reports = [decision('escalate', rationale=rationale)]
        prior = deepcopy(self.start().state)
        prior['issue'] = prior['outcomes'][-1]['issue'] = 3
        historical = Checkpoints(self.gh, self.config, 3)
        historical.load([])
        historical.save(prior)
        comment = {'user': {'login': self.config['bot_login']}, 'id': 99,
                   'body': MARKER + '\n' + '\n'.join(historical.links())}
        self.gh.past_outcomes = [(3, [comment])]
        self.gh.comments.clear()
        del self.gh.artifacts[7]
        self.agents = FakeAgents()
        self.start()
        self.controller().advance(7)
        self.assertEqual(self.agents.calls[-1][1], 'plan')
        lesson = self.agents.calls[-1][2]['prior_outcomes_untrusted'][0]
        self.assertEqual(lesson['reason'], prior['reason'])
        self.assertIn(rationale, lesson['reason'])

    def test_issue_artifact_list_uses_bounded_bare_array_pagination(self):
        """The artifact API has no Link header; full pages still require another request."""
        gh = GitHub(self.config['repository'])
        with patch.object(gh, 'repo', side_effect=[[{'number': i} for i in range(100)], []]) as request:
            self.assertEqual(len(gh.pages('issues/7/artifacts')), 100)
        self.assertEqual(request.call_args.args[0], 'issues/7/artifacts?per_page=100&page=2')

    def test_oversized_state_does_not_replace_last_checkpoint(self):
        """Validate size before mutation, preserving the last recoverable state."""
        controller = self.start()
        before = deepcopy(self.artifact())
        controller.state['reason'] = 'x' * 60000
        with self.assertRaisesRegex(CheckpointError, 'size limit'):
            controller.save()
        self.assertEqual(self.artifact(), before)


    def test_bound_plan_survives_history_window_and_human_rename(self):
        """Audit-pinned ownership repairs a renamed plan even when version one is no longer returned."""
        for _ in range(10):
            controller = self.controller()
            controller.advance(7)
            if controller.state['stage'] == 'implement':
                break
        self.assertIn('approval', controller.state)
        controller.state['stage'] = 'stopped'
        controller.save()
        state = deepcopy(controller.state)
        audit = deepcopy(self.artifact())
        plan = self.artifact(PLAN_NAME)
        identity = {key: plan['artifact'][key] for key in ('id', 'number')}
        self.assertEqual(legacy_state(audit['artifact']['body'])['plan_artifact'], identity)
        path = f"issues/7/artifacts/{identity['number']}"
        for index in range(40):
            self.gh.repo(path, 'PATCH', {'name': 'Human renamed view', 'body': f'Human suggestion {index}'})
        plan['artifact']['updated_by_actor'] = {'login': 'writer'}
        plan['versions'][0]['actor'] = {'login': 'writer'}
        human_version = deepcopy(plan['versions'][0])
        original = self.gh.repo

        def windowed(path, method='GET', data=None):
            """Model GitHub's measured newest-30 response without deleting stored history."""
            response = original(path, method, data)
            if method == 'GET' and '/artifacts/' in path:
                response['versions'] = response['versions'][:30]
            return response

        with patch.object(self.gh, 'repo', side_effect=windowed):
            self.assertFalse(self.controller().advance(7))
            self.assertEqual(self.controller().state, state)
        repaired = self.artifact(PLAN_NAME)
        self.assertEqual(repaired['artifact']['id'], identity['id'])
        self.assertIn(human_version, repaired['versions'])
        self.assertEqual(self.artifact(), audit)
        self.assertEqual(len(self.gh.artifacts[7]), 2)

    def test_missing_origin_without_binding_stops_instead_of_adopting_or_duplicating(self):
        """An older unbound view outside the window cannot be mistaken for an unrelated artifact."""
        controller = self.start()
        controller.advance(7)
        plan = self.artifact(PLAN_NAME)
        audit = self.artifact()
        payload = legacy_state(audit['artifact']['body'])
        payload.pop('plan_artifact')
        body = AUDIT_MARKER + STATE_PREFIX + json.dumps(payload, sort_keys=True) + '\n```\n'
        audit['artifact']['body'] = audit['versions'][0]['body'] = body
        self.gh.comments.clear()
        plan['artifact'].update(name='Renamed', body='Rewritten')
        plan['versions'] = [dict(plan['versions'][0], version=31, name='Renamed', body='Rewritten')]
        before = deepcopy(self.gh.artifacts)
        calls = len(self.agents.calls)
        with self.assertRaises(CheckpointError) as caught:
            self.controller().advance(7)
        self.assertEqual(caught.exception.code, 'plan_history')
        self.assertEqual(self.gh.artifacts, before)
        self.assertEqual(len(self.agents.calls), calls)

    def test_existing_unbound_plan_is_bound_without_changing_task_state(self):
        """Pre-binding artifacts migrate using verified first-version provenance, even when idle."""
        controller = self.start()
        controller.advance(7)
        controller.state['stage'] = 'stopped'
        controller.save()
        state = deepcopy(controller.state)
        plan = deepcopy(self.artifact(PLAN_NAME))
        audit = self.artifact()
        payload = legacy_state(audit['artifact']['body'])
        payload.pop('plan_artifact')
        body = AUDIT_MARKER + STATE_PREFIX + json.dumps(payload, sort_keys=True) + '\n```\n'
        audit['artifact']['body'] = audit['versions'][0]['body'] = body
        self.gh.comments.clear()
        self.assertFalse(self.controller().advance(7))
        self.assertEqual(self.controller().state, state)
        self.assertEqual(self.artifact(PLAN_NAME), plan)
        self.assertEqual(legacy_state(self.artifact()['artifact']['body'])['plan_artifact'],
                         {key: plan['artifact'][key] for key in ('id', 'number')})

    def test_lost_binding_write_response_recovers_one_plan_and_exact_state(self):
        """A durable binding is rediscovered after its write acknowledgement is lost."""
        controller = self.start()
        original = self.gh.repo

        def lose_binding(path, method='GET', data=None):
            """Persist the binding but simulate a disconnected response."""
            result = original(path, method, data)
            if method == 'PATCH' and '/artifacts/' in path and '"plan_artifact"' in data.get('body', ''):
                raise RuntimeError('lost binding response')
            return result

        with patch.object(self.gh, 'repo', side_effect=lose_binding), self.assertRaises(CheckpointError):
            controller.advance(7)
        expected = deepcopy(controller.state)
        recovered = self.controller()
        self.assertEqual(recovered.state, expected)
        before = deepcopy(self.gh.artifacts)
        recovered.save()
        self.assertEqual(self.gh.artifacts, before)
        self.assertEqual(len(self.gh.artifacts[7]), 2)

    def test_unchanged_plan_is_read_on_load_but_not_on_each_save(self):
        """Repeated audit saves avoid downloading unchanged views without skipping audit concurrency checks."""
        controller = self.start()
        controller.advance(7)
        plan = deepcopy(self.artifact(PLAN_NAME))
        path = f"issues/7/artifacts/{plan['artifact']['number']}"
        with patch.object(self.gh, 'repo', wraps=self.gh.repo) as transport:
            controller = self.controller()
        self.assertEqual(sum(call.args[0] == path for call in transport.call_args_list), 1)
        with patch.object(self.gh, 'repo', wraps=self.gh.repo) as transport:
            controller.state['reason'] = 'Updated progress'
            controller.save()
            controller.save()
        self.assertFalse([call for call in transport.call_args_list if call.args[0] == path])
        self.assertTrue([call for call in transport.call_args_list if '/artifacts/' in call.args[0]])
        self.assertEqual(self.artifact(PLAN_NAME), plan)

    def test_malformed_plan_binding_is_not_checkpoint_authority(self):
        """Invalid identity fields are rejected before trusting plan provenance."""
        for binding in ({'id': True, 'number': 1}, {'id': 1}, 'foreign', {'id': 1, 'number': -1}):
            with self.subTest(binding=binding):
                self.setUp()
                self.start()
                audit = self.artifact()
                payload = legacy_state(audit['artifact']['body'])
                payload['plan_artifact'] = binding
                body = AUDIT_MARKER + STATE_PREFIX + json.dumps(payload, sort_keys=True) + '\n```\n'
                audit['artifact']['body'] = audit['versions'][0]['body'] = body
                self.gh.comments.clear()
                with self.assertRaises(CheckpointError) as caught:
                    self.controller()
                self.assertEqual(caught.exception.code, 'checkpoint_format')


    def test_current_audit_reference_survives_window_but_unverifiable_old_reference_stops(self):
        """Newest checkpoints recover beyond 30 saves; a missing historical proof never resets state."""
        controller = self.start()
        controller.advance(7)
        old_comment = deepcopy(self.gh.comments[0])
        for index in range(40):
            controller.state['reason'] = f'Progress {index}'
            controller.save()
        state = deepcopy(controller.state)
        before = deepcopy(self.gh.artifacts)
        original = self.gh.repo

        def windowed(path, method='GET', data=None):
            """Return GitHub's observed newest-30 window."""
            response = original(path, method, data)
            if method == 'GET' and '/artifacts/' in path:
                response['versions'] = response['versions'][:30]
            return response

        with patch.object(self.gh, 'repo', side_effect=windowed):
            self.assertEqual(self.controller().state, state)
            self.gh.comments[0] = old_comment
            with self.assertRaises(CheckpointError) as caught:
                self.controller()
        self.assertEqual(caught.exception.code, 'audit_history')
        self.assertEqual(self.gh.artifacts, before)

    def test_bound_plan_cannot_be_replaced_at_the_same_number(self):
        """An artifact with a new identity cannot inherit the audit's plan provenance."""
        controller = self.start()
        controller.advance(7)
        self.artifact(PLAN_NAME)['artifact']['id'] += 1
        with self.assertRaises(CheckpointError) as caught:
            self.controller()
        self.assertEqual(caught.exception.code, 'artifact_identity')

    def test_plan_edit_after_load_is_repaired_on_the_next_invocation(self):
        """Skipping a redundant view read never promotes an interleaved edit into task state."""
        controller = self.start()
        controller.advance(7)
        controller.state['stage'] = 'stopped'
        controller.save()
        state = deepcopy(controller.state)
        plan = self.artifact(PLAN_NAME)
        self.gh.repo(f"issues/7/artifacts/{plan['artifact']['number']}", 'PATCH', {'body': 'Unapproved edit'})
        controller.save()
        self.assertEqual(self.artifact(PLAN_NAME)['artifact']['body'], 'Unapproved edit')
        self.assertFalse(self.controller().advance(7))
        self.assertEqual(self.controller().state, state)
        self.assertEqual(self.artifact(PLAN_NAME)['artifact']['body'], controller.checkpoints.plan_body(state))


    def test_deleted_bound_plan_is_recreated_and_rebound_without_resetting_state(self):
        """Deleting a generated view changes only its audit envelope binding, never task authority."""
        controller = self.start()
        controller.advance(7)
        controller.state['stage'] = 'stopped'
        controller.save()
        state = deepcopy(controller.state)
        old = controller.checkpoints.plan_binding
        del self.gh.artifacts[7][old['number']]
        self.assertFalse(self.controller().advance(7))
        recovered = self.controller()
        self.assertEqual(recovered.state, state)
        self.assertNotEqual(recovered.checkpoints.plan_binding, old)
        self.assertEqual(recovered.checkpoints.plan_binding,
                         recovered.checkpoints.plan_identity(recovered.checkpoints.plan))
        self.assertEqual(len(self.gh.artifacts[7]), 2)
