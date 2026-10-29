"""Pure validation rules shared by the controller and offline tests."""

import hashlib
import json
import re
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import PurePosixPath

IMAGE = re.compile(r"(?:[a-zA-Z0-9./:_-]+@)?sha256:[a-f0-9]{64}")
PROVIDER_CREDENTIALS = {
    "codex": ("CODEX_API_KEY",),
    "claude": ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"),
    "copilot": ("COPILOT_GITHUB_TOKEN",),
}
SECRET_NAMES = frozenset(name for names in PROVIDER_CREDENTIALS.values() for name in names)


def instruction_matches(path, pattern):
    """Match path segments with ** covering zero or more directories."""
    parts, patterns = path.split("/"), pattern.split("/")

    @lru_cache(maxsize=None)
    def match(index, pattern_index):
        """Memoize wildcard branches within this bounded path comparison."""
        if pattern_index == len(patterns):
            return index == len(parts)
        if patterns[pattern_index] == "**":
            return (match(index, pattern_index + 1)
                    or index < len(parts) and match(index + 1, pattern_index))
        return (index < len(parts) and fnmatchcase(parts[index], patterns[pattern_index])
                and match(index + 1, pattern_index + 1))

    return match(0, 0)


class Blocked(ValueError):
    """An input cannot safely advance the pipeline."""


def digest(value):
    """Bind approvals to a canonical JSON value."""
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def selected_model(config, provider, stage):
    """Allow a separate QC model while retaining provider defaults for other roles."""
    if stage in ("intake", "plan_qc") and config["qc_model"]:
        return config["qc_model"]
    if stage == "local_review" and config["local_review_model"]:
        return config["local_review_model"]
    return config["models"][provider]


def required_local_checks(paths):
    """Require whitespace validation and Rust checks whenever the plan includes Rust."""
    return ["diff"] + (["format", "check", "nextest"] if any(p.endswith(".rs") for p in paths) else [])


def local_evidence_valid(state):
    """Require passing execution and local review evidence for the commit being published."""
    evidence = state.get("local_evidence", {})
    head = state.get("pending", {}).get("head", state.get("head"))
    if not head or evidence.get("head") != head or not evidence.get("tree") or not evidence.get("local_commit"):
        return False
    if evidence.get("image") != state.get("execution_image"):
        return False
    checks = evidence.get("checks", [])
    names = [c.get("name") for c in checks]
    if len(names) != len(set(names)) or not set(required_local_checks(state["plan"]["files"])).issubset(names):
        return False
    if any(c.get("exit_code") != 0 or c.get("timed_out") or c.get("output_limited")
           or c.get("tree") != evidence["tree"] for c in checks):
        return False
    review = evidence.get("review", {})
    try:
        validate_review({k: v for k, v in review.items() if k not in ("head", "model")})
    except (Blocked, TypeError):
        return False
    return review["verdict"] == "approve"


def validate_qc(report, stage, charter=None):
    """Reject incomplete or contradictory decisions before they can authorize work."""
    if not isinstance(report, dict) or set(report) != {"decision", "rationale", "risk", "questions", "concerns", "scope"}:
        raise Blocked("invalid QC fields")
    decisions = {"clarify", "reject", "escalate"} | ({"proceed"} if stage == "intake" else {"approve", "revise"})
    if report["decision"] not in decisions or report["risk"] not in ("low", "medium", "high"):
        raise Blocked("invalid QC decision or risk")
    if not isinstance(report["rationale"], str) or not report["rationale"].strip():
        raise Blocked("QC rationale missing")
    for key in ("questions", "concerns"):
        if not isinstance(report[key], list) or not all(isinstance(v, str) and v.strip() for v in report[key]):
            raise Blocked("invalid QC questions or concerns")
    if len(json.dumps(report)) > 6000:
        raise Blocked("QC report too large")
    scope = report["scope"]
    if (not isinstance(scope, dict) or set(scope) != {"goal_ids", "fit", "conflicts"}
            or not isinstance(scope["goal_ids"], list) or not isinstance(scope["conflicts"], list)
            or not all(isinstance(v, str) and v.strip() for v in scope["goal_ids"] + scope["conflicts"])
            or not isinstance(scope["fit"], str) or not scope["fit"].strip()
            or len(scope["goal_ids"]) != len(set(scope["goal_ids"]))):
        raise Blocked("incomplete charter scope judgment")
    if charter is not None and not set(scope["goal_ids"]).issubset(charter["goals"]):
        raise Blocked("scope cites an unknown charter goal")
    decision = report["decision"]
    if decision in ("proceed", "approve") and (not scope["goal_ids"] or scope["conflicts"]):
        raise Blocked("acceptance contradicts charter scope")
    if decision in ("proceed", "approve") and (report["questions"] or report["concerns"]):
        raise Blocked("QC acceptance contradicts outstanding questions or concerns")
    if decision == "clarify" and not report["questions"]:
        raise Blocked("QC clarification requires questions")
    if decision == "revise" and (not report["concerns"] or report["questions"]):
        raise Blocked("QC revision requires concerns and no unanswered questions")


