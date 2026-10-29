"""Prepare credentials and advance one supervised local task, with merging disabled."""

import argparse
import base64
import fcntl
import getpass
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

from bundle import Bundle
from controller import validate_config
from policy import Blocked, design_roster

ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = Path.home() / ".config/stacks-autobot-pilot"
PERMISSIONS = {"contents": "write", "issues": "write", "pull_requests": "write",
               "checks": "read", "administration": "read"}
SECRET_NAMES = {"CODEX_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "COPILOT_GITHUB_TOKEN"}
IMAGE = re.compile(r"(?:[a-zA-Z0-9./:_-]+@)?sha256:[a-f0-9]{64}")


def private_file(path):
    """Read a bounded owner-only regular file without following a final symlink."""
    path = Path(path).expanduser()
    if path.resolve().is_relative_to(ROOT.resolve()):
        raise Blocked("private material must stay outside the repository")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Blocked("private files must be owned by you with mode 600")
        data = handle.read(1_000_001)
        if len(data) > 1_000_000:
            raise Blocked("private file exceeds size limit")
        return data


def write_private(path, data):
    """Create a private file exclusively; never replace an existing credential."""
    path = Path(path).expanduser()
    if path.resolve().is_relative_to(ROOT.resolve()):
        raise Blocked("private material must stay outside the repository")
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.parent.stat()
    if path.parent.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Blocked("private directory must be owned by you with mode 700")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


def read_secrets(path, config):
    """Accept only provider credentials; owner GitHub tokens cannot be supplied here."""
    data = json.loads(private_file(path))
    if (not isinstance(data, dict) or not set(data).issubset(SECRET_NAMES)
            or not all(isinstance(v, str) and v.strip() and "\x00" not in v for v in data.values())):
        raise Blocked("credential file has invalid or unsupported entries")
    providers = {identity["provider"] for identity in design_roster(config)}
    for provider, names in {"codex": {"CODEX_API_KEY"}, "claude": {"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"},
                            "copilot": {"COPILOT_GITHUB_TOKEN"}}.items():
        if provider in providers and len(set(data) & names) != 1:
            raise Blocked("configure exactly one dedicated credential for " + provider)
    return data


def capture_secrets(path, claude_api_key=False, copilot=False):
    """Prompt on a real terminal without echoing or accepting secrets as arguments."""
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise Blocked("credential capture requires an interactive terminal")
    if Path(path).expanduser().exists():
        raise Blocked("credential file already exists; use a new private filename for rotation")
    names = ["CODEX_API_KEY", "ANTHROPIC_API_KEY" if claude_api_key else "CLAUDE_CODE_OAUTH_TOKEN"]
    if copilot:
        names.append("COPILOT_GITHUB_TOKEN")
    data = {name: getpass.getpass(name + " (hidden): ").strip() for name in names}
    if not all(data.values()):
        raise Blocked("empty credentials were not saved")
    write_private(path, data)
    print("Dedicated credentials saved with owner-only permissions; no credentials printed.")


def owner_api(path):
    """Use the existing named-account wrapper for read-only administrative preflight."""
    result = subprocess.run(["sh", str(ROOT / "tools/autobot/gh-owner"), "api", path],
                            cwd=ROOT, capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise Blocked("owner preflight request failed: " + path)
    return json.loads(result.stdout)


def api(path, token, method="GET", data=None):
    """Use GitHub HTTPS with in-memory credentials and sanitized errors."""
    request = urllib.request.Request("https://api.github.com/" + path, method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json", "User-Agent": "stacks-autobot-local-pilot"})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            body = response.read(1_000_001)
        if len(body) > 1_000_000:
            raise Blocked("GitHub response exceeds pilot limit")
        return json.loads(body) if body else None
    except urllib.error.HTTPError as error:
        raise Blocked("GitHub App request failed (HTTP " + str(error.code) + ")") from None
    except urllib.error.URLError:
        raise Blocked("GitHub App connection failed") from None


def installation_token(settings, config):
    """Mint a short-lived App token narrowed to the sole pilot repository."""
    key = Path(settings["private_key_path"]).expanduser()
    private_file(key)
    def encode(value):
        """Encode a JWT segment without padding."""
        return base64.urlsafe_b64encode(value).rstrip(b"=")
    now = int(time.time())
    message = encode(b'{"alg":"RS256","typ":"JWT"}') + b"." + encode(json.dumps(
        {"iat": now - 60, "exp": now + 540, "iss": str(settings["app_id"])}).encode())
    signed = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key)],
                            input=message, capture_output=True, timeout=15)
    if signed.returncode:
        raise Blocked("GitHub App key could not sign authentication request")
    jwt = (message + b"." + encode(signed.stdout)).decode()
    app = api("app", jwt)
    if app.get("slug", "") + "[bot]" != config["bot_login"]:
        raise Blocked("App identity differs from configured bot_login")
    installation = api("repos/" + config["repository"] + "/installation", jwt)
    if installation.get("repository_selection") != "selected":
        raise Blocked("install the App on selected repositories, not all repositories")
    if installation.get("account", {}).get("login") != config["repository"].split("/")[0]:
        raise Blocked("App installation owner differs from pilot repository owner")
    granted = api(f"app/installations/{installation['id']}/access_tokens", jwt, "POST",
                  {"repositories": [config["repository"].split("/")[1]], "permissions": PERMISSIONS})
    token = granted["token"]
    try:
        permissions = {k: v for k, v in granted["permissions"].items() if k != "metadata"}
        if permissions != PERMISSIONS:
            raise Blocked("App token permissions differ from the least-privilege pilot scope")
        repositories = api("installation/repositories?per_page=100", token)
        if (repositories.get("total_count") != 1 or
                [r["full_name"] for r in repositories["repositories"]] != [config["repository"]]):
            raise Blocked("App token repository scope differs from the pilot repository")
    except BaseException:
        revoke(token)
        raise
    return token


