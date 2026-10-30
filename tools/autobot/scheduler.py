"""Bound scheduled queue scans with a GitHub-persisted rotation cursor."""

import json

from policy import Blocked

MARKER = "<!-- stacks-autobot:queue:v1 -->\n"


class QueueScan:
    """Resume after the last inspected issue, including when it could not advance."""

    def __init__(self, github, config):
        """Read one bot-owned cursor on the oldest currently enrolled open issue."""
        self.gh = github
        self.comment_id = None
        self.limit = config["max_open_issues"]
        opened = github.pages("issues?state=open&labels=autobot&sort=updated&direction=asc")
        closed = github.repo("issues?state=closed&labels=autobot&sort=updated&direction=desc"
                             f"&per_page={config['max_outcome_issues']}")
        open_numbers = {issue["number"] for issue in opened if "pull_request" not in issue}
        numbers = sorted(open_numbers | {issue["number"] for issue in closed[:config["max_outcome_issues"]]
                                        if "pull_request" not in issue})
        self.numbers = numbers
        if not numbers:
            return
        self.anchor = min(open_numbers) if open_numbers else numbers[0]
        comments = github.pages(f"issues/{self.anchor}/comments")
        cursors = [comment for comment in comments
                   if comment["user"]["login"] == config["bot_login"]
                   and comment["body"].startswith(MARKER)]
        if len(cursors) > 1:
            raise Blocked("multiple queue cursors; manual reconciliation required")
        if cursors:
            try:
                cursor = json.loads(cursors[0]["body"][len(MARKER):])
                if (not isinstance(cursor, dict) or set(cursor) != {"after"}
                        or type(cursor["after"]) is not int or cursor["after"] <= 0):
                    raise ValueError("invalid cursor")
            except (ValueError, TypeError):
                raise Blocked("invalid queue cursor; manual reconciliation required") from None
            self.comment_id = cursors[0]["id"]
            self.numbers = ([number for number in numbers if number > cursor["after"]]
                            + [number for number in numbers if number <= cursor["after"]])

    def __iter__(self):
        """Persist each visit before dispatch so a failed issue cannot pin the queue."""
        for number in self.numbers[:self.limit]:
            body = MARKER + json.dumps({"after": number})
            if self.comment_id is None:
                comment = self.gh.repo(f"issues/{self.anchor}/comments", "POST", {"body": body})
                self.comment_id = comment["id"]
            else:
                self.gh.repo(f"issues/comments/{self.comment_id}", "PATCH", {"body": body})
            yield number
