"""Exercise the trusted controller workflow's job condition with event fixtures."""

import json
from pathlib import Path
import re
import unittest


WORKFLOW = Path(__file__).resolve().parents[3] / '.github/workflows/autobot.yml'


def eligible(event_name, event, enabled='true'):
    """Evaluate the workflow's boolean expression subset against GitHub event fixtures."""
    source = WORKFLOW.read_text().split('    if: >-\n', 1)[1].split('    runs-on:', 1)[0]
    context = {'github': {'repository': 'cylewitruk/stacks-core-autobot',
                         'ref': 'refs/heads/main', 'event_name': event_name, 'event': event},
               'vars': {'AUTOBOT_ENABLED': enabled}}

    def lookup(match):
        """Resolve context properties and GitHub's labels projection without code interpolation."""
        value = context
        for part in match.group().split('.'):
            if part == '*':
                continue
            if isinstance(value, list):
                value = [item.get(part, '') for item in value]
            else:
                value = value.get(part, '') if isinstance(value, dict) else ''
        key = 'value_' + str(len(values))
        values[key] = value
        return key

    values = {}
    expression = re.sub(r'\b(?:github|vars)(?:\.(?:[A-Za-z_]+|\*))+', lookup, source)
    expression = expression.replace('&&', ' and ').replace('||', ' or ')
    expression = re.sub(r'!(?!=)', ' not ', expression)
    functions = {'contains': lambda seq, item: item in seq,
                 'startsWith': lambda value, prefix: value.casefold().startswith(prefix.casefold()),
                 'fromJSON': json.loads}
    # Only repository-owned workflow text is evaluated; event values remain bound data.
    return bool(eval(' '.join(expression.split()), {'__builtins__': {}}, {**functions, **values}))


class WorkflowEventTests(unittest.TestCase):
    """Progress display changes cannot cause a new stage to execute."""

    def test_issue_label_events_preserve_enrollment_and_ignore_stage_updates(self):
        """Both additions and removals are filtered while enrollment revocation still wakes."""
        for action in ('labeled', 'unlabeled'):
            for label, enrolled, expected in (
                    ('autobot:stage:plan', True, False),
                    ('autobot:stage:implement', True, False),
                    ('AUTOBOT:STAGE:PLAN', True, False),
                    ('autobot:attention', True, False),
                    ('AUTOBOT:ATTENTION', True, False),
                    ('autobot', action == 'labeled', True),
                    ('bug', True, True), ('bug', False, False)):
                with self.subTest(action=action, label=label, enrolled=enrolled):
                    event = {'action': action, 'label': {'name': label},
                             'issue': {'labels': [{'name': 'autobot'}] if enrolled else []}}
                    self.assertEqual(eligible('issues', event), expected)

    def test_other_events_and_disable_switch_keep_their_guards(self):
        """Schedule, dispatch and legitimate comments remain usable without bot recursion."""
        for name in ('schedule', 'workflow_dispatch'):
            self.assertTrue(eligible(name, {}))
            self.assertFalse(eligible(name, {}, enabled='false'))
        issue = {'labels': [{'name': 'autobot'}], 'user': {'login': 'reporter'}}
        for action in ('opened', 'edited', 'reopened', 'closed'):
            self.assertTrue(eligible('issues', {'action': action, 'issue': issue}))
        for login, kind, association, expected in (
                ('reporter', 'User', 'NONE', True), ('writer', 'User', 'COLLABORATOR', True),
                ('outsider', 'User', 'NONE', False), ('app[bot]', 'Bot', 'OWNER', False)):
            event = {'issue': issue, 'comment': {'body': 'Details',
                     'user': {'login': login, 'type': kind}, 'author_association': association}}
            with self.subTest(login=login):
                self.assertEqual(eligible('issue_comment', event), expected)