def revoke(token):
    """Revoke only the temporary token minted for this invocation."""
    try:
        api("installation/token", token, "DELETE")
    except Exception:
        print("Temporary token revocation failed; it retains GitHub's normal expiry.", file=sys.stderr)


def preflight(settings, config):
    """Collect blockers without invoking models or writing GitHub state."""
    validate_config(config)
    problems = []
    if config["merge_enabled"] is not False:
        problems.append("merge_enabled must be false")
    if any(not i["model"].strip() for i in design_roster(config)):
        problems.append("all participating roles require explicit model IDs")
    if not isinstance(settings.get("app_id"), int) or settings["app_id"] <= 0:
        problems.append("GitHub App ID is not configured")
    try:
        private_file(settings.get("private_key_path") or "/nonexistent-autobot-key")
    except (OSError, Blocked):
        problems.append("GitHub App private key is missing or not an owner-only file")
    try:
        read_secrets(settings["credentials_file"], config)
    except (OSError, ValueError):
        problems.append("dedicated provider credentials are missing or invalid")
    for kind in ("agent", "execution"):
        image = settings.get(kind + "_image", "")
        if not IMAGE.fullmatch(image):
            problems.append(kind + " image must be pinned by digest or local image ID")
            continue
        result = subprocess.run(["docker", "image", "inspect", image], capture_output=True, timeout=30)
        if result.returncode:
            problems.append(kind + " image is unavailable locally")
    if IMAGE.fullmatch(settings.get("agent_image", "")):
        result = subprocess.run(["docker", "run", "--rm", "--network=none", "--read-only",
            "--cap-drop=ALL", "--security-opt=no-new-privileges", "--entrypoint", "python3", settings["agent_image"],
            "-I", "-c", "import sys; sys.path.insert(0,'/opt/tools/autobot'); from bundle import Bundle; print(Bundle('/opt').identity)"],
            capture_output=True, text=True, timeout=30)
        if result.returncode or result.stdout.strip() != Bundle(ROOT).identity:
            problems.append("inference image governance bundle differs from this checkout")
    repository = "repos/" + config["repository"]
    try:
        remote = owner_api(repository + "/branches/" + config["base"])
        local = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        if local != remote["commit"]["sha"]:
            problems.append("local HEAD must equal the published main commit")
        dirty = subprocess.run(["git", "diff", "--quiet", "HEAD", "--"], cwd=ROOT)
        untracked = subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard"], cwd=ROOT)
        if dirty.returncode or untracked.strip():
            problems.append("review, sign, and publish all pipeline changes before a live pilot")
        for suffix in ("actions/variables", "environments/autobot/variables"):
            response = owner_api(repository + "/" + suffix + "?per_page=100")
            variables = response["variables"]
            if response.get("total_count", len(variables)) > len(variables):
                raise Blocked("too many variables to establish disabled execution")
            enabled = next((v["value"] for v in variables if v["name"] == "AUTOBOT_ENABLED"), "false")
            if enabled.lower() != "false":
                problems.append("scheduled Autobot execution must remain explicitly disabled")
        info = owner_api(repository)
        if info.get("allow_auto_merge"):
            problems.append("GitHub auto-merge must remain disabled")
        for status in ("queued", "in_progress", "waiting", "pending", "requested"):
            response = owner_api(repository + "/actions/runs?status=" + status + "&per_page=100")
            runs = response["workflow_runs"]
            if response.get("total_count", len(runs)) > len(runs):
                raise Blocked("too many active runs to establish exclusive local execution")
            if any(r.get("path") == ".github/workflows/autobot.yml" for r in runs):
                problems.append("an Actions controller run is already pending or active")
    except (OSError, ValueError, subprocess.SubprocessError):
        problems.append("could not establish remote pilot safety settings")
    return problems


