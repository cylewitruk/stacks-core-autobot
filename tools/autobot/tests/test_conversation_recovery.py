"""Publication outages and large reports preserve recoverable checkpoint authority."""

from copy import deepcopy
import io
import json
import os
import unittest
from unittest.mock import patch

import controller as controller_module
import conversation
from checkpoint_health import MARKER as HEALTH_MARKER
from controller import Controller
from policy import approval_valid, digest
from test_pipeline import CONFIG, FakeAgents, FakeGitHub, FakeWorkspace


class ConversationRecoveryTests(unittest.TestCase):
    """Exercise failure reporting through the production entry point."""

    def setUp(self):
        """Prepare durable state before injecting transport failures."""
        self.config = deepcopy(CONFIG)
        self.gh, self.agents = FakeGitHub(self.config), FakeAgents()
        self.gh.comments = []
        Controller(self.gh, self.agents, self.config, FakeWorkspace).advance(7)

    def load(self):
        """Recover a fresh controller without running another stage."""
        controller = Controller(self.gh, self.agents, self.config, FakeWorkspace)
        controller.issue = self.gh.issue
        controller.load(self.gh.comments)
        return controller

    def run_main(self):
        """Use real error handling with deterministic GitHub and provider transports."""
        with patch.object(controller_module, "GitHub", return_value=self.gh), \
                patch.object(controller_module, "Agents", return_value=self.agents), \
                patch.dict(os.environ, {"AUTOBOT_ISSUE": "7"}):
            controller_module.main()

    def assert_blocked(self, code=None):
        """Both native status and durable state remain visible without losing the outbox."""
        controller = self.load()
        self.assertEqual(controller.state["stage"], "blocked")
        self.assertTrue(controller.state["conversation"]["pending"])
        labels = {v["name"] for v in self.gh.issue["labels"]}
        self.assertTrue({"autobot:stage:blocked", "autobot:attention"}.issubset(labels))
        if code:
            notices = [c for c in self.gh.comments if c["body"].startswith(HEALTH_MARKER)]
            self.assertEqual(len(notices), 1)
            self.assertIn("`" + code + "`", notices[0]["body"])
            self.assertNotIn("private-fixture", notices[0]["body"])
            self.assertIn("Status:", self.gh.summary()["body"])
        return controller

    def test_repeated_publication_failures_stay_visible_then_retry(self):
        """Repeated event failures never recur inside the blocked-state save."""
        original = self.gh.repo

        def fail_event(path, method="GET", data=None):
            """Reject only conversation POSTs, leaving checkpoints and notices reachable."""
            if method == "POST" and (data or {}).get("body", "").startswith(conversation.MARKER):
                raise RuntimeError("private-fixture")
            return original(path, method, data)

        for _ in range(2):
            with patch.object(self.gh, "repo", side_effect=fail_event), \
                    patch("sys.stderr", new_callable=io.StringIO) as output, self.assertRaises(SystemExit):
                self.run_main()
            self.assertNotIn("private-fixture", output.getvalue())
            self.assert_blocked("conversation_publish")
            self.assertEqual(len(self.agents.calls), 2)
        self.gh.comment("/autobot retry")
        self.run_main()
        self.assertEqual(self.load().state["stage"], "plan_qc")
        self.assertFalse(self.load().state["conversation"]["pending"])
        self.assertNotIn({"name": "autobot:attention"}, self.gh.issue["labels"])
        self.assertEqual(self.gh.merges, 0)

    def test_edited_pending_event_has_diagnostic_and_recovers_when_restored(self):
        """A lost response followed by an edit stops visibly without overwriting the edit."""
        original = self.gh.repo

        def lose_and_edit(path, method="GET", data=None):
            """Accept the plan comment, then lose the response after a human changes it."""
            result = original(path, method, data)
            if method == "POST" and (data or {}).get("body", "").startswith(conversation.MARKER):
                self.gh.comments[-1]["body"] += " human edit"
                raise RuntimeError("private-fixture")
            return result

        with patch.object(self.gh, "repo", side_effect=lose_and_edit), \
                patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
            self.run_main()
        for _ in range(2):
            with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
                self.run_main()
            controller = self.assert_blocked("conversation_conflict")
        event = controller.state["conversation"]["pending"][0]
        comment = next(c for c in self.gh.comments if c["body"].startswith(conversation.MARKER + event["id"]))
        self.assertTrue(comment["body"].endswith("human edit"))
        comment["body"] = conversation.event_body(controller.state, event)
        self.gh.comment("/autobot retry")
        self.run_main()
        self.assertEqual(self.load().state["stage"], "plan_qc")

    def test_all_comment_writes_fail_without_masking_failure_or_stage(self):
        """An unavailable comment API still leaves native labels and a valid blocked checkpoint."""
        original = self.gh.repo
        outage = False

        def fail_comments(path, method="GET", data=None):
            """Keep artifact and label endpoints available during a comment outage."""
            nonlocal outage
            if (data or {}).get("body", "").startswith(conversation.MARKER):
                outage = True
            if outage and "comments" in path and method in ("POST", "PATCH"):
                raise RuntimeError("private-fixture")
            return original(path, method, data)

        with patch.object(self.gh, "repo", side_effect=fail_comments), \
                patch("sys.stderr", new_callable=io.StringIO) as output, self.assertRaises(SystemExit):
            self.run_main()
        self.assertNotIn("private-fixture", output.getvalue())
        self.assert_blocked()


