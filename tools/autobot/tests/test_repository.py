"""Repository inspection contracts, hostile source inputs and isolated Docker transport."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_entry import command_for
from policy import Blocked, path_allowed
from repository import Repository, TOOL_NAMES, extract, overlay, serve
from snapshot import Snapshot, inspection
from test_pipeline import CONFIG, Controller, FakeAgents, FakeGitHub


def archive(entries):
    """Build a small pinned-archive fixture, optionally containing hostile link entries."""
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as tar:
        for name, content in entries.items():
            member = tarfile.TarInfo(name)
            if isinstance(content, tuple):
                member.type, member.linkname = content
                tar.addfile(member)
            else:
                member.size, member.mode = len(content), 0o755
                tar.addfile(member, io.BytesIO(content))
    return data.getvalue()


class RepositoryTests(unittest.TestCase):
    """Read protection is distinct from edit permission and cannot reach host secrets."""

    def test_protected_reference_is_listed_readable_and_searchable(self):
        """The real pilot's missing operations guide is discoverable without edit authority."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            extract(io.BytesIO(archive({"repo/docs/autobot.md": b"Writer enrollment\nMerge disabled\n",
                                       "repo/.autobot/config.json": b"{}"})), root)
            repo = Repository(root)
            self.assertFalse(path_allowed("docs/autobot.md"))
            self.assertIn("docs/autobot.md", json.loads(repo.call("list_files", {}))["paths"])
            self.assertEqual(json.loads(repo.call("read_file", {"path": "docs/autobot.md"}))["lines"][0]["text"],
                             "Writer enrollment")
            self.assertEqual(json.loads(repo.call("search", {"query": "enrollment"}))["matches"][0]["path"],
                             "docs/autobot.md")
            self.assertEqual(repo.read(".autobot/config.json"), "{}")
            self.assertEqual((root / "docs/autobot.md").stat().st_mode & 0o777, 0o444)

    def test_archive_rejects_traversal_metadata_special_files_and_multiple_roots(self):
        """Untrusted archive names cannot escape or supply Git state or devices."""
        for entries in ({"repo/../escape": b"x"}, {"/absolute": b"x"}, {"repo/.git/config": b"x"},
                        {"repo/a": b"x", "other/b": b"x"}, {"repo/fifo": (tarfile.FIFOTYPE, "")}):
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):
                    extract(io.BytesIO(archive(entries)), Path(directory))

    def test_archive_links_are_omitted_and_server_rejects_symlink_components(self):
        """Neither archived nor subsequently introduced symlinks expose outside files."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            extract(io.BytesIO(archive({"repo/link": (tarfile.SYMTYPE, "/etc/passwd"),
                                       "repo/hard": (tarfile.LNKTYPE, "repo/file"),
                                       "repo/file": b"safe"})), root)
            self.assertFalse((root / "link").exists())
            self.assertFalse((root / "hard").exists())
            (root / "link").symlink_to("/etc")
            repo = Repository(root)
            for name in ("../secret", "/etc/passwd", "link/passwd", ".git/config", "file/../file", "./file"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    repo.read(name)

    def test_local_overlay_is_current_and_cannot_change_protected_files(self):
        """Inspection sees tested local text, while overlay authority stays within candidate paths."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            extract(io.BytesIO(archive({"repo/docs/guide.md": b"old"})), root)
            overlay({"docs/guide.md": "new", "docs/new.md": "added"}, root)
            self.assertEqual(Repository(root).read("docs/guide.md"), "new")
            for files in ({"docs/autobot.md": "bad"}, {"../secret": "bad"}, {"docs/guide.md": None}):
                with self.assertRaises(ValueError):
                    overlay(files, root)

    def test_read_pagination_binary_rejection_and_budget(self):
        """Large output is paginated and repeated retrieval cannot run unbounded."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "lines").write_text("\n".join(str(i) for i in range(250)))
            (root / "binary").write_bytes(b"secret\x00data")
            repo = Repository(root)
            first = json.loads(repo.call("read_file", {"path": "lines"}))
            self.assertEqual(first["next_offset"], 100)
            self.assertEqual(repo.read_file("lines", offset=100)["lines"][0]["text"], "100")
            with self.assertRaises(ValueError):
                repo.read("binary")
            with self.assertRaises(ValueError):
                repo.read_file("lines", limit=True)
            repo.calls = 80
            with self.assertRaises(ValueError):
                repo.call("list_files", {})

    def test_mcp_protocol_has_only_read_tools_and_sanitized_errors(self):
        """A real stdio handshake and calls expose the fixed read surface only."""
        with tempfile.TemporaryDirectory() as directory:
            requests = [
                {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
                {"method": "notifications/initialized"}, {"id": 2, "method": "tools/list"},
                {"id": 3, "method": "tools/call", "params": {"name": "read_file", "arguments": {"path": "/secret"}}},
                {"id": 4, "method": "tools/call", "params": {"name": "shell", "arguments": {"command": "touch /tmp/pwn"}}},
                {"id": 5, "method": "tools/call", "params": {"name": "list_files", "arguments": {}}},
            ]
            output = io.StringIO()
            serve(Repository(Path(directory)), io.StringIO("\n".join(map(json.dumps, requests))), output)
            results = list(map(json.loads, output.getvalue().splitlines()))
            self.assertEqual(results[0]["result"]["protocolVersion"], "2025-03-26")
            self.assertEqual([t["name"] for t in results[1]["result"]["tools"]], list(TOOL_NAMES))
            self.assertTrue(results[2]["result"]["isError"])
            self.assertTrue(results[3]["result"]["isError"])
            self.assertNotIn("/secret", output.getvalue())
            self.assertEqual(json.loads(results[4]["result"]["content"][0]["text"])["paths"], [])

    def test_mcp_recovers_after_malformed_messages(self):
        """Bad frames receive protocol errors without losing the next valid request."""
        frames = ["not-json", "[]", '{"id":1,"method":"initialize","params":null}',
                  '{"id":2,"method":"tools/call","params":[]}',
                  '{"id":3,"method":"tools/list"}']
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            serve(Repository(Path(directory)), io.StringIO("\n".join(frames)), output)
            replies = list(map(json.loads, output.getvalue().splitlines()))
            self.assertEqual([r["error"]["code"] for r in replies[:-1]], [-32700, -32600, -32602, -32602])
            self.assertEqual([t["name"] for t in replies[-1]["result"]["tools"]], list(TOOL_NAMES))

    def test_mcp_notifications_and_pings_do_not_spend_retrieval_budget(self):
        """More than 200 protocol messages must not terminate useful repository inspection."""
        frames = [{"method": "notifications/initialized"}] * 250
        frames += [{"id": n, "method": "ping"} for n in range(250)]
        frames += [{"id": 999, "method": "tools/call", "params": {"name": "list_files"}}]
        with tempfile.TemporaryDirectory() as directory:
            repo, output = Repository(Path(directory)), io.StringIO()
            serve(repo, io.StringIO("\n".join(map(json.dumps, frames))), output)
            replies = list(map(json.loads, output.getvalue().splitlines()))
            self.assertEqual(len(replies), 251)
            self.assertEqual(replies[-1]["id"], 999)
            self.assertNotIn("error", replies[-1])
            self.assertEqual(repo.calls, 1)
            repo.calls = 80
            with self.assertRaises(ValueError):
                repo.call("list_files", {})

    def test_read_and_search_use_source_line_numbers(self):
        """Unicode separators and form feeds stay on their LF-delimited source line."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source").write_bytes("first\fsecond\u2028third\r\nfourth\nfifth".encode())
            repo = Repository(root)
            lines = repo.read_file("source")["lines"]
            self.assertEqual([line["line"] for line in lines], [1, 2, 3])
            self.assertEqual(lines[0]["text"], "first\fsecond\u2028third")
            self.assertEqual(repo.search("third")["matches"][0]["line"], 1)
            self.assertEqual(repo.search("fourth")["matches"][0]["line"], 2)
            self.assertEqual(repo.read_file("source", offset=2)["lines"][0]["text"], "fifth")

    def test_controller_binds_base_then_head_and_transport_selects_local_overlay(self):
        """Models cannot substitute the inspection revision and local reviews see their candidate."""
        agents = FakeAgents()
        controller = Controller(FakeGitHub(CONFIG), agents, CONFIG)
        controller.state = {"base": "a" * 40, "calls": 0}
        with patch.object(controller, "save"):
            controller.ask("claude", "plan", {"repository_commit": "f" * 40})
            self.assertEqual(agents.calls[-1][2]["repository_commit"], "a" * 40)
            controller.state["head"] = "b" * 40
            controller.ask("claude", "review", {})
            self.assertEqual(agents.calls[-1][2]["repository_commit"], "b" * 40)
        context = {"repository_commit": "b" * 40, "files": {"docs/guide.md": "develop"},
                   "after": {"docs/guide.md": "review"}}
        self.assertEqual(inspection(context, "develop")[1]["docs/guide.md"], "develop")
        self.assertEqual(inspection(context, "local_review")[1]["docs/guide.md"], "review")
        self.assertEqual(inspection(context, "review")[1], {})
        with self.assertRaises(Blocked):
            inspection({"repository_commit": "main"}, "plan")

    def test_snapshot_preparation_is_offline_and_cleanup_survives_failure(self):
        """Only disposable source volumes reach preparation; provider secrets never do."""
        with patch.dict(os.environ, {"GH_TOKEN": "owner", "CODEX_API_KEY": "secret"}), \
                patch("snapshot.subprocess.run") as run:
            run.return_value.returncode = 1
            snapshot = Snapshot("sha256:" + "a" * 64, b"archive", {})
            with self.assertRaises(Blocked):
                with snapshot:
                    self.fail("preparation should fail")
            preparation = run.call_args_list[1]
            self.assertIn("--network=none", preparation.args[0])
            self.assertNotIn("GH_TOKEN", preparation.kwargs["env"])
            self.assertNotIn("CODEX_API_KEY", preparation.kwargs["env"])
            self.assertEqual(run.call_args.args[0], ["docker", "volume", "rm", snapshot.volume])

    def test_provider_tools_are_explicit_and_mcp_environment_is_empty(self):
        """All adapters expose the same server, with no implicit repository configuration."""
        for provider in ("codex", "claude", "copilot"):
            cmd = command_for({"provider": provider, "model": "test", "schema": {}}, "schema", "out")
            joined = " ".join(cmd)
            self.assertIn("/usr/bin/env", joined)
            self.assertIn('"-i"', joined)
            self.assertIn("repository.py", joined)
            self.assertNotIn("--allow-all-tools", cmd)
            if provider == "claude":
                self.assertNotIn("--safe-mode", cmd)
                self.assertEqual(cmd[cmd.index("--tools") + 1], "")
                self.assertIn("--strict-mcp-config", cmd)
            elif provider == "codex":
                self.assertIn("features.shell_tool=false", cmd)
                self.assertIn("mcp_servers.repository.required=true", cmd)
            else:
                self.assertIn("--no-custom-instructions", cmd)
                self.assertIn("--disable-builtin-mcps", cmd)
                config = json.loads(cmd[cmd.index("--additional-mcp-config") + 1])
                self.assertEqual(config["mcpServers"]["repository"]["type"], "local")


