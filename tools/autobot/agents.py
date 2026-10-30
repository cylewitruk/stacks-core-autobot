"""Run each model in a fresh container with only its own inference credential."""

import json
import os
import subprocess
import uuid

from policy import Blocked, IMAGE, PROVIDER_CREDENTIALS, selected_model
from bundle import Bundle
from github import GitHub
from snapshot import Snapshot, inspection


class Agents:
    """Launch bounded, independent inference sessions without host filesystem mounts."""

    def __init__(self, config, archive_loader=None):
        """Use trusted configuration and an operator-pinned image digest."""
        self.config = config
        self.bundle = Bundle()
        self.archive_loader = archive_loader or GitHub(config["repository"]).archive
        self.archives = {}

    def ask(self, provider, stage, context):
        """Return one JSON response, killing the container on timeout or interruption."""
        model = (context["reviewer_identity"]["model"] if stage == "plan_qc"
                 else selected_model(self.config, provider, stage))
        if not model.strip():
            raise Blocked("live inference requires an explicit model ID for every participating role")
        image = os.environ.get("AUTOBOT_AGENT_IMAGE", "")
        if not IMAGE.fullmatch(image):
            raise Blocked("AUTOBOT_AGENT_IMAGE must use an immutable digest or local image ID")
        available = [key for key in PROVIDER_CREDENTIALS[provider] if os.environ.get(key)]
        if len(available) != 1:
            raise Blocked(f"configure exactly one inference credential for {provider}")
        name = "autobot-" + uuid.uuid4().hex
        command = ["docker", "run", "--rm", "--init", "-i", "--name", name,
                   "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                   "--pids-limit=128", "--memory=2g", "--cpus=2", "--user=10001:10001",
                   "--tmpfs=/tmp:rw,nosuid,nodev,size=256m,mode=1777",
                   "--env", available[0], image]
        if context.get("bundle_id") != self.bundle.identity:
            raise Blocked("agent transport bundle differs from the approved bundle")
        payload = {"provider": provider, "model": model, "stage": stage,
                   "bundle_id": self.bundle.identity, "context": context}
        # Docker receives only a named inference secret. GH_TOKEN and host paths stay out.
        environment = {k: os.environ[k] for k in ("PATH", "DOCKER_HOST", "DOCKER_CONTEXT")
                       if k in os.environ}
        environment[available[0]] = os.environ[available[0]]
        commit, overlays = inspection(context, stage)
        if commit not in self.archives:
            # Cache only the current immutable archive within this controller invocation.
            self.archives = {commit: self.archive_loader(commit)}
        with Snapshot(image, self.archives[commit], overlays) as snapshot:
            command[-1:-1] = ["--mount", snapshot.mount()]
            try:
                result = subprocess.run(command, input=json.dumps(payload), capture_output=True,
                                        text=True, timeout=600, env=environment)
                if result.returncode or len(result.stdout.encode()) > 300000:
                    raise Blocked(f"{provider} failed; output suppressed to protect credentials")
                return json.loads(result.stdout)
            finally:
                subprocess.run(["docker", "rm", "-f", name], capture_output=True,
                               timeout=30, env=environment)
