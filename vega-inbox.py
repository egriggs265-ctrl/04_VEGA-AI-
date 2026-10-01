#!/usr/bin/env python3
"""vega-inbox.py — inbox/outbox command dispatcher.

Watches inbox/ for command files (<id>.json: {"cmd", "args"}), routes each to
a registered handler, writes exactly one reply to outbox/<id>.json, and keeps
heartbeat + state JSON. File-based IPC: any program (or the watchdog's
re-arm files) can queue work by dropping a JSON file in inbox/.

Lineage: hexwatch_v6_monolith.py's inbox/outbox contract and the Hexstrike
monolith's atomic writes (temp + os.replace), SingleInstance pid guard, and
SHA-256 duplicate suppression.
Fixes vs the originals:
  - hexwatch defined safe_write() but never used it (write_json did plain
    writes -> torn state on crash). Here EVERY state write is atomic.
  - hexwatch's restart/stop handlers double-replied. Here each command file
    is claimed by an atomic rename into processing/ and produces exactly one
    reply; a second run finds nothing to do.
  - hexwatch's inbox offset tracking missed content if chat.inbox was
    truncated. File-per-command has no offsets to lose.
  - No `run <shell>` command: this dispatcher is a safe core. Shell stays
    in the operator's hands (or a separate, explicitly sandboxed handler).

Usage:
    vega-inbox.py --root ./vega-root            # process all pending, exit
    vega-inbox.py --root ./vega-root --loop 5   # daemon: poll every 5s
    vega-inbox.py --self-test

Command file format (inbox/<id>.json):
    {"id": "abc123", "cmd": "ping", "args": {}}
"id" is optional; the filename stem is used when absent.

Builtin handlers: ping, echo, status, help, note. Unknown cmd -> error reply
(never a crash, never silent).

Layout under root:
    inbox/  processing/  done/  outbox/  state/  run/
    state/heartbeat.json  state/state.json  state/commands.jsonl
    run/vega-inbox.pid

Stdlib only. No network.
"""
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

