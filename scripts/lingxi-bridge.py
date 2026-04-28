#!/usr/bin/env python3
"""Tiny LAN-local bridge so 玄 on Oma can dispatch one-shot turns to
灵犀 (the openclaw `main` agent) on Agent99.

Listens on Agent99's LAN address (192.168.122.x) and accepts:

  POST /agent   {"message": "...", "agent": "main", "token": "<shared>"}

For each request, spawns `openclaw agent --agent <id> --message <text>
--json` and returns the parsed result. Shared-secret token auth keeps
casual scanners from triggering 灵犀 turns; not a substitute for proper
network controls.
"""
import http.server
import json
import os
import socket
import subprocess
import sys
import threading

BIND_HOST = os.environ.get("LINGXI_BRIDGE_BIND", "0.0.0.0")
BIND_PORT = int(os.environ.get("LINGXI_BRIDGE_PORT", "18890"))
SHARED_TOKEN = os.environ.get("LINGXI_BRIDGE_TOKEN", "").strip()
DEFAULT_AGENT = os.environ.get("LINGXI_BRIDGE_DEFAULT_AGENT", "main")
TIMEOUT_S = int(os.environ.get("LINGXI_BRIDGE_TIMEOUT_S", "300"))


def _json_resp(handler, code, payload):
    body = json.dumps(payload).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a, **k):
        return

    def do_GET(self):
        if self.path == "/health":
            return _json_resp(self, 200, {
                "ok": True,
                "host": socket.gethostname(),
                "default_agent": DEFAULT_AGENT,
                "auth_required": bool(SHARED_TOKEN),
            })
        return _json_resp(self, 404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path != "/agent":
            return _json_resp(self, 404, {"ok": False, "error": "not found"})
        n = int(self.headers.get("Content-Length", "0") or 0)
        try:
            body = json.loads(self.rfile.read(n)) if n else {}
        except Exception:
            return _json_resp(self, 400, {"ok": False, "error": "bad json"})
        token = (body.get("token") or self.headers.get("X-Bridge-Token") or "").strip()
        if SHARED_TOKEN and token != SHARED_TOKEN:
            return _json_resp(self, 401, {"ok": False, "error": "auth required"})
        message = (body.get("message") or "").strip()
        if not message:
            return _json_resp(self, 400, {"ok": False, "error": "message is required"})
        agent = (body.get("agent") or DEFAULT_AGENT).strip() or DEFAULT_AGENT
        thinking = (body.get("thinking") or "").strip() or None

        argv = ["openclaw", "agent", "--agent", agent, "--message", message, "--json"]
        if thinking:
            argv += ["--thinking", thinking]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return _json_resp(self, 504, {"ok": False, "error": f"openclaw agent timed out after {TIMEOUT_S}s"})
        except FileNotFoundError:
            return _json_resp(self, 500, {"ok": False, "error": "openclaw CLI not on PATH"})
        except Exception as e:
            return _json_resp(self, 500, {"ok": False, "error": f"spawn failed: {e}"})

        out = proc.stdout.strip()
        err = proc.stderr.strip()
        # openclaw agent --json on this CLI version sends structured
        # output AND informational lines ("Gateway target: ...",
        # "Source:", "Config:", "Bind:") interleaved on stderr when the
        # gateway path falls back to the embedded harness. stdout may be
        # empty. So scan both streams for the first balanced JSON object,
        # stripping any leading non-JSON noise.
        def _extract_first_json(blob):
            if not blob:
                return None
            idx = blob.find("{")
            if idx < 0:
                return None
            depth = 0
            in_str = False
            esc = False
            for i, ch in enumerate(blob[idx:], start=idx):
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                else:
                    if ch == '"':
                        in_str = True
                    elif ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            try:
                                return json.loads(blob[idx:i + 1])
                            except Exception:
                                return None
            return None

        result = _extract_first_json(out) or _extract_first_json(err)
        return _json_resp(self, 200 if proc.returncode == 0 else 502, {
            "ok": proc.returncode == 0,
            "agent": agent,
            "exit_code": proc.returncode,
            "result": result,
            "stdout": out if result is None else None,
            "stderr": err if err else None,
        })


def main():
    print(f"[lingxi-bridge] listening on {BIND_HOST}:{BIND_PORT} (auth={'token' if SHARED_TOKEN else 'OPEN'})")
    print(f"[lingxi-bridge] default agent: {DEFAULT_AGENT}")
    sys.stdout.flush()
    server = http.server.ThreadingHTTPServer((BIND_HOST, BIND_PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
