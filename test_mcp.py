"""Synthetic MCP transport tests; no Teams cache access."""
import contextlib
import io
import json
import subprocess
import sys
import unittest


class SyntheticService:
    tools = [{"name": "echo", "description": "Echo synthetic data", "inputSchema": {
        "type": "object", "properties": {
            "text": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 3},
            "mode": {"type": "string", "enum": ["ok", "fail"]}},
        "required": ["text"], "additionalProperties": False}}]

    def call(self, name, arguments):
        print("parser diagnostic")
        if arguments.get("mode") == "fail":
            raise ValueError("synthetic failure")
        return {"text": arguments["text"], "name": name}


def request(method, params=None, id=1):
    result = {"jsonrpc": "2.0", "method": method, "id": id}
    if params is not None:
        result["params"] = params
    return result


def initialize(version="2025-11-25"):
    return request("initialize", {"protocolVersion": version, "capabilities": {},
                                  "clientInfo": {"name": "test", "version": "1"}})


READY = {"jsonrpc": "2.0", "method": "notifications/initialized"}


class MCPTests(unittest.TestCase):
    def exchange(self, *messages, ready=False):
        from msteams_local_cli.mcp_server import serve
        inputs = ([initialize(), READY] if ready else []) + list(messages)
        source = "".join((m if isinstance(m, str) else json.dumps(m)) + "\n" for m in inputs)
        output, diagnostics = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(diagnostics):
            self.assertEqual(serve(SyntheticService(), io.StringIO(source), output), 0)
        results = [json.loads(line) for line in output.getvalue().splitlines()]
        expected = sum(not isinstance(m, dict) or "id" in m or not m.get("method") for m in inputs)
        self.assertEqual(len(results), expected)
        return results[1:] if ready else results, diagnostics.getvalue()

    def test_transport_exists(self):
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec("msteams_local_cli.mcp_server"))

    def test_versions_negotiate_and_ping_works(self):
        for version, expected in [("2025-11-25", "2025-11-25"), ("2025-06-18", "2025-06-18"),
                                  ("2099-01-01", "2025-11-25")]:
            results, _ = self.exchange(initialize(version), READY, request("ping", id="p"))
            self.assertEqual(results[0]["result"]["protocolVersion"], expected)
            self.assertEqual(results[0]["result"]["capabilities"], {"tools": {}})
            self.assertEqual(results[1], {"jsonrpc": "2.0", "id": "p", "result": {}})

    def test_initialize_validation_and_lifecycle(self):
        bad = initialize()
        bad["params"]["clientInfo"]["version"] = 4
        results, _ = self.exchange(request("tools/list"), bad, initialize(), request("tools/list"),
                                   READY, request("tools/list"), initialize())
        self.assertEqual([r.get("error", {}).get("code") for r in results],
                         [-32000, -32602, None, -32000, None, -32600])
        self.assertEqual(results[4]["result"]["tools"][0]["name"], "echo")

    def test_unicode_tool_result_and_diagnostics(self):
        results, diagnostics = self.exchange(request("tools/call", {"name": "echo", "arguments": {
            "text": "你好\n世界"}}), ready=True)
        result = results[0]["result"]
        self.assertEqual(result["structuredContent"], {"text": "你好\n世界", "name": "echo"})
        self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])
        self.assertIn("parser diagnostic", diagnostics)

    def test_tool_failure_is_result_and_next_request_survives(self):
        results, _ = self.exchange(request("tools/call", {"name": "echo", "arguments": {
            "text": "x", "mode": "fail"}}), request("ping", id=2), ready=True)
        self.assertTrue(results[0]["result"]["isError"])
        self.assertIn("synthetic failure", results[0]["result"]["content"][0]["text"])
        self.assertEqual(results[1]["result"], {})

    def test_invalid_tool_calls_do_not_execute(self):
        args = [{}, {"text": 3}, {"text": "x", "limit": True}, {"text": "x", "limit": 0},
                {"text": "x", "limit": 4}, {"text": "x", "mode": "bad"},
                {"text": "x", "extra": 1}, [], None]
        calls = [request("tools/call", {"name": "echo", "arguments": a}) for a in args]
        calls += [request("tools/call", {"name": "absent"}), request("tools/call", {}),
                  request("tools/list", {"cursor": "unrecognized"})]
        results, diagnostics = self.exchange(*calls, ready=True)
        self.assertEqual(len(results), len(calls))
        self.assertTrue(all(r["error"]["code"] == -32602 for r in results))
        self.assertEqual(diagnostics, "")

    def test_malformed_requests_survive(self):
        messages = ['{', 'NaN', '[]', 'null', '{}', request("ping", id=True),
                    request("ping", id=None), {"jsonrpc": "1.0", "id": 3, "method": "ping"},
                    request("ping", []), request("unknown"), request("ping", id=99)]
        results, _ = self.exchange(*messages, ready=True)
        self.assertEqual([r.get("error", {}).get("code") for r in results],
                         [-32700, -32700, -32600, -32600, -32600, -32600, -32600,
                          -32600, -32602, -32601, None])
        self.assertEqual(results[-1]["id"], 99)

    def test_notifications_never_respond_or_execute_tools(self):
        results, diagnostics = self.exchange(READY, {"jsonrpc": "2.0", "method": "unknown"},
            {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "echo", "arguments": {"text": "x"}}},
            {"jsonrpc": "2.0", "method": "ping", "params": []}, request("ping"), ready=True)
        self.assertEqual(len(results), 1)
        self.assertEqual(diagnostics, "")

    def test_oversize_line_drained_and_next_request_processed(self):
        from msteams_local_cli.mcp_server import MAX_REQUEST_CHARS
        results, _ = self.exchange("x" * (MAX_REQUEST_CHARS + 20), request("ping"))
        self.assertEqual(results[0]["error"]["code"], -32700)
        self.assertEqual(results[1]["result"], {})

    def test_subprocess_utf8_and_eof(self):
        source = '\n'.join(json.dumps(m, ensure_ascii=False) for m in [initialize(), READY,
            request("tools/call", {"name": "echo", "arguments": {"text": "你好"}})]) + '\n'
        proc = subprocess.run([sys.executable, "-c", "from test_mcp import SyntheticService; "
            "from msteams_local_cli.mcp_server import serve; raise SystemExit(serve(SyntheticService()))"],
            input=source.encode("utf-8"), capture_output=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", errors="replace"))
        lines = proc.stdout.decode("utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[1])["result"]["structuredContent"]["text"], "你好")
        self.assertIn(b"parser diagnostic", proc.stderr)

    def test_invalid_utf8_does_not_kill_subprocess(self):
        source = b'\xff\n' + json.dumps(request("ping", id=8)).encode() + b'\n'
        proc = subprocess.run([sys.executable, "-c", "from test_mcp import SyntheticService; "
            "from msteams_local_cli.mcp_server import serve; raise SystemExit(serve(SyntheticService()))"],
            input=source, capture_output=True, timeout=10)
        self.assertEqual(proc.returncode, 0)
        results = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual(results[0]["error"]["code"], -32700)
        self.assertEqual(results[1]["id"], 8)

    def test_unpaired_surrogate_cannot_break_output_encoding(self):
        source = '\n'.join(json.dumps(m) for m in [initialize(), READY,
            request("tools/call", {"name": "echo", "arguments": {"text": "\ud800"}}),
            request("ping", id=5)]) + '\n'
        proc = subprocess.run([sys.executable, "-c", "from test_mcp import SyntheticService; "
            "from msteams_local_cli.mcp_server import serve; raise SystemExit(serve(SyntheticService()))"],
            input=source.encode(), capture_output=True, timeout=10)
        self.assertEqual(proc.returncode, 0)
        results = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual(results[-1]["id"], 5)


if __name__ == "__main__":
    unittest.main()