def design_roster(config):
    """Deduplicate every participating provider/model identity, retaining role provenance."""
    members = [(config["planner"], "plan"), (config["implementer"], "develop"),
               (config["qc"], "intake"), (config["local_reviewer"], "local_review")]
    members += [(provider, "review") for provider in config["reviewers"]]
    roster = {}
    for provider, stage in members:
        model = selected_model(config, provider, stage)
        key = digest([provider, model])
        entry = roster.setdefault(key, {"id": key, "provider": provider, "model": model, "roles": []})
        if stage not in entry["roles"]:
            entry["roles"].append(stage)
    return list(roster.values())


def approval_binding(state, config):
    """Bind every vote to the exact design generation, governance and participating identities."""
    return digest({"plan": state["plan_id"], "plan_content": state["plan"], "base": state["base"],
                   "requirements": state["requirements"], "config": state["config_digest"],
                   "image": state["agent_image"], "execution_image": state["execution_image"],
                   "bundle": state.get("bundle_id"), "charter": state.get("charter_id"),
                   "generation": state.get("design_generation"), "roster": design_roster(config),
                   "parent": state.get("design_parent"), "changed_files": state.get("approved_accumulated_files", [])})


def consensus_valid(state, config):
    """Require complete, fresh unanimous judgments; code cannot prove semantic alignment."""
    roster = design_roster(config)
    votes = state.get("design_votes", {})
    if not state.get("bundle_id") or not state.get("charter_id") or set(votes) != {r["id"] for r in roster}:
        return False
    binding = approval_binding(state, config)
    for member in roster:
        vote = votes[member["id"]]
        if vote.get("identity") != member or vote.get("binding") != binding:
            return False
        report = vote.get("report")
        try:
            validate_qc(report, "plan_qc", state["charter"])
        except (Blocked, TypeError, KeyError):
            return False
        if report["decision"] != "approve" or report["risk"] == "high":
            return False
    return True


def approval_valid(state, config):
    """Manual commands add writer approval; they cannot waive the unanimous design gate."""
    approval = state.get("approval", {})
    if (approval.get("plan") != state.get("plan_id") or not consensus_valid(state, config)
            or approval.get("binding") != approval_binding(state, config)):
        return False
    return approval.get("kind") == ("human" if state.get("mode") == "manual" else "consensus")


def validate_design_issue(issue):
    """Require concrete evidence before reopening requirements or design."""
    if (not isinstance(issue, dict) or set(issue) != {"kind", "explanation", "evidence"}
            or issue["kind"] not in ("requirements", "plan")
            or not isinstance(issue["explanation"], str) or not issue["explanation"].strip()
            or not isinstance(issue["evidence"], list) or not issue["evidence"]
            or not all(isinstance(v, str) and v.strip() for v in issue["evidence"])
            or len(json.dumps(issue)) > 4000):
        raise Blocked("design reopening requires bounded structured evidence")


