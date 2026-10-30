"""Exercise durable queue fairness without GitHub writes or model calls."""

from copy import deepcopy
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import controller
from policy import Blocked
from scheduler import MARKER, QueueScan

CONFIG = json.loads((Path(__file__).resolve().parents[3] / ".autobot/config.json").read_text())


class QueueGitHub:
    """Retain cursor comments across independently constructed scheduler instances."""

    def __init__(self, opened, closed=()):
        """Seed issue listings and an empty durable comment store."""
        self.opened = [{"number": number} for number in opened]
        self.closed = [{"number": number} for number in closed]
        self.comments = {}
        self.writes = []
        self.next_id = 100

    def pages(self, path):
        """Serve the open queue and per-issue cursor comments."""
        if path.startswith("issues?state=open"):
            return deepcopy(self.opened)
        if path.startswith("issues/") and path.endswith("/comments"):
            return deepcopy(self.comments.get(int(path.split("/")[1]), []))
        raise AssertionError(path)

    def repo(self, path, method="GET", data=None):
        """Persist comment creations and updates as the GitHub adapter would."""
        if path.startswith("issues?state=closed"):
            return deepcopy(self.closed)
        self.writes.append((path, method, deepcopy(data)))
        if method == "POST":
            self.next_id += 1
            comment = {"id": self.next_id, "body": data["body"],
                       "user": {"login": CONFIG["bot_login"]}}
            self.comments.setdefault(int(path.split("/")[1]), []).append(comment)
            return deepcopy(comment)
        if method == "PATCH":
            comment_id = int(path.rsplit("/", 1)[1])
            comment = next(c for comments in self.comments.values() for c in comments if c["id"] == comment_id)
            comment.update(data)
            return deepcopy(comment)
        raise AssertionError((path, method))


class SchedulerTests(unittest.TestCase):
    """Check bounded recovery scans, interrupted dispatch and explicit pilot isolation."""

    def run_main(self, github, dispatch, issue=""):
        """Exercise the production entry point with only issue execution stubbed."""
        worker = Mock(state=None)
        worker.advance.side_effect = dispatch
        with patch.object(controller, "GitHub", return_value=github), \
                patch.object(controller, "Agents"), \
                patch.object(controller, "Controller", return_value=worker), \
                patch.dict(os.environ, {"AUTOBOT_ISSUE": issue, "GITHUB_REPOSITORY": CONFIG["repository"]}):
            controller.main()

    def test_waiting_window_does_not_starve_later_work(self):
        """A fresh process reaches actionable work beyond a full window of idle issues."""
        limit = CONFIG["max_open_issues"]
        github = QueueGitHub(range(1, limit + 3))
        visits = []

        def dispatch(number):
            """Model unchanged clarification/stopped/gate states before one ready issue."""
            visits.append(number)
            return number == limit + 1

        self.run_main(github, dispatch)
        self.assertEqual(visits, list(range(1, limit + 1)))
        self.run_main(github, dispatch)
        self.assertEqual(visits, list(range(1, limit + 2)))
        self.assertEqual(len(github.comments[1]), 1)
        self.assertEqual(json.loads(github.comments[1][0]["body"][len(MARKER):]), {"after": limit + 1})
        self.assertNotIn(limit + 2, visits)

    def test_failed_dispatch_does_not_pin_next_scan(self):
        """A failure before issue state loads still advances the separate queue cursor."""
        github = QueueGitHub([1, 2])
        with patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
            self.run_main(github, Mock(side_effect=RuntimeError("transport failed")))
        visits = []
        self.run_main(github, lambda number: visits.append(number) or True)
        self.assertEqual(visits, [2])

    def test_rotation_wraps_and_survives_issue_removal(self):
        """Stable issue numbers preserve progress when the queue membership changes."""
        github = QueueGitHub([1, 2, 3, 4])
        config = {**CONFIG, "max_open_issues": 2}
        self.assertEqual(list(QueueScan(github, config)), [1, 2])
        github.opened = [{"number": number} for number in (1, 3, 4, 5)]
        self.assertEqual(list(QueueScan(github, config)), [3, 4])
        self.assertEqual(list(QueueScan(github, config)), [5, 1])
        self.assertEqual(len(github.comments[1]), 1)

    def test_closed_candidates_share_bound_and_prs_are_excluded(self):
        """Closed reconciliation cannot consume an unbounded prefix ahead of open work."""
        github = QueueGitHub([5, 6], [1, 2, 3])
        github.opened.append({"number": 4, "pull_request": {}})
        config = {**CONFIG, "max_open_issues": 2, "max_outcome_issues": 3}
        self.assertEqual(list(QueueScan(github, config)), [1, 2])
        self.assertEqual(list(QueueScan(github, config)), [3, 5])
        self.assertEqual(list(QueueScan(github, config)), [6, 1])

    def test_explicit_issue_does_not_touch_queue_cursor(self):
        """The supervised one-issue pilot bypasses discovery and cursor writes."""
        github = Mock()
        visits = []
        self.run_main(github, lambda number: visits.append(number) or True, issue="23")
        self.assertEqual(visits, [23])
        github.pages.assert_not_called()
        github.repo.assert_not_called()

    def test_untrusted_cursor_cannot_steer_scan(self):
        """Only the configured App's cursor can affect the next scan position."""
        github = QueueGitHub([1, 2])
        github.comments[1] = [{"id": 42, "user": {"login": "outsider"},
                               "body": MARKER + '{"after": 1}'}]
        self.assertEqual(next(iter(QueueScan(github, CONFIG))), 1)
        self.assertEqual(github.comments[1][0]["body"], MARKER + '{"after": 1}')
        self.assertEqual(github.writes[0][1], "POST")

    def test_ambiguous_or_invalid_cursor_blocks_without_writes(self):
        """Malformed bot state is never guessed or silently replaced."""
        for bodies in (["invalid"], ['{"after": true}'], ['{"after": 0}'],
                       ['{"after": 1}', '{"after": 2}']):
            with self.subTest(bodies=bodies):
                github = QueueGitHub([1, 2])
                github.comments[1] = [{"id": i + 1, "user": {"login": CONFIG["bot_login"]},
                                       "body": MARKER + body} for i, body in enumerate(bodies)]
                with self.assertRaises(Blocked):
                    list(QueueScan(github, CONFIG))
                self.assertEqual(github.writes, [])

    def test_empty_queue_and_anchor_departure(self):
        """An empty queue writes nothing; a departed host resets safely on the new host."""
        github = QueueGitHub([])
        self.assertEqual(list(QueueScan(github, CONFIG)), [])
        self.assertEqual(github.writes, [])
        github.opened = [{"number": number} for number in (1, 2)]
        self.assertEqual(list(QueueScan(github, CONFIG)), [1, 2])
        github.opened = [{"number": 2}]
        self.assertEqual(list(QueueScan(github, CONFIG)), [2])
        self.assertEqual(len(github.comments[2]), 1)


if __name__ == "__main__":
    unittest.main()
