"""Durable issue-to-PR state machine. Only this trusted process writes to GitHub."""

import base64
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import quote

from agents import Agents
from bundle import Bundle
from github import GitHub
from local import LocalWorkspace, available_checks, resource_exhausted
from scheduler import QueueScan
from policy import (Blocked, approval_binding, approval_valid, digest, local_evidence_valid, merge_reasons,
                    consensus_valid, design_roster, path_allowed, required_local_checks, selected_model,
                    instruction_matches, validate_design_issue, validate_edits, validate_plan, validate_qc, validate_review)

MARKER = "<!-- stacks-autobot:v1 -->"
STATE_PREFIX = "\n```json\n"
PROVIDERS = {"claude", "codex", "copilot"}


def validate_config(config):
    """Reject unsafe or internally inconsistent operator configuration."""
    if config["repository"] != "cylewitruk/stacks-core-autobot" or config["base"] != "main":
        raise Blocked("v1 is restricted to the sandbox repository and main")
    reviewers = config["reviewers"]
    if not {"claude", "codex"}.issubset(reviewers) or len(set(reviewers)) != len(reviewers):
        raise Blocked("independent Claude and Codex reviews are mandatory")
    if not set(reviewers).issubset(PROVIDERS):
        raise Blocked("unknown reviewer")
    if config["planner"] not in PROVIDERS or config["implementer"] not in PROVIDERS:
        raise Blocked("unknown planner or implementer")
    if type(config["autonomous"]) is not bool or config["qc"] not in PROVIDERS or not isinstance(config["qc_model"], str):
        raise Blocked("invalid autonomy or QC configuration")
    if config["autonomous"] and config["qc"] == config["planner"]:
        raise Blocked("QC must use a different provider from the planner")
    if type(config["max_plan_revisions"]) is not int or not 0 <= config["max_plan_revisions"] <= 3:
        raise Blocked("invalid plan revision budget")
    if set(config["models"]) != PROVIDERS or not all(isinstance(v, str) for v in config["models"].values()):
        raise Blocked("model map must name each provider")
    if config["required_checks"] != ["Autobot policy", "Autobot Rust"]:
        raise Blocked("v1 required checks cannot be weakened")
    if not 0 <= config["max_fix_rounds"] <= 3 or not 1 <= config["max_model_calls"] <= 64:
        raise Blocked("invalid budget")
    if type(config["max_design_reopens"]) is not int or not 0 <= config["max_design_reopens"] <= 3:
        raise Blocked("invalid design reopen budget")
    if not 1 <= config["max_outcome_issues"] <= 10 or not 1 <= config["max_outcome_lessons"] <= 3:
        raise Blocked("invalid outcome retrieval bound")
    if config["local_reviewer"] not in PROVIDERS or not isinstance(config["local_review_model"], str):
        raise Blocked("invalid local reviewer")
    if not 0 <= config["max_local_fix_rounds"] <= 3 or not 600 <= config["local_timeout_seconds"] <= 2400:
        raise Blocked("invalid local iteration budget")
    if not 1 <= config["local_check_timeout_seconds"] <= 900:
        raise Blocked("invalid local check timeout")