def path_allowed(path):
    """Reject traversal, automation, dependency and executable configuration edits."""
    if not isinstance(path, str) or not path or len(path) > 240:
        return False
    parts = path.split("/")
    forbidden = {"AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "SECURITY.md", "Cargo.toml", "Cargo.lock", "build.rs"}
    return (
        not PurePosixPath(path).is_absolute()
        and all(p not in ("", ".", "..") and not p.startswith(".") for p in parts)
        and not any(p in forbidden or p.startswith("rust-toolchain") for p in parts)
        and "\\" not in path
        and all(c.isalnum() or c in "/_.-" for c in path)
        and path.endswith((".rs", ".md", ".added", ".changed", ".fixed", ".removed"))
        and path != "docs/autobot.md"
        and not path.startswith(("tools/", "contrib/", "scripts/"))
    )


def validate_plan(plan):
    """Require a bounded, reviewable plan or explicit clarification questions."""
    if set(plan) != {"summary", "questions", "files", "steps", "tests"}:
        raise Blocked("invalid plan fields")
    for key in ("questions", "files", "steps", "tests"):
        if not isinstance(plan[key], list) or not all(isinstance(v, str) for v in plan[key]):
            raise Blocked("invalid plan list")
    if not isinstance(plan["summary"], str) or not plan["summary"].strip():
        raise Blocked("plan summary missing")
    if len(json.dumps(plan)) > 20000:
        raise Blocked("plan too large")
    if plan["questions"]:
        return
    if not 1 <= len(plan["files"]) <= 8 or len(set(plan["files"])) != len(plan["files"]):
        raise Blocked("plan must name 1–8 distinct files")
    if not all(path_allowed(p) for p in plan["files"]):
        raise Blocked("plan includes protected or unsupported files")
    if not plan["steps"] or not plan["tests"]:
        raise Blocked("plan needs steps and validation criteria")


def validate_edits(result, plan, originals):
    """Accept complete text replacements only within the approved file set."""
    if set(result) != {"summary", "edits"} or not isinstance(result["summary"], str):
        raise Blocked("invalid implementation response")
    edits = result["edits"]
    if not isinstance(edits, list) or not 1 <= len(edits) <= 8:
        raise Blocked("implementation must contain 1–8 edits")
    seen = set()
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {"path", "content"}:
            raise Blocked("invalid edit fields")
        path, content = edit["path"], edit["content"]
        if not path_allowed(path) or path not in plan["files"] or path in seen:
            raise Blocked("edit outside approved scope or duplicate path")
        if not isinstance(content, str) or "\x00" in content or len(content.encode()) > 128000:
            raise Blocked("invalid text replacement")
        if originals.get(path) == content:
            raise Blocked("unchanged replacement")
        seen.add(path)
    if len(json.dumps(result).encode()) > 256000:
        raise Blocked("implementation too large")
    return edits


def validate_review(review):
    """Reject contradictory approvals and incomplete structured findings."""
    if set(review) != {"verdict", "summary", "findings", "design_issue"}:
        raise Blocked("invalid review fields")
    if review["verdict"] not in ("approve", "changes_requested", "reopen"):
        raise Blocked("invalid review verdict")
    if not isinstance(review["summary"], str) or not isinstance(review["findings"], list):
        raise Blocked("invalid review content")
    if review["verdict"] == "reopen":
        validate_design_issue(review["design_issue"])
        if review["findings"]:
            raise Blocked("design reopening cannot also request implementation fixes")
        return
    if review["design_issue"] is not None:
        raise Blocked("review verdict contradicts design issue")
    if (review["verdict"] == "approve") != (not review["findings"]):
        raise Blocked("review verdict contradicts findings")
    if len(json.dumps(review)) > 16000:
        raise Blocked("review too large")
    for finding in review["findings"]:
        if set(finding) != {"path", "detail"} or not all(isinstance(v, str) for v in finding.values()):
            raise Blocked("invalid finding")


def merge_reasons(state, pr, files, checks, threads, reviews, config):
    """Return every failed merge gate; missing evidence always blocks."""
    reasons = []
    head = pr["head"]["sha"]
    if not config["merge_enabled"]:
        reasons.append("autonomous merge is disabled")
    if pr["state"] != "open" or pr["draft"] or pr.get("mergeable") is not True:
        reasons.append("PR is not open, ready and mergeable")
    if pr.get("mergeable_state") != "clean":
        reasons.append("GitHub mergeability is not clean")
    if pr["base"]["ref"] != config["base"] or pr["base"]["sha"] != state["base"]:
        reasons.append("base moved; a new plan is required")
    if pr["head"]["repo"]["full_name"] != config["repository"]:
        reasons.append("foreign head repository")
    if pr["head"]["ref"] != state["branch"] or head != state.get("head"):
        reasons.append("head changed outside the controller")
    if not approval_valid(state, config):
        reasons.append("approved plan missing or stale")
    if not local_evidence_valid(state):
        reasons.append("local test or review evidence missing or stale")
    reports = state.get("reviews", {})
    for provider in config["reviewers"]:
        report = reports.get(provider, {})
        if report.get("head") != head or report.get("verdict") != "approve" or report.get("findings"):
            reasons.append(provider + " approval missing or stale")
    if not files or set(files) != set(state.get("changed_files", [])):
        reasons.append("PR file set differs from controller evidence")
    if not all(path_allowed(p) and p in state["plan"]["files"] for p in files):
        reasons.append("unapproved path")
    if not all(p.startswith(("docs/", "changelog.d/")) and not p.endswith(".rs") for p in files):
        reasons.append("v1 autonomous merge is limited to documentation and changelogs")
    for name in config["required_checks"]:
        candidates = [c for c in checks if c["name"] == name]
        if len(candidates) != 1:
            reasons.append("required check missing or ambiguous: " + name)
        else:
            check = candidates[0]
            if (check.get("head_sha") != head or check.get("status") != "completed"
                    or check.get("conclusion") != "success"
                    or check.get("app", {}).get("slug") != "github-actions"):
                reasons.append("required check unsuccessful or untrusted: " + name)
    if any(not t["isResolved"] for t in threads):
        reasons.append("unresolved review threads")
    latest = {}
    for review in reviews:
        if review["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[review["user"]["login"]] = review["state"]
    if "CHANGES_REQUESTED" in latest.values():
        reasons.append("outstanding human changes requested")
    return reasons
