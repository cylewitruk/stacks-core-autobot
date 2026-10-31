"""Native GitHub progress and Development links preserve the durable approval boundary."""

from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from controller import Controller, ESCALATION_REASON, STATE_PREFIX, MARKER
from github import GitHub
from policy import Blocked
from test_pipeline import CONFIG, FakeAgents, FakeGitHub, FakeWorkspace
from test_autonomy import decision


class SurfaceTests(unittest.TestCase):
    """Keep human-facing labels and summaries separate from machine authorization."""

    def setUp(self):
        """Create a writer-enrolled task with no approval commands."""
        self.config = deepcopy(CONFIG)
        self.gh, self.agents = FakeGitHub(self.config), FakeAgents()
        self.gh.comments = []

    def controller(self):
        """Load the most recent durable state as a fresh controller process would."""
        controller = Controller(self.gh, self.agents, self.config, FakeWorkspace)
        controller.issue = self.gh.issue
        controller.load(self.gh.comments)
        return controller

    def approved(self):
        """Stop immediately after exact-design consensus, before implementation starts."""
        for _ in range(10):
            controller = self.controller()
            controller.advance(7)
            if controller.state["stage"] == "implement":
                return controller
        self.fail("design did not reach approval")

    def test_summary_deduplicates_rationale_and_links_public_state(self):
        """One visible rationale and native stage label replace the duplicated status text."""
        controller = self.controller()
        controller.advance(7)
        body = self.gh.summary()["body"]
        visible = body.split("<details>", 1)[0]
        rationale = controller.state["qc"]["intake"]["rationale"]
        self.assertEqual(visible.count(rationale), 1)
        self.assertNotIn("Status:", visible)
        self.assertNotIn("Stage:", visible)
        self.assertIn("[Automation audit state]", body)
        self.assertNotIn(STATE_PREFIX, body)
        self.assertNotIn("<details>", body)
        self.assertIn({"name": "autobot:stage:plan"}, self.gh.issue["labels"])
        self.assertEqual(self.controller().state, controller.state)
        controller.state.update(stage="blocked", reason="Transport unavailable")
        controller.save()
        self.assertIn("Status: Transport unavailable", self.gh.summary()["body"].split("<details>")[0])

    def test_escalation_shows_rationale_once_and_keeps_action(self):
        """High risk and explicit escalations retain one explanation plus the required action."""
        for report in (decision("proceed", risk="high"), decision("escalate")):
            with self.subTest(report=report):
                self.setUp()
                self.agents.qc_reports = [report]
                controller = self.controller()
                controller.advance(7)
                self.assertEqual(controller.state["stage"], "escalated")
                expected = ESCALATION_REASON + " " + report["rationale"]
                recovered = self.controller().state
                self.assertEqual(recovered["reason"], expected)
                self.assertEqual(recovered["outcomes"][-1]["status"], "escalated")
                self.assertEqual(recovered["outcomes"][-1]["reason"], expected)
                visible = self.gh.summary()["body"].split("<details>")[0]
                self.assertEqual(visible.count(report["rationale"]), 1)
                self.assertIn("Status: " + ESCALATION_REASON, visible)
                self.assertFalse(self.gh.refs)

    def test_legacy_escalation_reason_is_deduplicated_without_changing_state(self):
        """Existing prefixed reasons stay recoverable and distinct blockers stay visible."""
        controller = self.controller()
        controller.advance(7)
        rationale = controller.state["qc"]["intake"]["rationale"]
        controller.state.update(stage="escalated", reason=ESCALATION_REASON + " " + rationale)
        controller.save()
        recovered = self.controller()
        self.assertEqual(recovered.state, controller.state)
        visible = self.gh.summary()["body"].split("<details>")[0]
        self.assertEqual(visible.count(rationale), 1)
        self.assertIn("Status: " + ESCALATION_REASON, visible)
        recovered.state["reason"] = "Transport failed after QC: " + rationale
        recovered.save()
        self.assertIn("Status: " + recovered.state["reason"], self.gh.summary()["body"])

    def test_old_expanded_state_remains_readable(self):
        """Changing the comment presentation does not reset existing budgets or evidence."""
        controller = self.controller()
        controller.advance(7)
        state = deepcopy(controller.state)
        self.gh.artifacts.clear()
        self.gh.summary()["body"] = "<!-- stacks-autobot:v1 -->\nOld summary" + STATE_PREFIX + json.dumps(state) + "\n```\n"
        self.assertEqual(self.controller().state, state)

    def test_labels_are_retryable_display_and_cannot_authorize_execution(self):
        """A failed label update preserves state; a forged label cannot skip design review."""
        controller = self.controller()
        with patch.object(self.gh, "sync_stage", side_effect=RuntimeError("private transport error")), \
                patch("sys.stderr", new_callable=io.StringIO) as output:
            controller.advance(7)
        self.assertNotIn("private transport error", output.getvalue())
        visible = self.gh.summary()["body"].split("<details>")[0]
        self.assertIn("Stage: **plan** (label sync unavailable).", visible)
        self.assertNotIn("private transport error", visible)
        self.gh.issue["labels"].append({"name": "autobot:stage:implement"})
        recovered = self.controller()
        self.assertEqual(recovered.state["stage"], "plan")
        recovered.save()
        self.assertNotIn("Stage:", self.gh.summary()["body"].split("<details>")[0])
        self.assertNotIn({"name": "autobot:stage:implement"}, self.gh.issue["labels"])
        self.assertIn({"name": "autobot"}, self.gh.issue["labels"])
        self.assertEqual(recovered.state["calls"], 1)
        with self.assertRaises(Blocked):
            recovered.implement()
        self.assertFalse(self.gh.refs)

    def test_idle_task_retains_durable_state_and_shows_failed_label_sync(self):
        """Even an already-escalated task gets a readable fallback without spending quota."""
        self.agents.qc_reports = [decision("escalate")]
        controller = self.controller()
        controller.advance(7)
        state = deepcopy(controller.state)
        resumed = self.controller()

        def fail_after_checkpoint(number, stage):
            """Ensure the checkpoint is already readable before a label transport failure."""
            self.assertEqual(self.controller().state, state)
            raise RuntimeError("private transport error")

        with patch.object(self.gh, "sync_stage", side_effect=fail_after_checkpoint), \
                patch("sys.stderr", new_callable=io.StringIO):
            self.assertFalse(resumed.advance(7))
        self.assertEqual(self.controller().state, state)
        visible = self.gh.summary()["body"].split("<details>")[0]
        self.assertIn("Stage: **escalated** (label sync unavailable).", visible)
        self.assertEqual(len(self.agents.calls), 1)
        with patch.object(self.gh, "repo", wraps=self.gh.repo) as repo:
            self.assertFalse(self.controller().advance(7))
        writes = [call for call in repo.call_args_list if len(call.args) > 1 and call.args[1] == "PATCH"]
        self.assertEqual(len(writes), 1)
        self.assertNotIn("label sync unavailable", self.gh.summary()["body"].split("<details>")[0])
        self.assertEqual(self.controller().state, state)
        with patch.object(self.gh, "repo", wraps=self.gh.repo) as repo:
            self.assertFalse(self.controller().advance(7))
        self.assertFalse([call for call in repo.call_args_list if len(call.args) > 1 and call.args[1] == "PATCH"])
        self.assertEqual(len(self.agents.calls), 1)

    def test_branch_links_only_after_approval_and_before_candidate_work(self):
        """Development shows the approved base while candidate objects await local acceptance."""
        controller = self.approved()
        self.assertFalse(self.gh.refs)
        branch = controller.state["branch"]
        ask = self.agents.ask

        def inspect(provider, stage, context):
            """Observe the external branch at the actual model invocation boundary."""
            if stage == "develop":
                self.assertEqual(self.gh.refs, {branch: self.gh.base})
                self.assertTrue(self.gh.branch_linked(7, branch))
                self.assertEqual(len(self.gh.commits), 1)
                self.assertIsNone(self.gh.pr)
            return ask(provider, stage, context)

        with patch.object(self.agents, "ask", side_effect=inspect):
            controller.advance(7)
        self.assertEqual(controller.state["stage"], "publish")
        self.assertEqual(self.gh.refs[branch], self.gh.base)
        published = self.controller()
        published.advance(7)
        self.assertEqual(self.gh.refs[branch], published.state["head"])
        self.assertEqual(published.state["stage"], "review")

    def test_lost_branch_creation_response_recovers_without_spending_a_model_call(self):
        """Durable intent recovers an existing linked base instead of creating another branch."""
        controller = self.approved()
        calls, branch = controller.state["calls"], controller.state["branch"]
        self.gh.fail_after_link = True
        with self.assertRaises(RuntimeError):
            controller.advance(7)
        recovered = self.controller()
        self.assertEqual(recovered.state["calls"], calls)
        self.assertFalse(recovered.state["development_branches"][branch]["linked"])
        recovered.advance(7)
        self.assertTrue(recovered.state["development_branches"][branch]["linked"])
        self.assertEqual(len(self.gh.refs), 1)

    def test_unlinked_or_moved_branch_blocks_before_model_spending(self):
        """Matching a branch name or base alone does not establish ownership of a reservation."""
        for linked, sha in ((False, "a" * 40), (True, "f" * 40)):
            with self.subTest(linked=linked, sha=sha):
                self.setUp()
                controller = self.approved()
                branch, calls = controller.state["branch"], controller.state["calls"]
                self.gh.refs[branch] = sha
                if linked:
                    self.gh.linked_branches.add(branch)
                with self.assertRaisesRegex(Blocked, "moved or is not linked"):
                    controller.advance(7)
                self.assertEqual(controller.state["calls"], calls)
                self.assertEqual(self.gh.refs[branch], sha)

    def test_deleted_or_unlinked_reservation_cannot_publish_candidate(self):
        """Publication cannot recreate a deleted reservation or overwrite an unlinked base."""
        for deleted in (False, True):
            with self.subTest(deleted=deleted):
                self.setUp()
                controller = self.approved()
                controller.advance(7)
                branch = controller.state["branch"]
                self.gh.linked_branches.clear()
                if deleted:
                    del self.gh.refs[branch]
                with self.assertRaises(Blocked):
                    self.controller().advance(7)
                self.assertIsNone(self.gh.pr)
                self.assertEqual(set(self.gh.refs.values()), set() if deleted else {self.gh.base})

    def test_legacy_prepared_publication_creates_native_link_at_validated_head(self):
        """Existing prepared work remains resumable without inventing a baseline reservation."""
        controller = self.approved()
        controller.advance(7)
        del controller.state["development_branches"]
        self.gh.refs.clear()
        self.gh.linked_branches.clear()
        controller.save()
        resumed = self.controller()
        resumed.advance(7)
        self.assertEqual(resumed.state["stage"], "review")
        self.assertEqual(self.gh.refs[resumed.state["branch"]], resumed.state["head"])
        self.assertTrue(self.gh.branch_linked(7, resumed.state["branch"]))


