"""Publish a bounded, recoverable outbox as a chronological issue conversation."""

from datetime import datetime, timezone
import json

from policy import Blocked, digest
from checkpoints import CheckpointError

MARKER = "<!-- stacks-autobot:event:v1:"


class PublicationError(CheckpointError):
    """A fixed diagnostic for delivery failure, separate from checkpoint integrity."""


def render(state, kind, source, details):
    """Render full public text from one checkpoint-owned source without duplicating it."""
    value = state
    for key in source:
        value = value[key]
    if kind == "plan":
        lines = [f"Plan `{details['plan']}` · round {details['round']}", "", value["summary"], "",
                 *["- " + step for step in value["steps"]], "", "Validation:", "",
                 *["- " + test for test in value["tests"]]]
        if value["responses"]:
            lines += ["", "Responses to review:", "",
                      *[f"- **{v['id']}**: {v['response']}" for v in value["responses"]]]
        if value["questions"]:
            lines += ["", "Questions:", "", *["- " + q for q in value["questions"]]]
        return "\n".join(lines)
    if kind == "qc":
        minimal = {"plan_id": details["plan"], "design_generation": details["round"] - 1,
                   "objections": [{"id": identifier, "reviewer": details["reviewer"], "resolved": False}
                                  for identifier in details["resolved"]]}
        return review_body(minimal, value, details["phase"], {"id": details["reviewer"]})
    if kind == "local":
        checks = ", ".join(f"{c['name']}: exit {c['exit_code']}" for c in value["checks"])
        body = f"Attempt {value['attempt'] + 1} · commit `{value.get('local_commit', 'pending')}`\n\n{checks}"
        if value.get("review"):
            report = value["review"]
            body += f"\n\n{details['reviewer']}: **{report['verdict']}** — {report['summary']}"
            body += "".join("\n\n- " + f["path"] + ": " + f["detail"] for f in report["findings"])
        return body
    if kind == "review":
        body = f"Commit `{details['head']}` · round {details['round']}\n\n{value['summary']}"
        body += "".join("\n\n- " + f["path"] + ": " + f["detail"] for f in value["findings"])
        if value["design_issue"]:
            body += "\n\n" + value["design_issue"]["explanation"]
        return body
    raise ValueError("unknown conversation renderer")


def content(event, body):
    """Build the exact immutable text used for publication and reconciliation."""
    return (f"{MARKER}{event['id']} -->\n### {event['title']}\n\n"
            f"Event {event['sequence']} · {event['at']}\n\n{body}")


def queue(state, title, body=None, *, source=None, kind=None, details=None):
    """Persist a bounded event descriptor; large reports stay in their existing state fields."""
    journal = state.setdefault("conversation", {"sequence": 0, "pending": []})
    if source is not None:
        body = render(state, kind, source, details)
    if len(body.encode()) > 24000 or len(journal["pending"]) >= 8:
        raise Blocked("conversation event exceeds publication bounds")
    journal["sequence"] += 1
    sequence = journal["sequence"]
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    identifier = digest([state["issue"], sequence, title, body, timestamp])
    event = {"id": identifier, "title": title, "sequence": sequence, "at": timestamp}
    rendered = content(event, body)
    if source is None:
        event = {"id": identifier, "body": rendered}
    else:
        event.update(source=list(source), kind=kind, details=details, content_digest=digest(rendered))
    journal["pending"].append(event)


def event_body(state, event):
    """Reject changed sources before acknowledging or posting a queued event."""
    if "body" in event:
        return event["body"]
    try:
        body = content(event, render(state, event["kind"], event["source"], event["details"]))
        if digest(body) == event["content_digest"]:
            return body
    except (KeyError, TypeError, ValueError):
        pass
    raise PublicationError("conversation_conflict")


