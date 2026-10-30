"""Container entry point: consume context on stdin and emit only validated JSON."""

import json
import os
from pathlib import Path
import subprocess
import sys

from bundle import Bundle


def command_for(request, schema_path, output_path):
    """Construct provider-specific invocations with explicit model selection."""
    model = ["--model", request["model"]] if request["model"] else []
    provider = request["provider"]
    if provider == "codex":
        return ["codex", "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
                "--skip-git-repo-check", "--sandbox", "read-only", "--output-schema", schema_path,
                "--output-last-message", output_path, "-c", "web_search=\"disabled\"",
                "-c", "features.shell_tool=false", *model, "-"]
    if provider == "claude":
        return ["claude", "-p", "--safe-mode", "--tools", "", "--setting-sources", "",
                "--strict-mcp-config", "--no-session-persistence", "--output-format", "json",
                "--json-schema", json.dumps(request["schema"]), *model]
    if provider == "copilot":
        return ["copilot", "--no-ask-user", "--disable-builtin-mcps",
                "--deny-tool", "shell,write,read,url,memory", "--silent", *model]
    raise ValueError("unsupported provider")


def prompt_for(request, bundle):
    """Keep controller-authored implementation authorization apart from untrusted task evidence."""
    context = dict(request["context"])
    authorization = context.pop("design_authorization", None)
    prompt = {"instructions": bundle.instructions(request["stage"]),
              "response_schema": request["schema"], "untrusted_context": context}
    if authorization is not None:
        if request["stage"] != "develop":
            raise ValueError("implementation authorization cannot enter an independent review")
        prompt["controller_verified_authorization"] = authorization
    return json.dumps(prompt)


def main():
    """Run in an empty directory, ignoring all repository configuration and hooks."""
    request = json.load(sys.stdin)
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
    print(json.dumps(response))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("agent invocation failed (details suppressed)", file=sys.stderr)
        sys.exit(1)
