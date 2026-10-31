"""Small GitHub REST/GraphQL adapter; credentials never reach model processes."""

import json
import subprocess
from urllib.parse import quote

STAGE_LABEL_PREFIX = "autobot:stage:"
ATTENTION_LABEL = "autobot:attention"
STAGES = {"intake", "clarify", "plan", "plan_qc", "planned", "implement", "publish", "review",
          "gates", "merging", "merged", "observed_merged", "blocked", "stopped", "rejected", "escalated"}


class GitHub:
    """Use gh's authenticated transport, with bounded pagination and explicit methods."""

    def __init__(self, repository):
        """Select the sole repository this client may mutate."""
        self.repository = repository
        self.root = "repos/" + repository
        self.known_labels = None

    def api(self, path, method="GET", data=None):
        """Make a request without placing payloads or credentials in shell text."""
        command = ["gh", "api", "--method", method, path]
        if data is not None:
            command += ["--input", "-"]
        result = subprocess.run(command, input=json.dumps(data) if data is not None else None,
                                text=True, capture_output=True, timeout=90)
        if result.returncode:
            raise RuntimeError("GitHub request failed: " + method + " " + path)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def repo(self, path, method="GET", data=None):
        """Address a repository endpoint."""
        return self.api(self.root + "/" + path, method, data)

    def archive(self, sha):
        """Download a pinned source archive without extracting candidate files on the host."""
        result = subprocess.run(["gh", "api", self.root + "/tarball/" + sha],
                                capture_output=True, timeout=120)
        if result.returncode or len(result.stdout) > 100_000_000:
            raise RuntimeError("source archive unavailable or too large")
        return result.stdout

    def sync_stage(self, number, stage):
        """Maintain one namespaced progress label while preserving enrollment and human labels."""
        if stage not in STAGES:
            raise ValueError("unknown stage label")
        wanted = STAGE_LABEL_PREFIX + stage.replace("_", "-")
        current = {label["name"] for label in self.pages(f"issues/{number}/labels")}
        if wanted not in current:
            self.ensure_label(wanted, "8250df", "Autobot progress display; does not grant execution or merge approval.")
            self.repo(f"issues/{number}/labels", "POST", {"labels": [wanted]})
        for old in sorted(current - {wanted}):
            if old.startswith(STAGE_LABEL_PREFIX):
                self.repo(f"issues/{number}/labels/" + quote(old, safe=""), "DELETE")

    def ensure_label(self, name, color, description):
        """Create a display label only when it is absent from the repository."""
        if self.known_labels is None:
            self.known_labels = {label["name"] for label in self.pages("labels")}
        if name not in self.known_labels:
            self.repo("labels", "POST", {"name": name, "color": color, "description": description})
            self.known_labels.add(name)

    def sync_attention(self, number, failed):
        """Set checkpoint health independently of durable stage and enrollment labels."""
        current = {label["name"] for label in self.pages(f"issues/{number}/labels")}
        if failed and ATTENTION_LABEL not in current:
            self.ensure_label(ATTENTION_LABEL, "d4c5f9", "Autobot checkpoint needs attention; display only.")
            self.repo(f"issues/{number}/labels", "POST", {"labels": [ATTENTION_LABEL]})
        elif not failed and ATTENTION_LABEL in current:
            self.repo(f"issues/{number}/labels/" + quote(ATTENTION_LABEL, safe=""), "DELETE")

    def branch_linked(self, number, branch):
        """Check the issue's native Development links with bounded pagination and repo identity."""
        owner, name = self.repository.split("/")
        query = """query($owner:String!,$name:String!,$number:Int!,$cursor:String) {
          repository(owner:$owner,name:$name) { issue(number:$number) {
            linkedBranches(first:100,after:$cursor) {
              nodes { ref { name repository { nameWithOwner } } }
              pageInfo { hasNextPage endCursor }
            }
          } }
        }"""
        cursor = None
        for _ in range(100):
            response = self.api("graphql", "POST", {"query": query, "variables": {
                "owner": owner, "name": name, "number": number, "cursor": cursor}})
            if response.get("errors"):
                raise RuntimeError("cannot establish issue Development links")
            page = response["data"]["repository"]["issue"]["linkedBranches"]
            if any(node.get("ref") and node["ref"]["name"] == branch
                   and node["ref"]["repository"]["nameWithOwner"] == self.repository for node in page["nodes"]):
                return True
            if not page["pageInfo"]["hasNextPage"]:
                return False
            cursor = page["pageInfo"]["endCursor"]
        raise RuntimeError("Development link pagination exceeded safety bound")

    def create_linked_branch(self, issue_id, branch, sha):
        """Create a repository branch linked to its issue without publishing candidate edits."""
        mutation = """mutation($input:CreateLinkedBranchInput!) {
          createLinkedBranch(input:$input) { linkedBranch { ref { name target { oid } } } }
        }"""
        result = self.api("graphql", "POST", {"query": mutation, "variables": {"input": {
            "issueId": issue_id, "name": branch, "oid": sha}}})
        if result.get("errors"):
            raise RuntimeError("linked branch creation failed; reconcile on retry")
        ref = result["data"]["createLinkedBranch"]["linkedBranch"]["ref"]
        if ref["name"] != branch or ref["target"]["oid"] != sha:
            raise RuntimeError("created Development branch differs from reserved name or commit")

    def pages(self, path, key=None):
        """Read all pages or fail if the bounded traversal cannot finish."""
        result = []
        for page in range(1, 101):
            sep = "&" if "?" in path else "?"
            batch = self.repo(f"{path}{sep}per_page=100&page={page}")
            batch = batch[key] if key else batch
            result.extend(batch)
            if len(batch) < 100:
                return result
        raise RuntimeError("pagination exceeded safety bound")

    def outcome_candidates(self, limit):
        """Retrieve at most ten recent opted-in issues and ten recent comments per issue."""
        issues = self.repo(f"issues?state=all&labels=autobot&sort=updated&direction=desc&per_page={limit}")
        result = []
        for issue in issues[:limit]:
            if "pull_request" in issue:
                continue
            # A small fixed window is intentional: discovery must not scan arbitrary history.
            comments = self.repo(f"issues/{issue['number']}/comments?per_page=10&page=1")
            result.append((issue["number"], comments[:10]))
        return result

    def permitted(self, login):
        """Require current repository write access, not author association alone."""
        result = self.repo("collaborators/" + quote(login, safe="") + "/permission")
        return result["permission"] in ("admin", "maintain", "write")

    def threads(self, number):
        """Read every review thread; an outdated unresolved thread still blocks."""
        owner, name = self.repository.split("/")
        query = """query($owner:String!,$name:String!,$number:Int!,$cursor:String) {
          repository(owner:$owner,name:$name) { pullRequest(number:$number) {
            reviewThreads(first:100,after:$cursor) {
              nodes { id isResolved isOutdated }
              pageInfo { hasNextPage endCursor }
            }
          } }
        }"""
        cursor, result = None, []
        for _ in range(100):
            response = self.api("graphql", "POST", {"query": query, "variables": {
                "owner": owner, "name": name, "number": number, "cursor": cursor}})
            if response.get("errors"):
                raise RuntimeError("cannot establish review-thread state")
            page = response["data"]["repository"]["pullRequest"]["reviewThreads"]
            result.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                return result
            cursor = page["pageInfo"]["endCursor"]
        raise RuntimeError("review-thread pagination exceeded safety bound")
