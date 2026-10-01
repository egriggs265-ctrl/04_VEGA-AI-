#!/usr/bin/env python3
"""vega-ask.py — offline-LLaMA ask layer for the vega toolkit.

POSTs a prompt to Ollama's /api/generate on 127.0.0.1:11434 and prints the
model's response text. Stdlib urllib only — no requests, no network beyond
localhost.

Lineage: Hexwatch V6's Ollama layer (which shelled out to `ollama run`
with the prompt on stdin and a "(no response)" sentinel). Improvement: the
HTTP API gives structured errors, and the failure contract is explicit —
when Ollama is down it prints exactly `ollama unavailable` to stderr and
exits 2, so callers can distinguish "model down" from "model errored".

Model: VEGA_MODEL env, default phi3:mini (hexwatch's default).

Usage:
    vega-ask.py [--model M] [--timeout S] [--host H] [--port P] "prompt"
    echo "summarize this" | vega-ask.py --model llama3.2:3b
    vega-ask.py --self-test

Exit codes: 0 answered, 1 Ollama answered with an error / bad response,
2 usage error or Ollama unreachable.
"""
import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.request

DEFAULT_MODEL = os.environ.get("VEGA_MODEL", "phi3:mini")
DEFAULT_TIMEOUT = 120


def ask(prompt: str, model: str, host: str, port: int, timeout: float) -> str:
    """Return the model's response text, or raise AskError."""
    url = f"http://{host}:{port}/api/generate"
    body = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError,
            socket.timeout) as e:
        raise AskDown(f"ollama unavailable: {e}")
    if not isinstance(data, dict) or "response" not in data:
        raise AskError(f"bad response from ollama: {str(data)[:200]}")
    return data["response"]


class AskError(Exception):
    pass


class AskDown(AskError):
    """Ollama itself is unreachable — distinct from a model error."""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=11434)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("prompt", nargs="?")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    prompt = args.prompt
    if prompt is None:
        if sys.stdin.isatty():
            ap.print_usage(sys.stderr)
            return 2
        prompt = sys.stdin.read().strip()
    if not prompt:
        print("vega-ask: no prompt given", file=sys.stderr)
        return 2

    try:
        print(ask(prompt, args.model, args.host, args.port, args.timeout))
    except AskDown as e:
        print("ollama unavailable", file=sys.stderr)
        return 2
    except AskError as e:
        print(f"vega-ask: {e}", file=sys.stderr)
        return 1
    return 0


def self_test() -> int:
    import http.server
    import subprocess
    import threading

    passed, failed = 0, 0

    def check(label, expected, actual):
        nonlocal passed, failed
        if expected == actual:
            passed += 1
            print(f"  PASS {label}")
        else:
            failed += 1
            print(f"  FAIL {label} (expected [{expected}] got [{actual}])")

    print("[vega-ask self-test]")

    seen = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            seen.update(json.loads(self.rfile.read(length) or b"{}"))
            data = json.dumps({"model": seen.get("model"),
                               "response": "hello from fake",
                               "done": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def run(*argv, env=None, inp=None):
        e = dict(os.environ)
        e.pop("VEGA_MODEL", None)
        if env:
            e.update(env)
        return subprocess.run([sys.executable, __file__, *argv], env=e,
                              input=inp, capture_output=True, text=True,
                              timeout=30)

    # 1-2. fake server answers, model override via env honored
    r = run("--port", str(port), "hi there", env={"VEGA_MODEL": "test-model-xyz"})
    check("fake ollama answers (exit 0)", 0, r.returncode)
    check("response text printed", "hello from fake", r.stdout.strip())
    check("VEGA_MODEL sent to server", "test-model-xyz", seen.get("model"))

    # 3. --model flag beats the default
    r = run("--port", str(port), "--model", "flag-model", "hi")
    check("--model flag honored", "flag-model", seen.get("model"))

    # 4. prompt via stdin
    r = run("--port", str(port), inp="stdin prompt here")
    check("stdin prompt works", "hello from fake", r.stdout.strip())
    check("stdin prompt forwarded", "stdin prompt here", seen.get("prompt"))

    # 5-6. down path: closed port -> clean message, exit 2
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead_port = s.getsockname()[1]
    s.close()
    r = run("--port", str(dead_port), "--timeout", "5", "hi")
    check("ollama down -> exit 2", 2, r.returncode)
    check("ollama down -> clean message", "ollama unavailable", r.stderr.strip())

    # 7. no prompt at all -> usage error
    r = run("--port", str(port), inp="")
    check("empty prompt -> exit 2", 2, r.returncode)

    server.shutdown()
    print(f"[vega-ask self-test] {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
