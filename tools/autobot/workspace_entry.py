"""Trusted local Git operations inside a credential-free container, never on the host."""

import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tarfile

sys.path.insert(0, "/opt/autobot")
from policy import path_allowed

ROOT = Path("/workspace")
STATE = Path("/state")
CARGO = Path("/cargo")
TARGET = Path("/target")


def git(*args):
    """Use isolated Git metadata and disable hooks, global configuration and signing."""
    env = {**os.environ, "GIT_DIR": str(STATE / "git"), "GIT_WORK_TREE": str(ROOT),
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_AUTHOR_NAME": "Autobot local", "GIT_AUTHOR_EMAIL": "autobot@localhost",
           "GIT_COMMITTER_NAME": "Autobot local", "GIT_COMMITTER_EMAIL": "autobot@localhost"}
    if args[0] == "init":
        env.pop("GIT_DIR")
        env.pop("GIT_WORK_TREE")
    return subprocess.check_output(["git", "-c", "core.bare=false", "-c", "core.hooksPath=/dev/null",
                                    "-c", "commit.gpgsign=false", *args], env=env, text=True).strip()


def safe_file(name):
    """Reject symlink ancestors and non-regular files before reading or editing source."""
    if not path_allowed(name):
        raise ValueError("forbidden source path")
    path = ROOT
    for part in Path(name).parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("symlink source path")
    if path.exists() and not path.is_file():
        raise ValueError("non-regular source file")
    return path


def extract_member(archive, member):
    """Extract allowed entries without ownership changes or version-dependent tar filters."""
    root = ROOT.resolve()
    destination = ROOT / member.name
    resolved = destination.resolve()
    if not resolved.is_relative_to(root) or ".git" in resolved.relative_to(root).parts:
        raise ValueError("archive destination escapes source")
    if destination.is_symlink():
        raise ValueError("archive replaces a symlink")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if member.issym():
        target = (destination.parent / member.linkname).resolve()
        if (Path(member.linkname).is_absolute() or not target.is_relative_to(root)
                or ".git" in target.relative_to(root).parts):
            raise ValueError("unsafe archive symlink")
        destination.symlink_to(member.linkname)
    elif member.isdir():
        destination.mkdir(exist_ok=True)
    else:
        with archive.extractfile(member) as source, destination.open("xb") as output:
            shutil.copyfileobj(source, output, length=65536)
        destination.chmod(0o755 if member.mode & 0o111 else 0o644)


def initialize(branch):
    """Safely extract the GitHub archive and create a local-only baseline branch."""
    if not branch.startswith("autobot/") or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789/-" for c in branch):
        raise ValueError("invalid local branch")
    total = 0
    prefix = None
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|gz") as archive:
        for member in archive:
            parts = Path(member.name).parts
            if not parts or Path(member.name).is_absolute() or ".." in parts or ".git" in parts:
                raise ValueError("unsafe archive path")
            prefix = prefix or parts[0]
            if prefix != parts[0] or member.islnk() or not (member.isfile() or member.isdir() or member.issym()):
                raise ValueError("unsupported archive entry")
            if len(parts) == 1:
                continue
            total += member.size
            if total > 500_000_000:
                raise ValueError("archive expansion limit exceeded")
            member.name = "/".join(parts[1:])
            extract_member(archive, member)
    for path in (CARGO, TARGET):
        path.chmod(0o777)
    git("init", "--bare", "--initial-branch=main", str(STATE / "git"))
    git("symbolic-ref", "HEAD", "refs/heads/" + branch)
    git("add", "--force", "-A")
    git("commit", "-m", "Local baseline")
    STATE.joinpath("base").write_text(git("rev-parse", "HEAD"))
    return snapshot()


def snapshot():
    """Identify both the local checkpoint and its complete source tree."""
    return {"local_commit": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}"),
            "branch": git("symbolic-ref", "--short", "HEAD")}


def main():
    """Perform one trusted operation; candidate test execution uses another container."""
    operation = sys.argv[1]
    if operation == "init":
        result = initialize(sys.argv[2])
    elif operation == "checkpoint":
        payload = json.load(sys.stdin)
        if git("rev-parse", "HEAD") != payload["expected"]:
            raise ValueError("local head moved")
        for edit in payload["edits"]:
            path = safe_file(edit["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(edit["content"])
        if payload["edits"]:
            git("add", "--force", "-A")
            git("commit", "-m", "Agent local checkpoint")
        result = snapshot()
    elif operation == "files":
        result = {name: safe_file(name).read_text() if safe_file(name).exists() else None
                  for name in json.load(sys.stdin)}
    elif operation == "export":
        base = STATE.joinpath("base").read_text()
        names = git("diff", "--name-only", base, "HEAD").splitlines()
        result = {**snapshot(), "edits": [{"path": name, "content": safe_file(name).read_text()} for name in names]}
    elif operation == "diff":
        # Git output is diagnostic data. Disable repository-defined external diff tools.
        git("diff", "--no-ext-diff", "--no-textconv", "--check", STATE.joinpath("base").read_text(), "HEAD")
        result = snapshot()
    else:
        raise ValueError("unknown operation")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
