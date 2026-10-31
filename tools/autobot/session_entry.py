"""Container-local transcript journal; never exported into public issue state."""

import fcntl
import json
import os
from pathlib import Path
import uuid

from policy import digest


class SessionTurn:
    """Serialize a turn and detect interrupted or repeated provider invocations."""

    def __init__(self, session, prompt, root=Path("/retained")):
        """Lock storage before inspecting the last completed turn."""
        self.root, self.session, self.prompt = root, session, json.loads(prompt)
        self.lock = root.joinpath("turn.lock").open("a")
        self.path = root / "turn.json"
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.load()
        except BaseException:
            self.lock.close()
            raise

    def load(self):
        """Validate the journal while holding its role lock."""
        session, root = self.session, self.root
        self.previous = json.loads(self.path.read_text()) if self.path.exists() else None
        self.request_digest = digest(self.prompt)
        self.cached = None
        self.identifier = str(uuid.UUID(session["scope"][:32]))
        if self.previous:
            prior = self.previous
            if prior["scope"] != session["scope"] or prior["status"] != "complete":
                raise ValueError("ambiguous retained turn; operator reconciliation required")
            if prior["turn"] == session["turn"] and prior["request"] == self.request_digest:
                self.cached = prior["response"]
            elif prior["turn"] != session["turn"] - 1:
                raise ValueError("retained turn does not match checkpoint")
            self.identifier = prior["identifier"]
        elif session["turn"] != 0:
            raise ValueError("retained transcript missing")
        size = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
        if size > 64 * 1024 * 1024:
            raise ValueError("retained transcript exceeds storage budget")

    def write(self, data):
        """Atomically replace the journal so failed writes cannot look complete."""
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)

    def start(self):
        """Mark ambiguous failures before the paid call and append only changed context."""
        if not self.previous:
            self.write({"scope": self.session["scope"], "status": "pending"})
            return json.dumps(self.prompt)
        before = self.previous["prompt"]
        # Stable instructions/schema remain in the official harness's conversation.
        if any(before.get(k) != self.prompt.get(k) for k in ("instructions", "response_schema")):
            raise ValueError("retained instruction contract changed")
        prior, current = before["untrusted_context"], self.prompt["untrusted_context"]
        self.write({"scope": self.session["scope"], "status": "pending"})
        return json.dumps({"context_update": {k: v for k, v in current.items() if prior.get(k) != v},
                           "removed_context_keys": [k for k in prior if k not in current],
                           "repository_snapshot": self.prompt["repository_snapshot"],
                           "controller_verified_authorization": self.prompt.get("controller_verified_authorization")})

    def finish(self, response, identifier):
        """Cache the exact response for recovery after a lost controller response."""
        uuid.UUID(identifier)
        self.write({"scope": self.session["scope"], "status": "complete", "turn": self.session["turn"],
                    "request": self.request_digest, "identifier": identifier,
                    "prompt": self.prompt, "response": response})

    def close(self):
        """Release the role lock after output has been validated and journaled."""
        self.lock.close()