class Controller:
    """Advance at most one bounded stage, preserving intent before remote mutations."""

    def __init__(self, github, agents, config, workspace_factory=None, bundle_root=None):
        """Inject transports so complete lifecycles can be tested without credentials."""
        self.gh, self.agents, self.config = github, agents, config
        self.comment_id = None
        self.state = None
        self.issue = None
        self.permissions = {}
        self.trees = {}
        self.workspace_factory = workspace_factory or LocalWorkspace
        self.bundle = Bundle(bundle_root)

    def save(self):
        """Persist machine state and a readable plan/review summary in one bot-owned comment."""
        state = self.state
        self.record_outcome()
        lines = [MARKER, "## Autobot", "", "Stage: **" + state["stage"] + "**", ""]
        for stage, report in state.get("qc", {}).items():
            lines += ["**QC " + stage + ": " + report["decision"] + "**", "", report["rationale"], ""]
            lines += ["- " + v for v in report["questions"] + report["concerns"]] + [""]
        if state.get("plan"):
            plan = state["plan"]
            lines += [plan["summary"], "", "Plan: `" + state["plan_id"] + "`", ""]
            for key in ("questions", "files", "steps", "tests"):
                lines += ["**" + key.capitalize() + "**", ""] + ["- " + v for v in plan[key]] + [""]
            if state["stage"] == "planned":
                lines += ["A writer can approve with `/autobot approve " + state["plan_id"] + "`.", ""]
        if state.get("pr"):
            lines += ["PR: #" + str(state["pr"]), ""]
        if state.get("local_evidence"):
            local = state["local_evidence"]
            lines += ["Local checkpoint: `" + local["local_commit"] + "`", "",
                      "Local checks: " + ", ".join(c["name"] for c in local["checks"]), "",
                      "Local review: " + local["review"]["verdict"], ""]
        for provider, report in state.get("reviews", {}).items():
            lines += ["**" + provider + "** (`" + report["head"] + "`): " + report["verdict"],
                      "", report["summary"], ""]
            lines += ["- " + f["path"] + ": " + f["detail"] for f in report["findings"]] + [""]
        if state.get("reason"):
            lines += ["Status: " + state["reason"], ""]
        body = "\n".join(lines) + STATE_PREFIX + json.dumps(state, sort_keys=True) + "\n```\n"
        if len(body.encode()) > 60000:
            raise Blocked("durable state exceeds comment limit")
        if self.comment_id:
            self.gh.repo(f"issues/comments/{self.comment_id}", "PATCH", {"body": body})
        else:
            result = self.gh.repo(f"issues/{self.issue['number']}/comments", "POST", {"body": body})
            self.comment_id = result["id"]

    def load(self, comments):
        """Trust only the configured App's single, well-formed state comment."""
        matches = [c for c in comments if c["user"]["login"] == self.config["bot_login"]
                   and c["body"].startswith(MARKER)]
        if len(matches) > 1:
            raise Blocked("multiple state comments; manual reconciliation required")
        if matches:
            self.comment_id = matches[0]["id"]
            self.state = json.loads(matches[0]["body"].rsplit(STATE_PREFIX, 1)[1].rsplit("\n```", 1)[0])
            if self.state["version"] != 2 or self.state["issue"] != self.issue["number"]:
                raise Blocked("state identity mismatch")

    def tree(self, sha):
        """Fetch a complete tree; never silently treat truncation as missing files."""
        if sha in self.trees:
            return self.trees[sha]
        result = self.gh.repo(f"git/trees/{sha}?recursive=1")
        if result.get("truncated"):
            raise Blocked("repository tree is truncated")
        self.trees[sha] = {v["path"]: v for v in result["tree"]}
        return self.trees[sha]

    def files(self, sha, paths):
        """Read bounded regular text blobs, rejecting symlinks and executable files."""
        tree = self.tree(sha)
        result = {}
        for path in paths:
            entry = tree.get(path)
            if entry is None:
                # Existing symlink/file ancestors must not be treated as new directories.
                ancestors = ["/".join(path.split("/")[:i]) for i in range(1, len(path.split("/")))]
                if any(a in tree and tree[a]["type"] != "tree" for a in ancestors):
                    raise Blocked("non-directory path ancestor")
                result[path] = None
                continue
            if entry["mode"] != "100644" or entry["type"] != "blob" or entry.get("size", 0) > 128000:
                raise Blocked("source must be a bounded regular text file")
            blob = self.gh.repo("git/blobs/" + entry["sha"])
            if blob["encoding"] != "base64":
                raise Blocked("unsupported blob encoding")
            result[path] = base64.b64decode(blob["content"]).decode("utf-8")
        return result

    def context(self):
        """Bind issue requirements and trusted repository guidance to the plan base."""
        paths = ["CONTRIBUTING.md", ".github/copilot-instructions.md",
                 ".agents/skills/stacks-pr-review/SKILL.md"]
        tree = self.tree(self.state["base"])
        paths += [p for p in tree if p.startswith(".github/instructions/") and p.endswith(".md")]
        policies = self.files(self.state["base"], paths)
        # Review instructions are data for review stages; select matching path scopes.
        selected = {}
        for path, content in policies.items():
            if not content:
                continue
            if path.startswith(".github/instructions/"):
                match = re.search(r'^applyTo: "([^"]+)"', content, re.MULTILINE)
                if not match or not any(instruction_matches(p, pattern.strip())
                                       for p in self.state.get("plan", {}).get("files", [])
                                       for pattern in match[1].split(",")):
                    continue
            selected[path] = content
        return {"issue": self.state["requirements"], "policy": selected,
                "plan": self.state.get("plan"), "base": self.state["base"],
                "bundle_id": self.bundle.identity, "charter": self.bundle.charter,
                "prior_outcomes_untrusted": self.state.get("prior_outcomes", [])}

    def ask(self, provider, stage, context):
        """Debit the persistent attempt budget before contacting a model."""
        context = {**context, "repository_commit": self.state.get("head", self.state["base"])}
        if len(json.dumps(context).encode()) > self.config["max_context_bytes"]:
            raise Blocked("context exceeds v1 limit; narrow the issue")
        if self.state["calls"] >= self.config["max_model_calls"]:
            raise Blocked("model-call budget exhausted")
        self.state["calls"] += 1
        self.save()
        return self.agents.ask(provider, stage, context)

    def permitted(self, login):
        """Cache fresh writer permissions within this invocation only."""
        if login not in self.permissions:
            self.permissions[login] = self.gh.permitted(login)
        return self.permissions[login]

    def enrollment(self):
        """Require the current opt-in label to have been applied by a current writer."""
        if not any(label["name"] == "autobot" for label in self.issue.get("labels", [])):
            return None
        events = self.gh.pages(f"issues/{self.issue['number']}/events")
        events = [e for e in events if e["event"] in ("labeled", "unlabeled")
                  and e.get("label", {}).get("name") == "autobot"]
        event = events[-1] if events else None
        actor = event.get("actor") if event else None
        if not actor or event["event"] != "labeled" or not self.permitted(actor["login"]):
            return None
        return {"actor": actor["login"], "event": event["id"]}

    def requirements(self, comments):
        """Snapshot issue text and author/writer replies without treating them as authority."""
        author = self.issue.get("user", {}).get("login")
        discussion = []
        for comment in comments:
            body, login = comment["body"], comment["user"]["login"]
            if body.startswith(MARKER) or body.strip().startswith("/autobot ") or login == self.config["bot_login"]:
                continue
            if login == author or self.permitted(login):
                discussion.append({"author": login, "body": body})
        return {"title": self.issue["title"], "body": self.issue["body"], "discussion": discussion[-20:]}

    def initialize(self, comments, mode, command_id=0, enrollment=None):
        """Create a bounded task while preserving explicit manual mode compatibility."""
        base = self.gh.repo("git/ref/heads/" + self.config["base"])["object"]["sha"]
        self.state = {"version": 2, "issue": self.issue["number"], "mode": mode,
                      "stage": "intake", "enrollment": enrollment,
                      "base": base, "calls": 0, "round": 0, "plan_revisions": 0, "reviews": {},
                      "command_id": command_id, "requirements": self.requirements(comments),
                      "issue_digest": digest([self.issue["title"], self.issue["body"]]),
                      "agent_image": os.environ.get("AUTOBOT_AGENT_IMAGE", ""),
                      "execution_image": os.environ.get("AUTOBOT_EXECUTION_IMAGE", ""),
                      "config_digest": digest(self.config), "bundle_id": self.bundle.identity,
                      "bundle_hashes": self.bundle.hashes, "charter_id": digest(self.bundle.charter),
                      "charter": self.bundle.charter, "design_generation": 0,
                      "design_reopens": 0, "design_votes": {}, "design_history": []}
        self.state["prior_outcomes"] = self.retrieve_outcomes()
        self.save()

    def revise_plan(self, stage, reason, invalidate=True):
        """Limit automatic plan revisions across QC feedback and changing inputs."""
        if invalidate:
            self.invalidate_design(reason)
        if self.state["plan_revisions"] >= self.config["max_plan_revisions"]:
            self.state.update(stage="escalated", reason="Plan revision budget exhausted: " + reason)
            self.save()
            return False
        self.state["plan_revisions"] += 1
        self.state.update(stage=stage, reason=reason)
        return True

    def invalidate_design(self, reason):
        """Archive bounded judgment evidence and invalidate all approval generations."""
        state = self.state
        if state.get("plan_id"):
            state["design_history"].append({"generation": state["design_generation"],
                "plan": state["plan_id"], "summary": state["plan"]["summary"][:500],
                "parent": state.get("head"), "reason": reason[:1000],
                "local_evidence_digest": digest(state.get("local_evidence")),
                "votes": [{"identity": v["identity"], "binding": v["binding"],
                    "decision": v["report"]["decision"], "report_digest": digest(v["report"]),
                    "concerns": v["report"]["concerns"]} for v in state.get("design_votes", {}).values()]})
        state["design_generation"] += 1
        state["design_votes"] = {}
        state.pop("approval", None)
        state.pop("local_evidence", None)

    def reopen_design(self, issue, source):
        """Pause candidate work and preserve the published parent under persistent budgets."""
        validate_design_issue(issue)
        state = self.state
        if state.get("pending"):
            raise Blocked("prepared publication must be reconciled before reopening design")
        state.setdefault("reopen_history", []).append({"source": source, "issue": issue,
            "head": state.get("head"), "plan": state["plan_id"]})
        state["design_feedback"] = issue
        self.invalidate_design(issue["explanation"])
        if state["design_reopens"] >= self.config["max_design_reopens"]:
            state.update(stage="escalated", reason="Design reopen budget exhausted.")
            self.save()
            return
        state["design_reopens"] += 1
        if not self.revise_plan("plan", issue["explanation"], invalidate=False):
            return
        state["design_parent"] = state.get("head")
        if state.get("pr"):
            changed = [f["filename"] for f in self.gh.pages(f"pulls/{state['pr']}/files")]
            if not changed or not all(path_allowed(p) for p in changed):
                raise Blocked("cannot reconcile accumulated PR changes")
            state["changed_files"] = changed
        self.save()

    def record_outcome(self):
        """Persist bounded outcome evidence in the bot-owned GitHub state, without policy edits."""
        state = self.state
        if state["stage"] not in ("merged", "observed_merged", "rejected", "escalated", "blocked", "stopped"):
            return
        outcome = {"version": 1, "status": state["stage"], "issue": state["issue"],
            "title": state["requirements"]["title"][:200], "reason": state.get("reason", "")[:1000],
            "plan_summary": state.get("plan", {}).get("summary", "")[:500],
            "goals": sorted({goal for vote in state.get("design_votes", {}).values()
                             for goal in vote["report"]["scope"]["goal_ids"]}),
            "files": state.get("plan", {}).get("files", []), "head": state.get("head"),
            "plan": state.get("plan_id"), "bundle": state.get("bundle_id"),
            "calls": state["calls"], "local_evidence_digest": digest(state.get("local_evidence")),
            "reviews_digest": digest(state.get("reviews", {})),
            "completion": state.get("completion")}
        history = state.setdefault("outcomes", [])
        if not history or history[-1] != outcome:
            history.append(outcome)
            del history[:-6]

    def retrieve_outcomes(self):
        """Read a fixed GitHub window and select related, explicitly untrusted lessons."""
        state = self.state
        issues = self.gh.outcome_candidates(self.config["max_outcome_issues"])
        words = set(re.findall(r"[a-z]{4,}", state["requirements"]["title"].lower()))
        lessons = []
        for issue, comments in issues[:self.config["max_outcome_issues"]]:
            if issue == state["issue"]:
                continue
            for comment in comments[:10]:
                if (comment.get("user", {}).get("login") != self.config["bot_login"]
                        or not comment.get("body", "").startswith(MARKER)
                        or len(comment["body"].encode()) > 60000):
                    continue
                try:
                    prior = json.loads(comment["body"].rsplit(STATE_PREFIX, 1)[1].rsplit("\n```", 1)[0])
                    if prior.get("issue") != issue or prior.get("version") != 2:
                        continue
                    outcome = prior.get("outcomes", [])[-1]
                    relevance = len(words & set(re.findall(r"[a-z]{4,}", outcome["title"].lower())))
                    if not relevance or outcome.get("version") != 1:
                        continue
                    lesson = {k: outcome.get(k) for k in ("issue", "status", "title", "reason", "plan_summary",
                                                         "goals", "files", "head", "plan", "bundle", "completion")}
                    if len(json.dumps(lesson).encode()) <= 4000:
                        lessons.append((relevance, issue, lesson))
                except (ValueError, KeyError, IndexError, TypeError):
                    continue
        lessons.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [lesson for _, _, lesson in lessons[:self.config["max_outcome_lessons"]]]

    def refresh_autonomous(self, comments):
        """Resume clarification or invalidate stale inputs without resetting any budget."""
        state = self.state
        if self.enrollment() != state["enrollment"]:
            raise Blocked("autonomous enrollment removed, changed or no longer authorized")
        requirements = self.requirements(comments)
        base = self.gh.repo("git/ref/heads/" + self.config["base"])["object"]["sha"]
        changed = requirements != state["requirements"] or base != state["base"]
        if not changed:
            return state["stage"] != "clarify"
        if state.get("head") or state.get("pending"):
            if base != state["base"] or state.get("pending"):
                raise Blocked("approved base or prepared publication changed; reconcile manually")
            if (state["stage"] == "clarify"
                    and all(requirements[key] == state["requirements"][key] for key in ("title", "body"))):
                self.invalidate_design("Clarification answered for the open design.")
                state.update(stage="plan", requirements=requirements)
                self.save()
                return True
            self.reopen_design({"kind": "requirements", "explanation": "Authorized issue requirements changed.",
                                "evidence": [digest(requirements)]}, "requirements")
            state.update(requirements=requirements,
                         issue_digest=digest([self.issue["title"], self.issue["body"]]))
            self.save()
            return state["stage"] == "plan"
        if not self.revise_plan("intake", "Requirements or base changed; QC is reassessing."):
            return False
        state.update(requirements=requirements, base=base,
                     issue_digest=digest([self.issue["title"], self.issue["body"]]))
        for key in ("plan", "plan_id", "branch", "qc"):
            state.pop(key, None)
        self.save()
        return True

    def quality_control(self, stage):
        """Gather independent votes without exposing any peer judgments within a round."""
        state = self.state
        context = self.context()
        member = None
        if stage == "plan_qc":
            roster = design_roster(self.config)
            member = next((m for m in roster if m["id"] not in state["design_votes"]), None)
            if member is None:
                if not consensus_valid(state, self.config):
                    raise Blocked("design votes are missing, stale or contradictory")
                self.finish_consensus()
                self.save()
                return
            context.update(files=self.files(state["base"], state["plan"]["files"]),
                           design_binding=approval_binding(state, self.config), reviewer_identity=member,
                           accumulated_changed_files=state.get("changed_files", []),
                           published_parent=state.get("design_parent"))
        else:
            context["editable_files"] = [p for p in self.tree(state["base"]) if path_allowed(p)]
        if member and state.get("head"):
            context["published_files"] = self.files(state["head"], state["plan"]["files"])
        provider = member["provider"] if member else self.config["qc"]
        report = self.ask(provider, stage, context)
        validate_qc(report, stage, self.bundle.charter)
        state.setdefault("qc", {})[stage] = report
        state.setdefault("qc_history", []).append({"stage": stage, "decision": report["decision"],
            "risk": report["risk"], "rationale": report["rationale"][:500], "report_digest": digest(report),
            "base": state["base"], "plan": state.get("plan_id"), "provider": provider,
            "model": member["model"] if member else selected_model(self.config, provider, stage)})
        state["reason"] = report["rationale"]
        decision = report["decision"]
        if member:
            state["design_votes"][member["id"]] = {"identity": member,
                "binding": approval_binding(state, self.config), "report": report}
        if report["risk"] == "high" or decision == "escalate":
            state.update(stage="escalated", reason="High-risk or consequential work requires human intervention. " + report["rationale"])
        elif decision in ("reject", "clarify"):
            state["stage"] = "rejected" if decision == "reject" else "clarify"
        elif stage == "intake":
            state["stage"] = "plan"
        elif len(state["design_votes"]) == len(design_roster(self.config)):
            objections = [vote["report"] for vote in state["design_votes"].values()
                          if vote["report"]["decision"] != "approve"]
            if objections:
                state["design_feedback"] = objections
                self.revise_plan("plan", "Design reviewers require a revision.")
            else:
                self.finish_consensus()
        if state["stage"] in ("rejected", "escalated"):
            state["retry_stage"] = "intake"
            state.pop("approval", None)
        self.save()

    def finish_consensus(self):
        """Authorize implementation only after a complete current unanimous round."""
        state = self.state
        if not consensus_valid(state, self.config):
            raise Blocked("unanimous design consensus missing or stale")
        if state.get("mode") == "manual":
            state["stage"] = "planned"
        else:
            state.update(stage="implement", approval={"kind": "consensus", "plan": state["plan_id"],
                                                       "binding": approval_binding(state, self.config)})

    def suspend(self, stage, reason=None):
        """Pause work without replacing an earlier resume target on repeated failures."""
        if self.state["stage"] not in ("blocked", "stopped"):
            self.state["retry_stage"] = self.state["stage"]
        self.state["stage"] = stage
        if reason is not None:
            self.state["reason"] = reason
        self.save()

    def commands(self, comments):
        """Apply exact writer commands; plan approval is bound to its full digest."""
        for comment in comments:
            if comment["id"] <= (self.state or {}).get("command_id", 0):
                continue
            text = comment["body"].strip()
            if not re.fullmatch(r"/autobot (start|approve [a-f0-9]{64}|stop|retry)", text):
                continue
            if not self.permitted(comment["user"]["login"]):
                continue
            if text == "/autobot start":
                if self.config["autonomous"]:
                    continue
                if self.state and self.state["stage"] not in ("clarify", "blocked", "stopped"):
                    continue
                if self.state and any(self.state.get(key) for key in ("pr", "head", "pending")):
                    raise Blocked("existing PR or prepared publication requires manual reconciliation before restarting")
                if self.state:
                    if not self.revise_plan("intake", "Writer requested reassessment."):
                        continue
                    base = self.gh.repo("git/ref/heads/" + self.config["base"])["object"]["sha"]
                    self.state.update(base=base, requirements=self.requirements(comments), command_id=comment["id"],
                        issue_digest=digest([self.issue["title"], self.issue["body"]]))
                    for key in ("plan", "plan_id", "branch", "qc", "design_parent", "approved_accumulated_files"):
                        self.state.pop(key, None)
                    self.save()
                else:
                    self.initialize([c for c in comments if c["id"] <= comment["id"]], "manual", comment["id"])
            elif (self.state and text.startswith("/autobot approve ") and self.state["stage"] == "planned"
                  and self.state.get("mode", "manual") == "manual"):
                if text.split()[-1] == self.state["plan_id"]:
                    if not consensus_valid(self.state, self.config):
                        raise Blocked("writer command cannot waive design consensus")
                    self.state.update(stage="implement", command_id=comment["id"],
                                      approval={"kind": "human", "binding": approval_binding(self.state, self.config),
                                                "plan": self.state["plan_id"],
                                                "actor": comment["user"]["login"], "comment": comment["id"]})
                    self.save()
            elif (self.state and text == "/autobot retry" and self.state["stage"] in ("blocked", "stopped", "rejected", "escalated")
                  and self.state.get("retry_stage")):
                if self.state["stage"] in ("rejected", "escalated"):
                    if not self.revise_plan("intake", "Writer requested reassessment after terminal judgment."):
                        continue
                    self.state.pop("retry_stage", None)
                    self.state["command_id"] = comment["id"]
                else:
                    self.state.update(stage=self.state.pop("retry_stage"), command_id=comment["id"])
                self.state.pop("reason", None)
                self.save()
            elif self.state and text == "/autobot stop":
                self.state["command_id"] = comment["id"]
                self.suspend("stopped")

    def plan(self):
        """Triage and attach a structured plan without making code changes."""
        context = self.context()
        context["design_feedback"] = self.state.get("design_feedback")
        context["editable_files"] = [p for p in self.tree(self.state["base"]) if path_allowed(p)]
        if self.state.get("head"):
            paths = self.state["changed_files"]
            context.update(accumulated_before=self.files(self.state["base"], paths),
                           accumulated_after=self.files(self.state["head"], paths))
        plan = self.ask(self.config["planner"], "plan", context)
        validate_plan(plan)
        if self.state.get("head") and not plan["questions"]:
            if not set(self.state.get("changed_files", [])).issubset(plan["files"]):
                raise Blocked("revised scope does not reconcile the accumulated PR diff")
        self.state["plan"] = plan
        self.state["plan_id"] = digest({"plan": plan, "base": self.state["base"],
                                        "requirements": self.state["requirements"]})
        if not self.state.get("head"):
            self.state["branch"] = f"autobot/issue-{self.issue['number']}-{self.state['plan_id'][:12]}"
        self.state["design_votes"] = {}
        self.state["approved_accumulated_files"] = self.state.get("changed_files", [])
        self.state["design_parent"] = self.state.get("head")
        self.state["stage"] = "clarify" if plan["questions"] else "plan_qc"
        self.save()

    def local_report(self, attempt, report):
        """Persist compact evidence to GitHub and optional full diagnostics as a CI artifact."""
        state = self.state
        compact = {k: v for k, v in report.items() if k not in ("edits", "checks")}
        compact["checks"] = [{k: v for k, v in c.items() if k != "output"} for c in report.get("checks", [])]
        compact.update(attempt=attempt, round=state["round"], run_id=os.environ.get("GITHUB_RUN_ID"))
        state["local_status"] = compact
        directory = os.environ.get("AUTOBOT_LOCAL_REPORT_DIR")
        if directory:
            path = Path(directory)
            path.mkdir(parents=True, exist_ok=True)
            path.joinpath(f"issue-{state['issue']}-round-{state['round']}-attempt-{attempt}.json").write_text(json.dumps(report))
        self.save()

    def design_authorization(self):
        """Attest the supplied implementation plan only from current validated controller consensus."""
        state = self.state
        if not approval_valid(state, self.config):
            raise Blocked("implementation authorization requires complete current design approval")
        roster = design_roster(self.config)
        return {"verified_by": "controller", "approval_kind": state["approval"]["kind"],
                "binding": approval_binding(state, self.config), "plan_id": state["plan_id"],
                "plan_digest": digest(state["plan"]), "base": state["base"],
                "bundle_id": state["bundle_id"], "charter_id": state["charter_id"],
                "requirements_digest": digest(state["requirements"]),
                "generation": state["design_generation"], "required_identities": roster,
                "successful_votes": [{"identity": member, "binding": state["design_votes"][member["id"]]["binding"],
                                      "decision": "approve"}
                                     for member in roster]}

    def develop_locally(self, parent, originals):
        """Iterate edits, real tests and one local review before creating any remote Git objects."""
        state = self.state
        self.design_authorization()
        deadline = time.monotonic() + self.config["local_timeout_seconds"]
        required = required_local_checks(state["plan"]["files"])
        feedback = None
        round_key = f"{state['design_generation']}:{state['round']}"
        attempts = state.setdefault("local_attempts", {})
        if attempts.get(round_key, 0) > self.config["max_local_fix_rounds"]:
            raise Blocked("local repair budget exhausted")
        archive = self.gh.archive(parent)
        with self.workspace_factory(self.config, archive, state["branch"]) as workspace:
            if workspace.files(state["plan"]["files"]) != originals:
                raise Blocked("source archive does not match the pinned GitHub files")
            workspace.prepare(required, min(600, deadline - time.monotonic()))
            for attempt in range(attempts.get(round_key, 0), self.config["max_local_fix_rounds"] + 1):
                if deadline - time.monotonic() < 600:
                    raise Blocked("local time budget cannot accommodate another agent turn")
                attempts[round_key] = attempt + 1
                context = self.context()
                context.update(files=workspace.files(state["plan"]["files"]), prior_reviews=state["reviews"],
                               local_feedback=feedback, available_checks=available_checks(required), required_checks=required,
                               design_authorization=self.design_authorization())
                result = self.ask(self.config["implementer"], "develop", context)
                if not isinstance(result, dict) or set(result) != {"summary", "edits", "checks", "design_issue"}:
                    raise Blocked("invalid local development response")
                if result["design_issue"] is not None:
                    if result["edits"] or result["checks"]:
                        raise Blocked("design reopening cannot include candidate edits or checks")
                    self.reopen_design(result["design_issue"], "implementation")
                    return None
                proposal = {"summary": result["summary"], "edits": result["edits"]}
                if result["edits"]:
                    validate_edits(proposal, state["plan"], context["files"])
                elif not workspace.export()["edits"]:
                    raise Blocked("initial development attempt must propose changes")
                checkpoint = workspace.checkpoint(result["edits"])
                reports = workspace.run_checks(required, result["checks"], deadline)
                export = workspace.export()
                record = {**export, "checks": reports, "image": state["execution_image"]}
                self.local_report(attempt, record)
                if any(resource_exhausted(c) for c in reports):
                    raise Blocked("local check exhausted execution resources; inspect diagnostics and runner capacity before retrying")
                if any(c["exit_code"] != 0 for c in reports):
                    feedback = {"checks": reports}
                    continue
                if deadline - time.monotonic() < 600:
                    raise Blocked("local time budget cannot accommodate pre-publication review")
                review_context = self.context()
                review_context.update(before=self.files(state["base"], state["plan"]["files"]),
                                      after=workspace.files(state["plan"]["files"]), local_checks=reports,
                                      local_commit=checkpoint["local_commit"])
                review = self.ask(self.config["local_reviewer"], "local_review", review_context)
                validate_review(review)
                if review["verdict"] == "reopen":
                    self.reopen_design(review["design_issue"], "local_review")
                    return None
                record["review"] = review
                self.local_report(attempt, record)
                if review["verdict"] != "approve":
                    feedback = {"checks": reports, "review": review}
                    continue
                final = {"summary": result["summary"], "edits": export["edits"]}
                validate_edits(final, state["plan"], originals)
                evidence = {k: v for k, v in record.items() if k != "edits"}
                evidence["checks"] = [{k: v for k, v in c.items() if k != "output"} for c in reports]
                evidence.update(parent=parent, edits_digest=digest(export["edits"]),
                                reviewer=self.config["local_reviewer"],
                                model=selected_model(self.config, self.config["local_reviewer"], "local_review") or "CLI default")
                return final, evidence
        raise Blocked("local tests or review still fail; local repair budget exhausted")

    def implement(self):
        """Publish Git objects only after isolated tests and the local review pass."""
        state = self.state
        parent = state.get("head", state["base"])
        originals = self.files(parent, state["plan"]["files"])
        developed = self.develop_locally(parent, originals)
        if developed is None:
            return
        result, evidence = developed
        edits = validate_edits(result, state["plan"], originals)
        # Git Trees API accepts content directly, avoiding filesystem extraction and hooks.
        parent_commit = self.gh.repo("git/commits/" + parent)
        tree = self.gh.repo("git/trees", "POST", {"base_tree": parent_commit["tree"]["sha"], "tree": [
            {"path": e["path"], "mode": "100644", "type": "blob", "content": e["content"]} for e in edits]})
        commit = self.gh.repo("git/commits", "POST", {
            "message": f"fix(autobot): address issue #{state['issue']} (round {state['round']})",
            "tree": tree["sha"], "parents": [parent]})
        state["pending"] = {"head": commit["sha"], "parent": parent,
                            "summary": result["summary"], "initial": "head" not in state}
        state["local_evidence"] = {**evidence, "head": commit["sha"]}
        state["stage"] = "publish"
        self.save()

    def publish(self):
        """Reconcile interrupted publication without overwriting a foreign branch tip."""
        state, pending = self.state, self.state["pending"]
        if not local_evidence_valid(state):
            raise Blocked("local tests and review do not authorize this publication")
        refs = self.gh.repo("git/matching-refs/heads/" + state["branch"])
        refs = [r for r in refs if r["ref"] == "refs/heads/" + state["branch"]]
        if refs:
            current = refs[0]["object"]["sha"]
            if current != pending["head"]:
                if pending["initial"] or current != pending["parent"]:
                    raise Blocked("branch moved outside the controller")
                self.gh.repo("git/refs/heads/" + state["branch"], "PATCH",
                             {"sha": pending["head"], "force": False})
        elif pending["initial"]:
            self.gh.repo("git/refs", "POST", {"ref": "refs/heads/" + state["branch"], "sha": pending["head"]})
        else:
            raise Blocked("implementation branch disappeared")
        owner = self.config["repository"].split("/")[0]
        prs = self.gh.pages("pulls?state=all&head=" + quote(owner + ":" + state["branch"]) +
                            "&base=" + self.config["base"])
        if len(prs) > 1 or (prs and prs[0]["state"] != "open"):
            raise Blocked("PR was closed or duplicated; reconcile manually")
        if prs:
            pr = prs[0]
        else:
            pr = self.gh.repo("pulls", "POST", {"title": f"fix: address issue #{state['issue']}",
                "head": state["branch"], "base": self.config["base"],
                "body": f"Closes #{state['issue']}\n\nPlan `{state['plan_id']}`.\n\n"
                        + pending["summary"] + "\n\nGenerated changes; CI and independent reviews are pending."})
        if state["reviews"]:
            state.setdefault("review_history", []).append(state["reviews"])
        state.update(head=pending["head"], pr=pr["number"], reviews={}, stage="review")
        state["changed_files"] = [f["filename"] for f in self.gh.pages(f"pulls/{pr['number']}/files")]
        del state["pending"]
        self.save()

    def review(self):
        """Run one fresh provider review against the same immutable before/after files."""
        state = self.state
        provider = next((p for p in self.config["reviewers"] if p not in state["reviews"]), None)
        if provider:
            context = self.context()
            context.update(before=self.files(state["base"], state["plan"]["files"]),
                           after=self.files(state["head"], state["plan"]["files"]), head=state["head"])
            context["previous_findings"] = [r[provider] for r in state.get("review_history", []) if provider in r]
            report = self.ask(provider, "review", context)
            validate_review(report)
            if report["verdict"] == "reopen":
                self.reopen_design(report["design_issue"], "pr_review:" + provider)
                return
            report.update(head=state["head"], model=self.config["models"][provider] or "CLI default")
            state["reviews"][provider] = report
            self.save()
        if len(state["reviews"]) == len(self.config["reviewers"]):
            if all(r["verdict"] == "approve" for r in state["reviews"].values()):
                state["stage"] = "gates"
            elif state["round"] < self.config["max_fix_rounds"]:
                state["round"] += 1
                state["stage"] = "implement"
            else:
                raise Blocked("independent reviews still request changes; fix budget exhausted")
            self.save()

    def gates(self):
        """Check fresh GitHub evidence and merge only the reviewed SHA, without admin bypass."""
        state = self.state
        pr = self.gh.repo(f"pulls/{state['pr']}")
        files = [f["filename"] for f in self.gh.pages(f"pulls/{state['pr']}/files")]
        checks = self.gh.pages(f"commits/{state['head']}/check-runs?filter=latest", "check_runs")
        reasons = merge_reasons(state, pr, files, checks, self.gh.threads(state["pr"]),
                                self.gh.pages(f"pulls/{state['pr']}/reviews"), self.config)
        if reasons:
            reason = "; ".join(reasons)
            if state.get("reason") != reason:
                state["reason"] = reason
                self.save()
            return False
        # Require server-side protection so a concurrent base/check change cannot bypass gates.
        protection = self.gh.repo("branches/" + self.config["base"] + "/protection")
        required = protection.get("required_status_checks") or {}
        names = set(required.get("contexts", [])) | {c["context"] for c in required.get("checks", [])}
        if not required.get("strict") or not set(self.config["required_checks"]).issubset(names):
            raise Blocked("strict branch protection must require both Autobot CI checks")
        if not protection.get("required_conversation_resolution", {}).get("enabled"):
            raise Blocked("branch protection must require conversation resolution")
        if not protection.get("enforce_admins", {}).get("enabled"):
            raise Blocked("branch protection must apply to administrators")
        # Save intent: a lost merge response is reconciled from GitHub on the next tick.
        state["stage"] = "merging"
        self.save()
        response = self.gh.repo(f"pulls/{state['pr']}/merge", "PUT",
                                {"sha": state["head"], "merge_method": "squash"})
        if not response.get("merged"):
            raise Blocked("GitHub rejected the guarded merge")
        state.update(stage="merged", merge_sha=response["sha"], reason="All merge gates passed.",
                     completion={"kind": "controller", "head": state["head"], "merge_sha": response["sha"],
                                 "pr": state["pr"], "gates_verified": True})
        self.save()
        return True

    def observe_merge(self, pr):
        """Record external completion without claiming that controller gates authorized it."""
        state = self.state
        if pr.get("head", {}).get("sha") != state.get("head") or not state.get("head"):
            raise Blocked("merged PR head differs from the exact published controller head")
        if not pr.get("merge_commit_sha"):
            raise Blocked("merged PR lacks completion evidence")
        intent = state["stage"] == "merging" or (state["stage"] == "blocked" and state.get("retry_stage") == "merging")
        actor = (pr.get("merged_by") or {}).get("login")
        controlled = intent and actor == self.config["bot_login"]
        state["completion"] = {"kind": "controller_reconciled" if controlled else "observed_external",
            "head": state["head"], "merge_sha": pr["merge_commit_sha"], "pr": state["pr"],
            "gates_verified": controlled, "observed_stage": state["stage"],
            "merged_by": actor}
        state.update(stage="merged" if controlled else "observed_merged", merge_sha=pr["merge_commit_sha"],
            reason="Controller merge response reconciled." if controlled else
                   "Observed an external merge of the exact published head; controller merge gates are not attested.")
        self.save()

    def advance(self, number):
        """Process writer commands, validate current authority, and advance one stage."""
        self.issue = self.gh.repo(f"issues/{number}")
        if "pull_request" in self.issue:
            return False
        comments = self.gh.pages(f"issues/{number}/comments")
        self.load(comments)
        if self.state and (self.state.get("bundle_id") != self.bundle.identity
                           or self.state.get("charter_id") != digest(self.bundle.charter)
                           or digest(self.state.get("charter")) != self.state.get("charter_id")
                           or self.state.get("bundle_hashes") != self.bundle.hashes):
            raise Blocked("trusted charter, role, skill or schema bundle changed; operator reconciliation required")
        if self.state and self.state.get("pr") and self.state["stage"] not in ("merged", "observed_merged"):
            pr = self.gh.repo(f"pulls/{self.state['pr']}")
            if pr.get("merged"):
                self.observe_merge(pr)
                return True
        if self.issue["state"] != "open":
            if self.state and self.state["stage"] not in ("merged", "observed_merged", "stopped"):
                self.state.update(stage="stopped", reason="Issue closed without a confirmed exact-head merge.")
                self.save()
                return True
            return False
        if not self.state and self.config["autonomous"]:
            enrollment = self.enrollment()
            if enrollment:
                self.initialize(comments, "autonomous", enrollment=enrollment)
        self.commands(comments)
        if not self.state or self.state["stage"] in ("planned", "blocked", "stopped", "merged", "observed_merged", "rejected", "escalated"):
            return False
        state = self.state
        if state["stage"] == "merging":
            state["stage"] = "gates"
        if state["config_digest"] != digest(self.config):
            raise Blocked("controller configuration changed; re-plan required")
        if state["agent_image"] != os.environ.get("AUTOBOT_AGENT_IMAGE", ""):
            raise Blocked("agent image changed; re-plan required")
        if state["execution_image"] != os.environ.get("AUTOBOT_EXECUTION_IMAGE", ""):
            raise Blocked("execution image changed; re-plan required")
        if state.get("mode") == "autonomous":
            if not self.refresh_autonomous(comments):
                return False
        elif state["stage"] == "clarify":
            return False
        if state["issue_digest"] != digest([self.issue["title"], self.issue["body"]]):
            raise Blocked("issue requirements changed; re-plan required")
        if self.gh.repo("git/ref/heads/" + self.config["base"])["object"]["sha"] != state["base"]:
            raise Blocked("base moved; re-plan required")
        if state.get("mode") == "manual" and state.get("approval"):
            self.check_human_approval(comments)
        if state["stage"] in ("implement", "publish", "review", "gates") and not approval_valid(state, self.config):
            raise Blocked("approved plan missing or stale")
        if state.get("head") and state["stage"] != "publish":
            pr = self.gh.repo(f"pulls/{state['pr']}")
            if pr["state"] != "open" or pr["head"]["sha"] != state["head"]:
                raise Blocked("PR closed or head changed outside the controller")
        method = {"intake": lambda: self.quality_control("intake"),
                  "plan_qc": lambda: self.quality_control("plan_qc"),
                  "plan": self.plan, "implement": self.implement, "publish": self.publish,
                  "review": self.review, "gates": self.gates}.get(state["stage"])
        if method is None:
            raise Blocked("unknown state")
        result = method()
        return result is not False

    def check_human_approval(self, comments):
        """Retain revocable writer approval for explicitly configured manual mode."""
        state = self.state
        if not self.permitted(state["approval"]["actor"]):
            raise Blocked("plan approver no longer has write access")
        if state.get("approval") and not any(
                c["id"] == state["approval"]["comment"]
                and c["user"]["login"] == state["approval"]["actor"]
                and c["body"].strip() == "/autobot approve " + state["plan_id"] for c in comments):
            raise Blocked("approval comment changed or was removed")


def main():
    """Run from the trusted default branch; schedule scans the opt-in label."""
    config = json.loads(Path(".autobot/config.json").read_text())
    validate_config(config)
    if os.environ.get("GITHUB_REPOSITORY", config["repository"]) != config["repository"]:
        raise Blocked("repository identity mismatch")
    gh = GitHub(config["repository"])
    number = os.environ.get("AUTOBOT_ISSUE", "")
    if number:
        if not number.isdecimal() or int(number) <= 0:
            raise Blocked("invalid issue number")
        numbers = [int(number)]
    else:
        numbers = QueueScan(gh, config)
    for number in numbers:
        controller = Controller(gh, Agents(config), config)
        try:
            if controller.advance(number):
                break
        except Exception as error:
            # Do not publish raw CLI/provider errors: they can contain credentials or injected text.
            if controller.state:
                reason = str(error) if isinstance(error, Blocked) else "Transport or schema failure; inspect runner health."
                controller.suspend("blocked", reason)
            print(f"Autobot stopped safely on issue {number}", file=sys.stderr)
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
