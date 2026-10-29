"""Load an operator-owned, bounded instruction bundle without candidate resources."""

import hashlib
import json
from pathlib import Path
import stat

from policy import Blocked, digest

STAGES = ("intake", "plan", "plan_qc", "develop", "local_review", "review")
SKILLS = dict(zip(STAGES, ("autobot-assess-scope", "autobot-design", "autobot-review-design",
                          "autobot-implement", "autobot-review-local", "autobot-review-pr")))
RESOURCES = {".autobot/charter.json", ".autobot/instructions.md", ".autobot/roles.json"} | {
    f".autobot/schemas/{stage}.json" for stage in STAGES} | {
    f".agents/skills/{name}/SKILL.md" for name in SKILLS.values()}


class Bundle:
    """Freeze bytes and role routing from an explicitly allowlisted trusted root."""

    def __init__(self, root=None):
        """Reject links, traversal, unexpected resources and oversized packages."""
        self.root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
        self.resources = {}
        manifest_text = self.read(".autobot/bundle.json")
        manifest = json.loads(manifest_text)
        if (set(manifest) != {"version", "resources"} or manifest["version"] != 1
                or len(manifest["resources"]) != len(RESOURCES)
                or set(manifest["resources"]) != RESOURCES):
            raise Blocked("instruction bundle has unapproved resources")
        self.resources[".autobot/bundle.json"] = manifest_text
        for path in sorted(RESOURCES):
            self.resources[path] = self.read(path)
        if sum(len(v.encode()) for v in self.resources.values()) > 100000:
            raise Blocked("instruction bundle too large")
        self.hashes = {p: hashlib.sha256(v.encode()).hexdigest() for p, v in self.resources.items()}
        self.identity = digest(self.hashes)
        self.charter = json.loads(self.resources[".autobot/charter.json"])
        if (self.charter.get("version") != 1 or not self.charter.get("id")
                or not self.charter.get("goals") or not self.charter.get("non_goals")
                or not self.charter.get("constraints") or not self.charter.get("acceptance")):
            raise Blocked("incomplete project charter")
        roles = json.loads(self.resources[".autobot/roles.json"])
        if (roles.get("version") != 1 or roles.get("shared") != ".autobot/instructions.md"
                or set(roles.get("roles", {})) != set(STAGES)):
            raise Blocked("invalid role manifest")
        self.roles = roles["roles"]
        for stage, role in self.roles.items():
            expected = {"skill": f".agents/skills/{SKILLS[stage]}/SKILL.md",
                        "schema": f".autobot/schemas/{stage}.json"}
            if role != expected:
                raise Blocked("role references an unapproved resource")
            skill = self.resources[role["skill"]]
            if not skill.startswith("---\nname: " + SKILLS[stage] + "\ndescription: "):
                raise Blocked("skill frontmatter does not match its role")
            schema = json.loads(self.resources[role["schema"]])
            if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
                raise Blocked("role requires a strict object schema")

    def read(self, path):
        """Read only regular bounded files under a real directory chain."""
        if path not in RESOURCES | {".autobot/bundle.json"}:
            raise Blocked("unapproved bundle resource")
        current = self.root
        if current.is_symlink():
            raise Blocked("bundle root cannot be a symlink")
        for part in Path(path).parts:
            current = current / part
            mode = current.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise Blocked("bundle resources cannot contain symlinks")
        metadata = current.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 24000:
            raise Blocked("bundle resource must be a bounded regular file")
        return current.read_text(encoding="utf-8")

    def schema(self, stage):
        """Return the external response contract for one role."""
        return json.loads(self.resources[self.roles[stage]["schema"]])

    def instructions(self, stage):
        """Load shared governance once and only the selected stage's skill."""
        return {"shared": self.resources[".autobot/instructions.md"],
                "charter": self.charter, "role": stage,
                "skill": self.resources[self.roles[stage]["skill"]]}


def main():
    """Validate trusted packaging for CI without model calls."""
    bundle = Bundle()
    print(json.dumps({"bundle": bundle.identity, "resources": bundle.hashes}, sort_keys=True))


if __name__ == "__main__":
    main()
