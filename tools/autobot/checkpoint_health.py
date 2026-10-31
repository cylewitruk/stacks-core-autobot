"""Best-effort checkpoint health display, separate from authoritative task state."""

import sys

from checkpoints import CheckpointError

MARKER = "<!-- stacks-autobot:checkpoint-health:v1 -->"
FAILED = (MARKER + "\n## Autobot checkpoint needs attention\n\n"
          "The controller could not verify or safely save its checkpoint and stopped. "
          "Check artifact access and integrity before retrying. Stage labels retain the last known progress; "
          "this notice does not reset budgets or approvals.\n")
RECOVERED = (MARKER + "\n## Autobot checkpoint recovered\n\n"
             "The controller verified checkpoint access and integrity and completed its invocation.\n")


def sync_checkpoint_health(gh, config, number, failed, error=None):
    """Update an attention label and one App-owned notice without mutating checkpoints."""
    incomplete = False
    try:
        gh.sync_attention(number, failed)
    except Exception:
        incomplete = True
    try:
        comments = gh.pages(f"issues/{number}/comments")
        matches = [c for c in comments if c.get("user", {}).get("login") == config["bot_login"]
                   and c.get("body", "").startswith(MARKER)]
        if len(matches) > 1:
            raise ValueError("duplicate health notices")
        body = FAILED if failed else RECOVERED
        if failed and isinstance(error, CheckpointError):
            body += "\nDiagnostic: " + error.public_detail() + "\n"
        if matches:
            if matches[0]["body"] != body:
                gh.repo(f"issues/comments/{matches[0]['id']}", "PATCH", {"body": body})
        elif failed:
            gh.repo(f"issues/{number}/comments", "POST", {"body": body})
    except Exception:
        incomplete = True
    if incomplete:
        print("Autobot checkpoint health display could not be fully synchronized.", file=sys.stderr)