DEDUP_WINDOW_S = 600  # suppress reprocessing of identical commands within 10 min


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Dispatcher:
    def __init__(self, root: Path):
        self.root = root
        self.inbox = root / "inbox"
        self.processing = root / "processing"
        self.done = root / "done"
        self.outbox = root / "outbox"
        self.state_dir = root / "state"
        self.run_dir = root / "run"
        for d in (self.inbox, self.processing, self.done, self.outbox,
                  self.state_dir, self.run_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.seen_path = self.state_dir / "seen.json"
        self.state_path = self.state_dir / "state.json"
        self.hb_path = self.state_dir / "heartbeat.json"
        self.cmdlog_path = self.state_dir / "commands.jsonl"
        self.counters = {"processed": 0, "errors": 0, "deduped": 0,
                         "started_at": time.time()}

    # -- handlers ---------------------------------------------------------
    def handle(self, cmd: str, args: dict):
        if cmd == "ping":
            return True, f"pong {time.time()}"
        if cmd == "echo":
            return True, str(args.get("text", ""))
        if cmd == "status":
            uptime = int(time.time() - self.counters["started_at"])
            return True, json.dumps({
                "app": "vega-inbox", "pid": os.getpid(),
                "uptime_seconds": uptime,
                "processed": self.counters["processed"],
                "errors": self.counters["errors"],
                "deduped": self.counters["deduped"],
            }, indent=2)
        if cmd == "help":
            return True, "commands: ping | echo text=... | status | help | note text=..."
        if cmd == "note":
            return True, f"noted: {args.get('text', '')}"
        return False, f"error: unknown command '{cmd}'"

    # -- dedup ------------------------------------------------------------
    def load_seen(self) -> dict:
        try:
            data = json.loads(self.seen_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_seen(self, seen: dict) -> None:
        atomic_write(self.seen_path, json.dumps(seen) + "\n")

    # -- core -------------------------------------------------------------
    def heartbeat(self) -> None:
        atomic_write(self.hb_path, json.dumps(
            {"ts": time.time(), "pid": os.getpid(), "ok": True}) + "\n")

    def save_state(self) -> None:
        atomic_write(self.state_path, json.dumps(
            {"ts": time.time(), "pid": os.getpid(), **self.counters},
            indent=2) + "\n")

    def log_command(self, cid: str, cmd: str, ok: bool, reply: str) -> None:
        line = json.dumps({"ts": time.time(), "id": cid, "cmd": cmd,
                           "ok": ok, "reply_preview": reply[:200]}) + "\n"
        with open(self.cmdlog_path, "a", encoding="utf-8") as f:
            f.write(line)

    def process_one(self, cmd_file: Path) -> str:
        """Claim, dispatch, reply exactly once. Returns 'ok'|'error'|'dedup'."""
        cid = cmd_file.stem
        # atomic claim: rename into processing/
        claimed = self.processing / cmd_file.name
        try:
            os.rename(cmd_file, claimed)
        except FileNotFoundError:
            return "gone"  # lost a race; someone else took it
        try:
            obj = json.loads(claimed.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            reply_obj = {"id": cid, "ok": False,
                         "reply": f"error: malformed command file: {e}",
                         "ts": time.time()}
            atomic_write(self.outbox / f"{cid}.json",
                         json.dumps(reply_obj, indent=2) + "\n")
            self.log_command(cid, "<malformed>", False, reply_obj["reply"])
            self.counters["errors"] += 1
            os.rename(claimed, self.done / cmd_file.name)
            return "error"
        cmd = str(obj.get("cmd", "")).strip()
        args = obj.get("args", {})
        if not isinstance(args, dict):
            args = {}
        cid = str(obj.get("id", cid))

        digest = sha256(canonical({"cmd": cmd, "args": args}))
        seen = self.load_seen()
        now = time.time()
        seen = {h: t for h, t in seen.items() if now - t < DEDUP_WINDOW_S}
        if digest in seen:
            self.counters["deduped"] += 1
            self.save_seen(seen)
            # a deduped command still gets exactly one reply, so a client
            # waiting on outbox/<id>.json never hangs on a timeout
            atomic_write(self.outbox / f"{cid}.json",
                         json.dumps({"id": cid, "ok": True,
                                     "reply": "deduped: identical command already processed",
                                     "ts": time.time()}, indent=2) + "\n")
            os.rename(claimed, self.done / cmd_file.name)
            return "dedup"
        seen[digest] = now
        self.save_seen(seen)

        try:
            ok, reply = self.handle(cmd, args)
        except Exception as e:  # noqa: BLE001 - a handler must never kill the loop
            ok, reply = False, f"error: handler crashed: {e}"
        reply_obj = {"id": cid, "ok": ok, "reply": reply, "ts": time.time()}
        atomic_write(self.outbox / f"{cid}.json",
                     json.dumps(reply_obj, indent=2) + "\n")
        self.log_command(cid, cmd, ok, reply)
        self.counters["processed"] += 1
        if not ok:
            self.counters["errors"] += 1
        os.rename(claimed, self.done / cmd_file.name)
        return "ok" if ok else "error"

    def cycle(self) -> dict:
        pending = sorted(self.inbox.glob("*.json"))
        outcomes = {"ok": 0, "error": 0, "dedup": 0, "gone": 0}
        for cmd_file in pending:
            outcomes[self.process_one(cmd_file)] += 1
        self.heartbeat()
        self.save_state()
        return outcomes


def acquire_lock(lock_path: Path) -> bool:
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        pass
    try:
        old_pid = int(lock_path.read_text(encoding="utf-8").strip().split()[0])
        os.kill(old_pid, 0)
        return False
    except (ValueError, OSError):
        pass
    try:
        lock_path.unlink()
    except OSError:
        pass
    return acquire_lock(lock_path)


def release_lock(lock_path: Path) -> None:
    try:
        if lock_path.read_text(encoding="utf-8").strip().split()[0] == str(os.getpid()):
            lock_path.unlink()
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--loop", type=int, default=0)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    root = Path(args.root)
    disp = Dispatcher(root)
    lock_path = root / "run" / "vega-inbox.pid"

    if args.loop > 0:
        if not acquire_lock(lock_path):
            print("[vega-inbox] another instance holds the lock; exiting",
                  file=sys.stderr)
            return 1
        try:
            while True:
                try:
                    outcomes = disp.cycle()
                    n = sum(outcomes.values())
                    if n:
                        print(f"[vega-inbox] {outcomes}", flush=True)
                except Exception as e:  # noqa: BLE001 - dispatcher must not die
                    print(f"[vega-inbox] cycle failed: {e}", file=sys.stderr)
                    time.sleep(5)
                time.sleep(args.loop)
        except KeyboardInterrupt:
            pass
        finally:
            release_lock(lock_path)
        return 0

    outcomes = disp.cycle()
    print(f"[vega-inbox] {outcomes}")
    return 0


def self_test() -> int:
    import tempfile
    passed = failed = 0

    def check(label, cond):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS {label}")
        else:
            failed += 1
            print(f"  FAIL {label}")

    print("[vega-inbox self-test]")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        disp = Dispatcher(root)
        (root / "inbox" / "c1.json").write_text(
            json.dumps({"id": "c1", "cmd": "ping", "args": {}}))
        (root / "inbox" / "c2.json").write_text(
            json.dumps({"id": "c2", "cmd": "echo", "args": {"text": "hello vega"}}))
        (root / "inbox" / "c3.json").write_text(
            json.dumps({"id": "c3", "cmd": "bogus", "args": {}}))
        (root / "inbox" / "c4.json").write_text("{not valid json!!!")
        (root / "inbox" / "c5.json").write_text(
            json.dumps({"id": "c5", "cmd": "status", "args": {}}))
        (root / "inbox" / "c6.json").write_text(
            json.dumps({"id": "c6", "cmd": "note", "args": {"text": "re-arm test"}}))

        outcomes = disp.cycle()
        check("cycle processed 6 files",
              sum(outcomes.values()) == 6 and outcomes["gone"] == 0)

        r1 = json.loads((root / "outbox" / "c1.json").read_text())
        check("ping round-trips", r1["ok"] and r1["reply"].startswith("pong "))
        r2 = json.loads((root / "outbox" / "c2.json").read_text())
        check("echo round-trips", r2["ok"] and r2["reply"] == "hello vega")
        r3 = json.loads((root / "outbox" / "c3.json").read_text())
        check("unknown cmd -> error reply not crash",
              r3["ok"] is False and "unknown command" in r3["reply"])
        r4 = json.loads((root / "outbox" / "c4.json").read_text())
        check("malformed file -> error reply not crash",
              r4["ok"] is False and "malformed" in r4["reply"])
        r5 = json.loads((root / "outbox" / "c5.json").read_text())
        st = json.loads(r5["reply"])
        check("status reply is valid json", st["app"] == "vega-inbox")
        r6 = json.loads((root / "outbox" / "c6.json").read_text())
        check("note handler acks (the watchdog's re-arm cmd)",
              r6["ok"] and r6["reply"] == "noted: re-arm test")

        check("inbox drained", list((root / "inbox").glob("*.json")) == [])
        check("done holds 6 files", len(list((root / "done").glob("*.json"))) == 6)
        check("processing empty", list((root / "processing").glob("*")) == [])

        hb = json.loads((root / "state" / "heartbeat.json").read_text())
        check("heartbeat advances", abs(hb["ts"] - time.time()) < 60 and hb["ok"])
        check("no .tmp files left",
              list(root.rglob("*.tmp")) == [])

        # exactly-once: second cycle produces no new replies
        before = sorted(p.name for p in (root / "outbox").glob("*.json"))
        outcomes2 = disp.cycle()
        after = sorted(p.name for p in (root / "outbox").glob("*.json"))
        check("rerun: nothing new, no double replies",
              before == after and sum(outcomes2.values()) == 0)

        # dedup: identical command re-queued within window is suppressed,
        # but still gets exactly one reply (no client hangs on a timeout)
        (root / "inbox" / "c7.json").write_text(
            json.dumps({"id": "c7", "cmd": "ping", "args": {}}))
        disp.cycle()
        r7 = json.loads((root / "outbox" / "c7.json").read_text())
        check("deduped command still gets a reply",
              r7["ok"] and "deduped" in r7["reply"])

        log_lines = (root / "state" / "commands.jsonl").read_text().strip().splitlines()
        check("commands.jsonl has 6 entries (deduped cmd not re-logged)",
              len(log_lines) == 6)
        check("dedup counter incremented",
              json.loads((root / "outbox" / "c1.json").read_text())["ok"]
              and disp.counters["deduped"] == 1)

    print(f"[vega-inbox self-test] {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
