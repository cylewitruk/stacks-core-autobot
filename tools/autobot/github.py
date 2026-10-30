"""Small GitHub REST/GraphQL adapter; credentials never reach model processes."""

import json
import subprocess
from urllib.parse import quote


class GitHub:
    """Use gh's authenticated transport, with bounded pagination and explicit methods."""

    def __init__(self, repository):
        """Select the sole repository this client may mutate."""
        self.repository = repository
        self.root = "repos/" + repository

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