def transition(state):
    """Publish meaningful stage changes once, rather than every checkpoint write."""
    journal = state.setdefault("conversation", {"sequence": 0, "pending": []})
    stage = state["stage"]
    signature = digest([stage, state.get("reason") if stage in ("blocked", "escalated", "gates") else None])
    if journal.get("transition") == signature:
        return
    journal["transition"] = signature
    if stage in ("intake", "implement", "planned", "clarify", "blocked", "stopped", "rejected",
                 "escalated", "gates", "merged", "observed_merged"):
        details = {"intake": "Request enrolled; scope assessment is next.",
                   "implement": "The exact plan has passed design approval. Local implementation is next.",
                   "planned": "Design approved; waiting for a writer's plan-bound approval.",
                   "gates": "Code reviews completed; checking publication and merge gates."}
        body = details.get(stage, "Progress paused or completed; see the current summary.")
        if stage in ("blocked", "stopped", "rejected", "escalated", "gates") and state.get("reason"):
            body += "\n\n" + state["reason"][:4000]
        queue(state, "Controller · " + stage.replace("_", " "), body)


def publish(state, github, config):
    """Reconcile lost POST responses by exact App-authored marker and content."""
    journal = state.get("conversation", {})
    pending = journal.get("pending", [])
    if not pending:
        return False
    try:
        comments = github.pages(f"issues/{state['issue']}/comments")
        for event in pending:
            body = event_body(state, event)
            marker = MARKER + event["id"] + " -->"
            matches = [c for c in comments if c.get("user", {}).get("login") == config["bot_login"]
                       and c["body"].startswith(marker)]
            if len(matches) > 1 or matches and matches[0]["body"] != body:
                raise PublicationError("conversation_conflict")
            comment = matches[0] if matches else github.repo(
                f"issues/{state['issue']}/comments", "POST", {"body": body})
            journal["latest"] = comment["id"]
            if "source" in event:
                source = state
                for key in event["source"]:
                    source = source[key]
                journal.setdefault("receipts", {})["/".join(event["source"])] = {
                    "comment": comment["id"], "content_digest": event["content_digest"],
                    "source_digest": digest(source)}
    except PublicationError:
        raise
    except Exception:
        raise PublicationError("conversation_publish") from None

    journal["pending"] = []
    return True


def compact(state):
    """Retain operative evidence and compact only published narrative text."""
    state.pop("cooperative_votes", None)
    if "objections" in state:
        state["objections"] = [v for v in state["objections"] if not v["resolved"]]
    journal = state.get("conversation", {})
    if journal.get("pending") or not journal.get("receipts"):
        return
    state.pop("review_event", None)
    published = {r.get("source_digest") for r in journal["receipts"].values()}
    reports = list(state.get("qc", {}).values()) + list(state.get("reviews", {}).values())
    reports += [v["report"] for v in state.get("design_votes", {}).values()]
    reports = [r for r in reports if digest(r) in published]
    reports += [s["review"] for key in ("local_status", "local_evidence")
                if (s := state.get(key, {})).get("review") and digest(s) in published]
    for report in reports:
        for key in ("rationale", "summary"):
            text = report.get(key, "")
            size = len(json.dumps(text).encode())
            if size > 1000:
                excerpt = text[:max(1, len(text) * 800 // size)]
                while len(json.dumps(excerpt).encode()) > 900:
                    excerpt = excerpt[:len(excerpt) // 2]
                report[key] = excerpt + "… [Full text in the published issue conversation.]"
                if state.get("reason") == text:
                    state["reason"] = report[key]


def review_body(state, report, phase, member):
    """Render attributed judgments and stable objection identifiers for one plan."""
    plan = state.get("plan_id", "intake")
    round_number = state.get("design_generation", 0) + 1
    prefix = f"D{round_number}-{member['id'][:8]}-{phase}"
    lines = [f"Plan `{plan}` · round {round_number} · {phase}", "", report["rationale"]]
    if report["decision"] == "approve":
        resolved = [v["id"] for v in state.get("objections", [])
                    if v["reviewer"] == member["id"] and not v["resolved"]]
        if resolved:
            lines += ["", "This approval closes earlier objections: " + ", ".join(resolved) + "."]
    for key in ("concerns", "questions"):
        for index, text in enumerate(report[key], 1):
            lines += ["", f"- **{prefix}-{key[0]}{index}**: {text}"]
    return "\n".join(lines)
