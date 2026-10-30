"""Prepare disposable repository volumes without exposing host paths or credentials."""

import json
import os
import re
import subprocess
import uuid

from policy import Blocked, IMAGE


class Snapshot:
    """Own one source volume, writable during preparation and read-only during inference."""

    def __init__(self, image, archive, overlays):
        """Keep trusted preparation inputs separate from the model's request stream."""
        if not IMAGE.fullmatch(image):
            raise Blocked("snapshot requires an immutable agent image")
        self.image, self.archive, self.overlays = image, archive, overlays
        self.volume = "autobot-source-" + uuid.uuid4().hex
        self.env = {k: os.environ[k] for k in ("PATH", "DOCKER_HOST", "DOCKER_CONTEXT") if k in os.environ}

    def mount(self, readonly=True):
        """Return only this allocated named volume, never a host bind mount."""
        return f"type=volume,source={self.volume},target=/repository" + (",readonly" if readonly else "")

    def prepare(self, operation, payload):
        """Run a fixed extraction helper offline without any provider or GitHub secrets."""
        name = self.volume + "-prepare"
        command = ["docker", "run", "--rm", "-i", "--name", name, "--network=none", "--read-only",
                   "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=64",
                   "--memory=1g", "--cpus=2", "--user=0:0", "--mount", self.mount(False),
                   "--entrypoint", "python3", self.image, "-I", "/opt/tools/autobot/repository.py", operation]
        try:
            result = subprocess.run(command, input=payload, capture_output=True, env=self.env, timeout=120)
            if result.returncode:
                raise Blocked("repository snapshot preparation failed")
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, env=self.env, timeout=30)

    def __enter__(self):
        """Create the complete source snapshot before any credential-bearing model starts."""
        try:
            subprocess.run(["docker", "volume", "create", self.volume], check=True,
                           capture_output=True, env=self.env, timeout=30)
            self.prepare("extract", self.archive)
            if self.overlays:
                self.prepare("overlay", json.dumps(self.overlays).encode())
            return self
        except BaseException:
            self.__exit__()
            raise

    def __exit__(self, *_):
        """Remove only this invocation's snapshot volume after its model container exits."""
        subprocess.run(["docker", "volume", "rm", self.volume], capture_output=True, env=self.env, timeout=30)


def inspection(context, stage):
    """Select the pinned source and current local text overlay specified by the controller."""
    commit = context.get("repository_commit", "")
    if not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise Blocked("repository inspection requires a pinned commit")
    overlays = context.get("files", {}) if stage == "develop" else context.get("after", {}) if stage == "local_review" else {}
    if not isinstance(overlays, dict):
        raise Blocked("invalid local inspection overlay")
    return commit, overlays
