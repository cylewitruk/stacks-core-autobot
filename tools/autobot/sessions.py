"""Scope provider transcript storage to one role and one cooperative loop."""

import os
import re
import subprocess

from policy import Blocked, SessionFailure, digest, selected_model


def prepare(state, config, provider, stage, context):
    """Select only cooperative sessions; fresh approval never receives a session."""
    if stage not in ("plan", "plan_qc", "develop") or context.get("review_phase") == "fresh":
        return None
    model = context.get("reviewer_identity", {}).get("model") or selected_model(config, provider, stage)
    scope = digest([config["repository"], state["issue"], provider, model, stage,
                    state["base"], state["requirements"], state["bundle_id"], state["agent_image"],
                    state["config_digest"], state.get("design_reopens", 0),
                    state.get("plan_id") if stage == "develop" else None])
    role = stage + ":" + provider + ":" + model
    sessions = state.setdefault("sessions", {})
    current = sessions.get(role, {})
    if current.get("binding", current.get("scope")) != scope:
        sessions[role] = {"binding": scope, "scope": scope, "turn": 0, "restarts": 0}
    return {**sessions[role], "role": role}


def restart(state, session):
    """Rotate only the failed role while retaining its consumed task budget."""
    current = state["sessions"][session["role"]]
    if current["scope"] != session["scope"]:
        raise Blocked("session identity changed during recovery")
    binding = current.get("binding", current["scope"])
    count = current.get("restarts", 0) + 1
    state["sessions"][session["role"]] = {
        "binding": binding, "scope": digest([binding, count]), "turn": 0, "restarts": count,
        "previous": {"scope": current["scope"], "turn": current["turn"]}}


class SessionVolume:
    """Keep one private named volume across containers, without any host bind mount."""

    def __init__(self, image, session):
        """Use only controller-generated scope digests as Docker names."""
        if not re.fullmatch(r"[a-f0-9]{64}", session.get("scope", "")) or type(session.get("turn")) is not int:
            raise Blocked("invalid retained session identity")
        self.image, self.session = image, session
        self.name = "autobot-session-" + session["scope"]
        self.env = {k: os.environ[k] for k in ("PATH", "DOCKER_HOST", "DOCKER_CONTEXT") if k in os.environ}

    def run(self, args):
        """Use a credential-free Docker client for bounded storage preparation."""
        return subprocess.run(["docker", *args], capture_output=True, env=self.env, timeout=30)

    def mount(self):
        """Expose only this role's transcript volume to its provider container."""
        return f"type=volume,source={self.name},target=/retained"

    def ensure(self):
        """Refuse missing resumed storage instead of silently losing conversation context."""
        found = self.run(["volume", "inspect", self.name])
        if found.returncode:
            if self.session["turn"]:
                raise SessionFailure("retained session storage is missing")
            created = self.run(["volume", "create", "--label", "stacks-autobot=session", self.name])
            if created.returncode:
                raise Blocked("cannot allocate retained session storage")
        result = self.run(["run", "--rm", "--network=none", "--read-only", "--cap-drop=ALL",
                           "--cap-add=CHOWN", "--security-opt=no-new-privileges", "--user=0:0",
                           "--mount", self.mount(), "--entrypoint", "python3", self.image,
                           "-c", "import os; p='/retained'; s=os.stat(p); "
                           "os.chmod(p, 0o700) if s.st_uid == 0 else None; "
                           "os.chown(p, 10001, 10001) if s.st_uid == 0 else None; "
                           "s=os.stat(p); assert (s.st_uid, s.st_gid, s.st_mode & 0o777) == (10001, 10001, 0o700)"])
        if result.returncode:
            raise Blocked("cannot prepare retained session storage")