@unittest.skipUnless(os.environ.get("AUTOBOT_TEST_AGENT_IMAGE"), "requires pinned agent Docker image")
class RepositoryDockerTests(unittest.TestCase):
    """Verify actual volume permissions and stdio reads without model credentials or quota."""

    def test_real_readonly_mount_and_mcp_reference_read(self):
        """The protected guide is readable, writes fail, and arbitrary file access is refused."""
        image = os.environ["AUTOBOT_TEST_AGENT_IMAGE"]
        data = archive({"repo/docs/autobot.md": b"enrollment-evidence", "repo/docs/guide.md": b"old"})
        with Snapshot(image, data, {"docs/guide.md": "current"}) as snapshot:
            command = ["docker", "run", "--rm", "-i", "--network=none", "--read-only", "--cap-drop=ALL",
                       "--security-opt=no-new-privileges", "--user=10001:10001", "--mount", snapshot.mount(),
                       "--entrypoint", "python3", image, "-I", "-c"]
            probe = "from pathlib import Path; Path('/repository/docs/guide.md').write_text('bad')"
            failed = subprocess.run(command + [probe], capture_output=True, timeout=30)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn(b"Read-only file system", failed.stderr)
            requests = [{"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
                        {"id": 2, "method": "tools/call", "params": {"name": "read_file", "arguments": {"path": "docs/autobot.md"}}},
                        {"id": 3, "method": "tools/call", "params": {"name": "read_file", "arguments": {"path": "docs/guide.md"}}},
                        {"id": 4, "method": "tools/call", "params": {"name": "read_file", "arguments": {"path": "/proc/self/environ"}}}]
            result = subprocess.run(command[:-1] + ["/opt/tools/autobot/repository.py", "serve"],
                input="\n".join(map(json.dumps, requests)), text=True, capture_output=True, timeout=30, check=True)
            replies = list(map(json.loads, result.stdout.splitlines()))
            self.assertIn("enrollment-evidence", replies[1]["result"]["content"][0]["text"])
            self.assertIn("current", replies[2]["result"]["content"][0]["text"])
            self.assertTrue(replies[3]["result"]["isError"])