class GitHubSurfaceAdapterTests(unittest.TestCase):
    """Verify exact native API operations without modifying live issues or branches."""

    def test_label_sync_preserves_unrelated_labels_and_only_creates_missing_status(self):
        """Progress updates must never replace all labels or remove the enrollment label."""
        gh = GitHub(CONFIG["repository"])
        current = [{"name": n} for n in ("autobot", "bug", "autobot:stage:intake")]
        with patch.object(gh, "pages", side_effect=[current, current]), patch.object(gh, "repo") as repo:
            gh.sync_stage(4, "plan_qc")
        self.assertEqual(repo.call_args_list[0].args[0:2], ("labels", "POST"))
        self.assertEqual(repo.call_args_list[1].args, ("issues/4/labels", "POST", {"labels": ["autobot:stage:plan-qc"]}))
        self.assertEqual(repo.call_args_list[2].args, ("issues/4/labels/autobot%3Astage%3Aintake", "DELETE"))
        with patch.object(gh, "pages", return_value=[{"name": "autobot:stage:plan-qc"}]), patch.object(gh, "repo") as repo:
            gh.sync_stage(4, "plan_qc")
            repo.assert_not_called()

    def test_development_query_paginates_and_checks_repository_identity(self):
        """A same-named branch in another repository is not our Development link."""
        gh = GitHub(CONFIG["repository"])
        pages = []
        for repository, more in (("another/repo", True), (CONFIG["repository"], False)):
            pages.append({"data": {"repository": {"issue": {"linkedBranches": {
                "nodes": [{"ref": None}, {"ref": {"name": "autobot/test", "repository": {"nameWithOwner": repository}}}],
                "pageInfo": {"hasNextPage": more, "endCursor": "next"}}}}}})
        with patch.object(gh, "api", side_effect=pages) as api:
            self.assertTrue(gh.branch_linked(4, "autobot/test"))
            self.assertEqual(api.call_args_list[1].args[2]["variables"]["cursor"], "next")
        with patch.object(gh, "api", return_value={"errors": [{"message": "private"}]}):
            with self.assertRaisesRegex(RuntimeError, "cannot establish"):
                gh.branch_linked(4, "autobot/test")

    def test_create_linked_branch_binds_issue_name_and_commit(self):
        """The server must confirm the exact approved-base branch requested by the controller."""
        gh = GitHub(CONFIG["repository"])
        reply = {"data": {"createLinkedBranch": {"linkedBranch": {"ref": {
            "name": "autobot/test", "target": {"oid": "a" * 40}}}}}}
        with patch.object(gh, "api", return_value=reply) as api:
            gh.create_linked_branch("issue-id", "autobot/test", "a" * 40)
            self.assertEqual(api.call_args.args[2]["variables"]["input"], {
                "issueId": "issue-id", "name": "autobot/test", "oid": "a" * 40})
        reply["data"]["createLinkedBranch"]["linkedBranch"]["ref"]["target"]["oid"] = "f" * 40
        with patch.object(gh, "api", return_value=reply):
            with self.assertRaisesRegex(RuntimeError, "differs"):
                gh.create_linked_branch("issue-id", "autobot/test", "a" * 40)
