"""Container entry point: consume context on stdin and emit only validated JSON."""

import json
import os
from pathlib import Path
import subprocess
import sys

from bundle import Bundle
from repository import TOOL_NAMES
from session_entry import SessionTurn


def command_for(request, schema_path, output_path):
    """Construct provider-specific invocations with explicit model selection."""
    model = ["--model", request["model"]] if request["model"] else []
    provider = request["provider"]
    session = request.get("session")
    resume = request.get("resume_id")
    server = {"command": "/usr/bin/env", "args": ["-i", "PATH=/usr/bin:/bin", "python3", "-I",
              "/opt/tools/autobot/repository.py", "serve"]}
    mcp = json.dumps({"mcpServers": {"repository": server}})
    if provider == "codex":
        return ["codex", "exec", *(["resume"] if resume else []),
                "--ignore-user-config", "--ignore-rules", *([] if session else ["--ephemeral"]),
                "--json", "--skip-git-repo-check", "-c", 'sandbox_mode="read-only"', "--output-schema", schema_path,
                "--output-last-message", output_path, "-c", "web_search=\"disabled\"",
                "-c", "features.shell_tool=false",
                "-c", 'mcp_servers.repository.command="/usr/bin/env"',
                "-c", "mcp_servers.repository.args=" + json.dumps(server["args"]),
                "-c", "mcp_servers.repository.enabled_tools=" + json.dumps(list(TOOL_NAMES)),
                "-c", "mcp_servers.repository.required=true", *model, *([resume] if resume else []), "-"]
    if provider == "claude":
        return ["claude", "-p", "--tools", "", "--setting-sources", "",
                "--disable-slash-commands", "--settings", '{"disableAllHooks":true}',
                "--mcp-config", mcp, "--allowedTools", ",".join("mcp__repository__" + t for t in TOOL_NAMES),
                "--strict-mcp-config", *(["--resume", resume] if resume else
                    ["--session-id", request["session_id"]] if session else ["--no-session-persistence"]),
                "--output-format", "json",
                "--json-schema", json.dumps(request["schema"]), *model]
    if provider == "copilot":
        server.update(type="local", tools=list(TOOL_NAMES))
        return ["copilot", "--no-ask-user", "--disable-builtin-mcps", "--no-custom-instructions",
                "--additional-mcp-config", json.dumps({"mcpServers": {"repository": server}}),
                "--available-tools", ",".join("repository-" + t for t in TOOL_NAMES),
                "--allow-tool", "repository",
                "--deny-tool", "shell,write,read,url,memory", "--no-remote", "--no-auto-update",
                "--config-dir", os.environ.get("COPILOT_HOME", "/tmp/copilot"),
                *(["--resume=" + request["session_id"]] if session else []), "--silent", *model]
    raise ValueError("unsupported provider")


def prompt_for(request, bundle):
    """Keep controller-authored implementation authorization apart from untrusted task evidence."""
    context = dict(request["context"])
    authorization = context.pop("design_authorization", None)
    prompt = {"instructions": bundle.instructions(request["stage"]),
              "repository_snapshot": {"commit": context["repository_commit"],
                  "local_checkpoint": context.get("local_commit"),
                  "has_local_overlay": request["stage"] in ("develop", "local_review"),
                  "tools": list(TOOL_NAMES)},
              "response_schema": request["schema"], "untrusted_context": context}
    if authorization is not None:
        if request["stage"] != "develop":
            raise ValueError("implementation authorization cannot enter an independent review")
        prompt["controller_verified_authorization"] = authorization
    return json.dumps(prompt)


def main():
    """Run in an empty directory, ignoring all repository configuration and hooks."""
    request = json.load(sys.stdin)
    os.umask(0o077)
    if request.get("session"):
        for name, directory in (("HOME", "home"), ("CODEX_HOME", "codex"),
                                ("CLAUDE_CONFIG_DIR", "claude"), ("COPILOT_HOME", "copilot")):
            os.environ[name] = "/retained/" + directory
    for name in ("HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "COPILOT_HOME"):
        Path(os.environ[name]).mkdir(parents=True, exist_ok=True)
    os.makedirs("/tmp/session", exist_ok=True)
    os.chdir("/tmp/session")
    schema_path, output_path = "/tmp/schema.json", "/tmp/result.json"
    bundle = Bundle(Path(__file__).resolve().parents[2])
    if request["bundle_id"] != bundle.identity or not request["model"].strip():
        raise ValueError("unbound bundle or model")
    request["schema"] = bundle.schema(request["stage"])
    Path(schema_path).write_text(json.dumps(request["schema"]))
    prompt = prompt_for(request, bundle)
    turn = SessionTurn(request["session"], prompt) if request.get("session") else None
    if turn:
        if turn.cached is not None:
            print(json.dumps(turn.cached))
            turn.close()
            return
        request["session_id"] = turn.identifier
        request["resume_id"] = turn.identifier if turn.previous else None
        prompt = turn.start()
    command = command_for(request, schema_path, output_path)
    # Copilot's piped stdin is already programmatic; -p would discard that input.
    result = subprocess.run(command, input=prompt, text=True, capture_output=True, timeout=550)
    if result.returncode:
        raise RuntimeError("provider invocation failed")
    if request["provider"] == "codex":
        response = json.loads(Path(output_path).read_text())
    elif request["provider"] == "claude":
        envelope = json.loads(result.stdout)
        if envelope.get("is_error") or envelope.get("subtype") != "success":
            raise RuntimeError("Claude did not complete successfully")
        response = envelope["structured_output"]
    else:
        response = json.loads(result.stdout)
    if turn:
        identifier = turn.identifier
        if request["provider"] == "codex":
            started = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
            identifiers = [event["thread_id"] for event in started if event.get("type") == "thread.started"]
            if len(identifiers) != 1:
                raise ValueError("missing Codex thread identity")
            identifier = identifiers[0]
        elif request["provider"] == "claude":
            identifier = envelope["session_id"]
        if request.get("resume_id") and identifier != request["resume_id"]:
            raise ValueError("provider resumed a different session")
        turn.finish(response, identifier)
        turn.close()
    print(json.dumps(response))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("agent invocation failed (details suppressed)", file=sys.stderr)
        sys.exit(1)