def advance(settings, config, issue):
    """Advance one explicit issue under an exclusive local lock, then revoke its token."""
    if issue <= 0:
        raise Blocked("issue must be a positive number")
    lock_path = DIRECTORY / "pilot.lock"
    DIRECTORY.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Blocked("another local pilot invocation is active") from None
        problems = preflight(settings, config)
        if problems:
            raise Blocked("Pilot blocked: " + "; ".join(problems))
        credentials = read_secrets(settings["credentials_file"], config)
        token = installation_token(settings, config)
        try:
            environment = {k: os.environ[k] for k in ("PATH", "TMPDIR", "DOCKER_HOST", "DOCKER_CONTEXT") if k in os.environ}
            environment.update(credentials, GH_TOKEN=token, AUTOBOT_ISSUE=str(issue),
                               AUTOBOT_AGENT_IMAGE=settings["agent_image"], AUTOBOT_EXECUTION_IMAGE=settings["execution_image"])
            result = subprocess.run([sys.executable, "-B", "tools/autobot/controller.py"], cwd=ROOT,
                                    env=environment, capture_output=True)
            print("One controller invocation finished; inspect the issue state before advancing again.")
            return result.returncode
        finally:
            revoke(token)


def main():
    """Expose read-only preflight, hidden credential capture and explicit one-stage execution."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "capture-secrets", "advance"))
    parser.add_argument("--settings", type=Path, default=DIRECTORY / "pilot.json")
    parser.add_argument("--credentials", type=Path, default=DIRECTORY / "credentials.json")
    parser.add_argument("--claude-api-key", action="store_true")
    parser.add_argument("--copilot", action="store_true")
    parser.add_argument("--issue", type=int)
    args = parser.parse_args()
    try:
        if args.command == "capture-secrets":
            capture_secrets(args.credentials, args.claude_api_key, args.copilot)
            return 0
        settings = json.loads(private_file(args.settings))
        config = json.loads((ROOT / ".autobot/config.json").read_text())
        if args.command == "preflight":
            problems = preflight(settings, config)
            print(json.dumps({"ready": not problems, "blockers": problems}, indent=2))
            return int(bool(problems))
        if args.issue is None:
            raise Blocked("advance requires --issue; queue scanning is disabled for the pilot")
        return advance(settings, config, args.issue)
    except (Blocked, OSError, KeyError, ValueError, subprocess.SubprocessError) as error:
        print(str(error) if isinstance(error, Blocked) else "Pilot setup is incomplete or unavailable; no raw credential errors displayed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