class ConversationSizeTests(unittest.TestCase):
    """Large public reports fit checkpoints without discarding operative evidence."""

    def test_large_plan_and_reviews_reach_disabled_merge_gates(self):
        """Measure every real audit write, including the not-yet-published outbox."""
        config = deepcopy(CONFIG)
        gh, agents = FakeGitHub(config), FakeAgents()
        gh.comments = []
        original_ask, original_repo = agents.ask, gh.repo
        sizes = []
        plan_text, rationale, summary = "p" * 19000, "r" * 5500, "s" * 15000

        def large_report(provider, stage, context):
            """Use valid near-limit responses from each production role."""
            result = original_ask(provider, stage, context)
            if stage == "plan":
                result["steps"] = [plan_text]
            elif stage in ("intake", "plan_qc"):
                result["rationale"] = rationale
            elif stage in ("review", "local_review"):
                result["summary"] = summary
            return result

        def measure(path, method="GET", data=None):
            """Capture the exact artifact payload accepted by the GitHub transport."""
            if "/artifacts" in path and method in ("POST", "PATCH"):
                sizes.append(len(data["body"].encode()))
            return original_repo(path, method, data)

        with patch.object(agents, "ask", side_effect=large_report), patch.object(gh, "repo", side_effect=measure):
            for _ in range(11):
                controller = Controller(gh, agents, config, FakeWorkspace)
                controller.advance(7)
        state = controller.state
        self.assertEqual(state["stage"], "gates")
        self.assertTrue(approval_valid(state, config))
        self.assertLessEqual(max(sizes), 60000)
        self.assertEqual(state["plan"]["steps"], [plan_text])
        self.assertNotIn("cooperative_votes", state)
        self.assertFalse(state["conversation"]["pending"])
        self.assertTrue(state["conversation"]["receipts"])
        self.assertTrue(all(not v["resolved"] for v in state.get("objections", [])))
        timeline = "\n".join(c["body"] for c in gh.comments if c["body"].startswith(conversation.MARKER))
        for full_text in (plan_text, rationale, summary):
            self.assertIn(full_text, timeline)
        self.assertLess(len(state["reviews"]["claude"]["summary"]), 1000)
        self.assertFalse(config["merge_enabled"])
        self.assertEqual(gh.merges, 0)

    def test_compaction_preserves_pending_and_unpublished_evidence(self):
        """Only acknowledged narrative is shortened; bindings, findings and open objections survive."""
        report = {"summary": "\u2028\\\"" * 1500, "findings": [{"path": "a", "detail": "Keep this finding"}]}
        state = {"issue": 7, "reviews": {"claude": deepcopy(report)},
                 "approval": {"binding": "exact"}, "plan": {"steps": ["unchanged"]},
                 "objections": [{"id": "open", "resolved": False}, {"id": "old", "resolved": True}],
                 "cooperative_votes": {"unused": report},
                 "conversation": {"pending": [], "receipts": {"reviews/claude": {"source_digest": digest(report)}}}}
        state["qc"] = {"intake": {"rationale": "unpublished" * 1000}}
        before = deepcopy(state)
        state["conversation"]["pending"] = [{"id": "pending"}]
        conversation.compact(state)
        self.assertEqual(state["reviews"], before["reviews"])
        state["conversation"]["pending"] = []
        conversation.compact(state)
        self.assertLess(len(json.dumps(state["reviews"]["claude"]["summary"]).encode()), 1000)
        for key in ("approval", "plan", "qc"):
            self.assertEqual(state[key], before[key])
        self.assertEqual(state["reviews"]["claude"]["findings"], report["findings"])
        self.assertEqual(state["objections"], [{"id": "open", "resolved": False}])

    def test_reference_outbox_rejects_source_mutation(self):
        """A queued report cannot silently change between checkpointing and publication."""
        state = {"issue": 7, "reviews": {"claude": {"summary": "original", "findings": [], "design_issue": None}}}
        conversation.queue(state, "Review", source=("reviews", "claude"), kind="review",
                           details={"head": "a" * 40, "round": 1})
        event = state["conversation"]["pending"][0]
        self.assertNotIn("body", event)
        self.assertIn("original", conversation.event_body(state, event))
        state["reviews"]["claude"]["summary"] = "changed"
        with self.assertRaises(conversation.PublicationError):
            conversation.event_body(state, event)
