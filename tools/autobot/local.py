"""Credential-free build/test workbench with local Git checkpoints and bounded output."""

import hashlib
import json
import os
import selectors
import subprocess
import time
import uuid

from policy import Blocked, IMAGE

CHECKS = {
    "format": ["cargo", "fmt", "--all", "--", "--check"],
    "check": ["cargo", "check", "--workspace", "--locked", "--offline"],
    "nextest": ["cargo", "nextest", "run", "--workspace", "--lib", "--locked", "--offline"],
}


def available_checks(required):
    """Offer dependency-consuming checks only when the plan prepares Cargo."""
    return ["diff", "format"] + (["check", "nextest"] if {"check", "nextest"} & set(required) else [])


def resource_exhausted(report):
    """Distinguish execution limits from ordinary compiler or assertion failures."""
    return report["timed_out"] or report["output_limited"] or report["exit_code"] in (137, -9)


def bounded_process(command, env, timeout):
    """Capture bounded untrusted output and stop on timeout or excessive output."""
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    tail, count, timed_out, limited = b"", 0, False, False
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                if not selector.select(min(remaining, 1)):
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                count += len(chunk)
                tail = (tail + chunk)[-12000:]
                if count > 2_000_000:
                    limited = True
                    break
        if timed_out or limited:
            process.kill()
        try:
            code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            code = process.wait()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
    return {"exit_code": code, "timed_out": timed_out, "output_limited": limited,
            "output": tail.decode("utf-8", errors="replace"), "output_sha": hashlib.sha256(tail).hexdigest()}


class LocalWorkspace:
    """Own temporary Docker volumes; tests never mount local Git metadata or credentials."""

    def __init__(self, config, archive, branch):
        """Select a pinned execution image and unique volumes for this task attempt."""
        self.config, self.archive, self.branch = config, archive, branch
        self.image = os.environ.get("AUTOBOT_EXECUTION_IMAGE", "")
        if not IMAGE.fullmatch(self.image):
            raise Blocked("AUTOBOT_EXECUTION_IMAGE must be an immutable digest or local image ID")
        self.prefix = "autobot-local-" + uuid.uuid4().hex
        self.volumes = {kind: self.prefix + "-" + kind for kind in ("source", "state", "cargo", "target")}
        self.env = {k: os.environ[k] for k in ("PATH", "DOCKER_HOST", "DOCKER_CONTEXT") if k in os.environ}
        self.current = None
        self.cargo_prepared = False

    def __enter__(self):
        """Initialize a local-only branch from the pinned source archive."""
        try:
            for volume in self.volumes.values():
                subprocess.run(["docker", "volume", "create", volume], env=self.env,
                               check=True, capture_output=True, timeout=30)
            self.current = self.control("init", self.branch, data=self.archive)
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_):
        """Attempt cleanup on normal exit and handled failures."""
        self.close()

    def close(self):
        """Clean only resources allocated by this workbench."""
        for volume in self.volumes.values():
            subprocess.run(["docker", "volume", "rm", "-f", volume], env=self.env,
                           capture_output=True, timeout=30)

    def command(self, name, control=False, network=False):
        """Build a container invocation with explicit mounts and no inherited secrets."""
        args = ["docker", "run", "--rm", "--init", "--name", name, "--read-only",
                "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=512",
                "--memory=8g", "--cpus=4", "--user=" + ("0:0" if control else "10001:10001"),
                "--tmpfs=/tmp:rw,nosuid,nodev,size=512m,mode=1777", "--workdir=/workspace"]
        if not network:
            args += ["--network=none"]
        for kind, target in (("source", "/workspace"), ("cargo", "/cargo"), ("target", "/target")):
            mode = ",readonly" if kind == "source" and not control else ""
            args += ["--mount", f"type=volume,source={self.volumes[kind]},target={target}{mode}"]
        if control:
            args += ["--mount", f"type=volume,source={self.volumes['state']},target=/state", "-i"]
        return args

    def control(self, operation, *arguments, data=None):
        """Run a trusted Git/file operation, without running candidate programs."""
        name = self.prefix + "-control"
        command = self.command(name, control=True) + [self.image, operation, *arguments]
        payload = data if isinstance(data, bytes) else json.dumps(data).encode() if data is not None else None
        try:
            result = subprocess.run(command, input=payload, env=self.env, capture_output=True, timeout=120)
            if result.returncode or len(result.stdout) > 500000:
                raise Blocked("local Git workspace operation failed")
            return json.loads(result.stdout)
        finally:
            subprocess.run(["docker", "rm", "-f", name], env=self.env, capture_output=True, timeout=30)

    def execute(self, name, command, timeout, network=False, control=False):
        """Run one bounded command and bind its actual exit status to the current tree."""
        container = self.prefix + "-run"
        args = self.command(container, control=control, network=network)
        args += ["--entrypoint", command[0], self.image, *command[1:]]
        try:
            report = bounded_process(args, self.env, timeout)
            return {"name": name, "tree": self.current["tree"], **report}
        finally:
            subprocess.run(["docker", "rm", "-f", container], env=self.env, capture_output=True, timeout=30)

    def prepare(self, required, timeout):
        """Fetch locked dependencies before edits, with no provider or GitHub credentials."""
        if {"check", "nextest"} & set(required):
            report = self.execute("fetch", ["cargo", "fetch", "--locked"], timeout, network=True)
            if report["exit_code"] or report["timed_out"] or report["output_limited"]:
                raise Blocked("dependency prefetch failed; repair the execution environment")
            self.cargo_prepared = True

    def files(self, paths):
        """Read approved source files through the isolated helper."""
        return self.control("files", data=paths)

    def checkpoint(self, edits):
        """Commit one agent edit batch on the local branch before testing it."""
        self.current = self.control("checkpoint", data={"expected": self.current["local_commit"], "edits": edits})
        return self.current

    def run_checks(self, required, requested, deadline):
        """Run mandatory and agent-requested checks; shell strings are never accepted."""
        if not isinstance(requested, list) or not all(isinstance(c, str) and c in {"diff", *CHECKS} for c in requested):
            raise Blocked("agent requested an unknown local check")
        if {"check", "nextest"} & set(required + requested) and not self.cargo_prepared:
            raise Blocked("Cargo checks require dependencies prepared before candidate edits; revise the plan's checks")
        reports = []
        for name in dict.fromkeys([*required, *requested]):
            remaining = deadline - time.monotonic()
            if remaining < 1:
                raise Blocked("local execution time budget exhausted")
            command = ["python3", "-I", "/opt/autobot/workspace_entry.py", "diff"] if name == "diff" else CHECKS[name]
            reports.append(self.execute(name, command, min(remaining, self.config["local_check_timeout_seconds"]), control=name == "diff"))
            if resource_exhausted(reports[-1]):
                break
        return reports

    def export(self):
        """Export only the net committed source changes, never Git config or build outputs."""
        return self.control("export")
