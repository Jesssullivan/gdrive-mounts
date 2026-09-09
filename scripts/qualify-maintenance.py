#!/usr/bin/env python3
"""Attended Darwin launchd experiment; never targets a real mount or watchdog.

This is bounded observation on one OS, not an upload-drain or unmount proof.
An enabled failure/restart is the positive control. The disabled experiment
must retain the live PID and heartbeat, observe no handled control signal,
and suppress restart after exit 42 for 45 seconds (ThrottleInterval is 30).
SIGKILL cannot be logged; disappearance/stalled heartbeats fail the live test.

Only the random fixture label is enabled/disabled/booted out. Cleanup may
signal that disposable process through launchd; no PID is ever signalled by
the controller. A failed cleanup retains its exact directory and reports it.
launchctl enable clears the disabled override; Apple may retain an enabled
override record. This tool never edits launchd's persistent database.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid


PREFIX = "dev.tinyland.gdrive-maintenance-fixture."
THROTTLE = 30
NO_RESPAWN_SECONDS = 45
LIVE_SECONDS = 5
HEARTBEAT_MAX_AGE = 3
COMMAND_TIMEOUT = 5
LEASE_SECONDS = 240
FIXTURE_FILES = ("job.plist", "worker.py", "lease.json", "events.jsonl", "heartbeat.json", "heartbeat-next", "command", "startup.err")
STATE_VALUES = {"running", "waiting", "exited", "not running", "spawn scheduled", "spawn pending"}
REASON_VALUES = {"none", "crash", "signal", "normal", "exit", "watchdog", "resource"}
MAX_EVENTS_BYTES = 64 * 1024


class QualificationError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise QualificationError(message)


def private_parent(value):
    """Require an existing operator-selected directory, not a fallback path."""
    path = Path(value)
    require(path.is_absolute(), "scratch parent must be absolute")
    resolved = path.resolve(strict=True)
    require(path == resolved, "scratch parent must be canonical, without symlinks or '..'")
    require(resolved != Path("/"), "scratch parent cannot be the filesystem root")
    require(not any(resolved == root or root in resolved.parents
                    for root in (Path("/tmp"), Path("/private/tmp"))),
            "scratch parent must be outside /tmp and /private/tmp")
    info = resolved.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid(),
            "scratch parent must be an existing directory owned by this user")
    require(stat.S_IMODE(info.st_mode) == 0o700, "scratch parent must have mode 0700")
    return resolved


def fixture_plist(label, directory, interpreter, source):
    return {
        "Label": label,
        "ProgramArguments": [interpreter, str(source), "--fixture-child", str(directory)],
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
        "RunAtLoad": True,
        "ThrottleInterval": THROTTLE,
        "ExitTimeOut": 5,
        "EnvironmentVariables": {"PYTHONDONTWRITEBYTECODE": "1"},
        "StandardOutPath": "/dev/null",
        # Interpreter/loader failures occur before worker_event can run. Keep
        # this owned private stream until diagnostics classify it; never print
        # raw stderr or inherited environment values in the public receipt.
        "StandardErrorPath": str(directory / "startup.err"),
    }


def worker_directory(directory):
    directory = private_parent(directory)
    require(directory.name.startswith("gdrive-maintenance-"), "unexpected fixture directory name")
    return directory


def worker_event(directory, kind, **fields):
    record = {"event": kind, "pid": os.getpid(), "time": time.monotonic(), **fields}
    payload = (json.dumps(record, sort_keys=True) + "\n").encode()
    require(len(payload) <= 1024, "fixture event exceeds its bound")
    fd = os.open(directory / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "ab") as stream:
        stream.write(payload)


def child(directory):
    """Darwin-only public worker entry; no launchctl calls occur in the child."""
    require(platform.system() == "Darwin", "fixture worker is Darwin-only")
    return _worker(directory)


def _worker(directory):
    """Worker body, also exercised by an ordinary subprocess in offline tests."""
    directory = worker_directory(directory)
    os.umask(0o077)  # launchd does not inherit the controller's process umask.
    worker_event(directory, "child-startup")
    lease = json.loads((directory / "lease.json").read_text())
    require(type(lease.get("expires")) in (int, float), "fixture lease expiry must be numeric")
    require(time.monotonic() < lease["expires"] <= time.monotonic() + LEASE_SECONDS,
            "fixture lease is expired or exceeds its bound")
    pid = os.getpid()

    def event(kind, **fields):
        worker_event(directory, kind, **fields)

    def signalled(number, _frame):
        event("signal", number=number)
        raise SystemExit(0)

    for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM, signal.SIGQUIT,
                   signal.SIGUSR1, signal.SIGUSR2, signal.SIGCONT, signal.SIGTSTP,
                   signal.SIGTTIN, signal.SIGTTOU, signal.SIGALRM, signal.SIGPIPE):
        signal.signal(number, signalled)
    event("start")
    while time.monotonic() < lease["expires"]:
        # The lease bounds abandoned children even if the controller is killed.
        heartbeat = {"pid": pid, "time": time.monotonic()}
        with (directory / "heartbeat-next").open("w") as stream:
            json.dump(heartbeat, stream)
        (directory / "heartbeat-next").replace(directory / "heartbeat.json")
        command = directory / "command"
        if command.exists() and command.read_text() == f"exit-42:{pid}":
            event("exit", code=42)
            return 42
        time.sleep(0.5)
    event("lease-expired")
    return 0


def child_entry(directory):
    """Retain a bounded structured failure, never an exception message or traceback."""
    if platform.system() != "Darwin":
        print("qualify-maintenance: fixture worker is Darwin-only", file=sys.stderr)
        return 2
    try:
        return child(directory)
    except (QualificationError, OSError, ValueError, KeyError, TypeError) as exc:
        fields = {"category": "guard" if isinstance(exc, QualificationError) else
                  "os-error" if isinstance(exc, OSError) else "invalid-input"}
        if isinstance(exc, OSError) and type(exc.errno) is int:
            fields["errno"] = exc.errno
        try:
            worker_event(worker_directory(directory), "startup-error", **fields)
        except (QualificationError, OSError, ValueError):
            pass  # No validated writable fixture means no child evidence, not never-exec proof.
        print("qualify-maintenance: fixture startup failed; structured evidence only", file=sys.stderr)
        return 2


class Fixture:
    def __init__(self, directory, label):
        self.directory = directory
        self.label = label
        self.domain = f"gui/{os.getuid()}"
        self.target = f"{self.domain}/{label}"
        self.touched = False
        self.known_pids = set()
        self.clock = time.monotonic
        self.sleep = time.sleep
        self.last_observed_state = None
        self.phase = "preflight"

    def launchctl(self, *args, missing_ok=False):
        try:
            result = subprocess.run(
                ["/bin/launchctl", *args], capture_output=True, text=True,
                timeout=COMMAND_TIMEOUT, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise QualificationError(f"launchctl {args[0]} timed out; outcome unknown") from exc
        if result.returncode:
            # Never treat arbitrary permission, domain or unsupported errors as absent.
            if (missing_ok and 'Could not find service "' + self.label + '"' in result.stderr):
                return None
            raise QualificationError(f"launchctl {args[0]} refused (exit {result.returncode}); "
                                     "raw output withheld because it may contain environment values")
        return result.stdout

    def state(self):
        output = self.launchctl("print", self.target, missing_ok=True)
        if output is None:
            return None
        pid = re.search(r"^\s*pid = ([0-9]+)\s*$", output, re.MULTILINE)
        last_exit = re.search(r"^\s*last exit code = ([0-9]+)\s*$", output, re.MULTILINE)
        result = {"pid": int(pid[1]) if pid else None,
                  "last_exit": int(last_exit[1]) if last_exit else None}
        for label, key, allowed in (("state", "state", STATE_VALUES), ("reason", "reason", REASON_VALUES)):
            match = re.search(r"^\s*" + label + r" = ([^\r\n]+)$", output, re.MULTILINE)
            if match:
                result[key] = match[1].strip() if match[1].strip() in allowed else "unrecognized"
        for label, key in (("runs", "runs"), ("last terminating signal", "last_signal"),
                           ("last spawn error", "spawn_errno")):
            match = re.search(r"^\s*" + label + r" = ([0-9]+)\s*$", output, re.MULTILINE)
            if match:
                result[key] = int(match[1])
        return result

    def events(self):
        path = self.directory / "events.jsonl"
        if not path.exists():
            return []
        # An incomplete last write is unavailable observation, never a clean pass.
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                raw = stream.read(MAX_EVENTS_BYTES + 1)
            require(len(raw) <= MAX_EVENTS_BYTES, "fixture event log exceeds its observation bound")
            return [json.loads(line) for line in raw.splitlines()]
        except (ValueError, OSError) as exc:
            raise QualificationError("fixture event log unavailable") from exc

    def startup_diagnostics(self):
        """Allowlisted evidence only, using the state already sampled by probes."""
        result = {"phase": self.phase, "last_observed_state": self.last_observed_state,
                  "heartbeat_present": (self.directory / "heartbeat.json").is_file()}
        try:
            events = self.events()
            result["event_count"] = len(events)
            result["child_startup_events"] = sum(row.get("event") == "child-startup" for row in events)
            errors = [row for row in events if row.get("event") == "startup-error"]
            result["startup_error_count"] = len(errors)
            result["child_startup_evidence"] = "observed" if result["child_startup_events"] else "unavailable"
            if errors:
                error = errors[-1]
                result["startup_error"] = {"category": error.get("category") if error.get("category") in
                                           {"guard", "os-error", "invalid-input"} else "unrecognized"}
                if type(error.get("errno")) is int:
                    result["startup_error"]["errno"] = error["errno"]
        except (QualificationError, OSError, ValueError, TypeError, AttributeError):
            result["events_available"] = False
        try:
            path = self.directory / "startup.err"
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise OSError("interpreter stderr is not a regular file")
                raw = stream.read(16385)
            result["interpreter_stderr"] = {
                "bytes_observed": len(raw), "truncated": len(raw) > 16384,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "categories": [name for name, marker in (
                    ("permission-denied", b"Permission denied"),
                    ("missing-file", b"No such file or directory"),
                    ("loader-error", b"dyld"),
                    ("python-traceback", b"Traceback (most recent call last)"),
                    ("import-error", b"ModuleNotFoundError"),
                ) if marker in raw],
            }
        except OSError:
            result["interpreter_stderr"] = {"available": False}
        return result

    def heartbeat(self, pid):
        try:
            item = json.loads((self.directory / "heartbeat.json").read_text())
        except (ValueError, OSError):
            return False
        return item.get("pid") == pid and 0 <= self.clock() - item.get("time", 0) < HEARTBEAT_MAX_AGE

    def wait_for(self, predicate, seconds, message):
        deadline = self.clock() + seconds
        while self.clock() < deadline:
            result = predicate()
            if result:
                return result
            self.sleep(1)
        raise QualificationError(message)

    def remember_pid(self, pid):
        require(type(pid) is int and pid > 1 and pid != os.getpid(), "invalid fixture PID")
        self.known_pids.add(pid)

    def observed_state(self):
        state = self.state()
        self.last_observed_state = state
        if state and state.get("pid") is not None:
            # Cleanup needs every observed process, even one that never writes
            # a heartbeat or startup event. Observation is not a passing control.
            self.remember_pid(state["pid"])
        return state

    def running(self, previous=None):
        state = self.observed_state()
        if state and state["pid"] and state["pid"] != previous and self.heartbeat(state["pid"]):
            return state["pid"]
        return None

    def assert_live(self, pid):
        state = self.observed_state()
        require(state and state["pid"] == pid and self.heartbeat(pid),
                "disable did not preserve the live PID and fresh heartbeat")
        require(not any(item["event"] == "signal" for item in self.events()),
                "fixture received a handled control signal")

    def observe(self, predicate, seconds):
        deadline = self.clock() + seconds
        while self.clock() < deadline:
            predicate()
            self.sleep(1)
        predicate()

    def request_failure(self, pid):
        (self.directory / "command").write_text(f"exit-42:{pid}")

    def failed(self, pid):
        state = self.observed_state()
        require(state is not None, "fixture unexpectedly unloaded")
        exits = [item for item in self.events()
                 if item["event"] == "exit" and item["pid"] == pid and item.get("code") == 42]
        return state["pid"] is None and state["last_exit"] == 42 and bool(exits)

    def assert_no_respawn(self, pid, starts):
        require(self.failed(pid), "disabled fixture respawned or exit 42 is not verifiable")
        require(sum(item["event"] == "start" for item in self.events()) == starts,
                "disabled fixture spawned another process")
        require(not any(item["event"] == "signal" for item in self.events()),
                "fixture received a handled control signal")

    def qualify(self):
        require(self.state() is None, "random fixture label already exists; refusing to adopt it")
        self.touched = True  # bootstrap can act even if its response times out.
        self.phase = "initial-start"
        self.launchctl("bootstrap", self.domain, str(self.directory / "job.plist"))
        first = self.wait_for(self.running, 20, "fixture did not start with a fresh heartbeat")
        self.phase = "positive-control"
        self.request_failure(first)
        self.wait_for(lambda: self.failed(first), 10, "positive control did not report exit 42")
        second = self.wait_for(lambda: self.running(first), 45,
                               "enabled unsuccessful-exit restart was not observed")
        require(sum(item["event"] == "start" for item in self.events()) == 2,
                "unexpected positive-control launch count")
        self.phase = "disabled-control"
        self.launchctl("disable", self.target)
        self.observe(lambda: self.assert_live(second), LIVE_SECONDS)
        self.request_failure(second)
        self.wait_for(lambda: self.failed(second), 10, "disabled fixture did not report exit 42")
        self.observe(lambda: self.assert_no_respawn(second, 2), NO_RESPAWN_SECONDS)
        self.phase = "reenable-control"
        self.launchctl("enable", self.target)
        # No -k: never replace a live process to make the re-enable test pass.
        self.launchctl("kickstart", self.target)
        third = self.wait_for(lambda: self.running(second), 20, "re-enabled fixture did not start")
        self.assert_live(third)
        require(sum(item["event"] == "start" for item in self.events()) == 3,
                "unexpected re-enable launch count")
        return {"enabled_failure_restarted": True, "disable_live_seconds": LIVE_SECONDS,
                "no_handled_control_signal": True, "disabled_no_respawn_seconds": NO_RESPAWN_SECONDS,
                "throttle_seconds": THROTTLE, "reenable_start_observed": True,
                "fixture_pids": [first, second, third]}

    def cleanup(self):
        if self.touched:
            # Restore only this random label's persistent disabled override.
            # bootout is allowed to signal the disposable worker, never rclone.
            errors = []
            try:
                self.launchctl("enable", self.target)
            except QualificationError as exc:
                errors.append(str(exc))
            # A failed enable must not skip the one owned-job removal attempt.
            try:
                if self.observed_state() is not None:
                    self.launchctl("bootout", self.target)
                self.wait_for(lambda: self.observed_state() is None, 10, "fixture remained loaded after cleanup")
                for item in self.events():
                    if item["event"] in {"child-startup", "start"}:
                        self.remember_pid(item["pid"])
                self.wait_for(lambda: not any(pid_alive(pid) for pid in self.known_pids),
                              10, "fixture PID still exists; refusing file cleanup")
            except QualificationError as exc:
                errors.append(str(exc))
            require(not errors, "; ".join(errors))
        remove_fixture_files(self.directory)


def pid_alive(pid):
    try:
        os.kill(pid, 0)  # existence check only; never sends a terminating signal.
        return True
    except ProcessLookupError:
        return False


def remove_fixture_files(directory):
    # No recursive deletion or caller-supplied glob. Unknown artifacts fail closed.
    require(directory.name.startswith("gdrive-maintenance-"), "unexpected fixture directory name")
    require(not directory.is_symlink(), "fixture directory became a symlink")
    for item in directory.iterdir():
        require(item.name in FIXTURE_FILES and item.is_file() and not item.is_symlink(),
                "unexpected fixture artifact; refusing cleanup")
    for name in FIXTURE_FILES:
        (directory / name).unlink(missing_ok=True)
    directory.rmdir()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attended", action="store_true", help="acknowledge disposable launchd mutations")
    parser.add_argument("--scratch-parent", required=True,
                        help="existing canonical, user-owned 0700 directory outside /tmp")
    args = parser.parse_args(argv)
    require(platform.system() == "Darwin", "qualification is Darwin-only")
    require(args.attended, "requires explicit --attended approval acknowledgment")
    require(os.getuid() != 0, "run as the ordinary GUI-session user, never root")
    parent = private_parent(args.scratch_parent)
    require(Path("/bin/launchctl").is_file(), "native launchctl unavailable")
    source = Path(__file__).resolve(strict=True)
    source_bytes = source.read_bytes()
    interpreter = str(Path(sys.executable).resolve(strict=True))
    # tempfile.mkdtemp is the mktemp primitive: exclusive unpredictable 0700 directory.
    os.umask(0o077)
    directory = Path(tempfile.mkdtemp(prefix="gdrive-maintenance-", dir=parent))
    fixture = Fixture(directory, PREFIX + uuid.uuid4().hex)
    receipt = {"qualification": "failed", "label": fixture.label, "darwin": platform.release(),
               "macos": platform.mac_ver()[0], "kernel": platform.version(),
               "host": platform.node(), "machine": platform.machine(),
               "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
               "interpreter": interpreter, "scratch_directory": str(directory),
               "keepalive": {"SuccessfulExit": False}, "run_at_load": True,
               "process_type": "Background",
               "scope": "disposable launchd observation only; not mount recovery or upload-drain proof"}
    def interrupted(_number, _frame):
        raise KeyboardInterrupt

    original_handlers = {number: signal.signal(number, interrupted)
                         for number in (signal.SIGHUP, signal.SIGTERM)}
    try:
        # All starts execute the same snapshot, even if the checkout changes.
        (directory / "worker.py").write_bytes(source_bytes)
        (directory / "lease.json").write_text(json.dumps({"expires": time.monotonic() + LEASE_SECONDS}))
        with (directory / "job.plist").open("wb") as stream:
            plistlib.dump(fixture_plist(fixture.label, directory, interpreter, directory / "worker.py"), stream)
        receipt.update(fixture.qualify())
        receipt["qualification"] = "passed"
    except (QualificationError, OSError, ValueError, KeyboardInterrupt) as exc:
        receipt["error"] = str(exc) if not isinstance(exc, KeyboardInterrupt) else "interrupted"
        receipt["startup_diagnostics"] = fixture.startup_diagnostics()
    finally:
        try:
            fixture.cleanup()
            receipt["cleanup"] = "fixture unloaded; disabled override cleared; private files removed"
        except (QualificationError, OSError, ValueError, KeyboardInterrupt) as exc:
            receipt["qualification"] = "failed"
            receipt["cleanup"] = "unverified; exact fixture directory retained"
            receipt["cleanup_error"] = str(exc)
        finally:
            for number, handler in original_handlers.items():
                signal.signal(number, handler)
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["qualification"] == "passed" else 1


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--fixture-child":
        sys.exit(child_entry(sys.argv[2]))
    try:
        sys.exit(main())
    except (QualificationError, OSError, ValueError) as error:
        print(f"qualify-maintenance: {error}", file=sys.stderr)
        sys.exit(2)
