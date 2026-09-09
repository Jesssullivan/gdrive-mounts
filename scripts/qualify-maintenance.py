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
import shlex
import signal
import socket
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
FIXTURE_FILES = ("job.plist", "worker.py", "lease.json", "events.jsonl", "heartbeat.json", "heartbeat-next", "command", "startup.err", "pre-interpreter.jsonl", "python-entry.json", "startup.sample", "capture-client", "watchdog.sh", "capture.log", "capture-mode", "capture.sock", "restart-intent", "restart-floor")
LEGACY_WATCHDOG_SHA256 = "8768d03483995c301c5bd721f5979edbf02e8c76978fb6f0c02411e29b832e2d"
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
    bootstrap = (
        "import os, sys\n"
        f"fd = os.open({str(directory / 'python-entry.json')!r}, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)\n"
        "os.write(fd, ('{\"phase\":\"python-entry\",\"pid\":%d}\\n' % os.getpid()).encode())\n"
        "os.close(fd)\n"
        "import runpy\n"
        f"sys.argv = [{str(source)!r}] + sys.argv[1:]\n"
        f"runpy.run_path({str(source)!r}, run_name='__main__')\n"
    )
    entry = ("umask 077\nprintf '{\"phase\":\"pre-interpreter\",\"pid\":%s}\\n' \"$$\"\nexec " +
             shlex.join([interpreter, "-I", "-B", "-c", bootstrap, "--fixture-child", str(directory)]))
    return {
        "Label": label,
        "ProgramArguments": ["/bin/sh", "-c", entry],
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
        "RunAtLoad": True,
        "ThrottleInterval": THROTTLE,
        "ExitTimeOut": 5,
        "EnvironmentVariables": {"PYTHONDONTWRITEBYTECODE": "1"},
        "StandardOutPath": str(directory / "pre-interpreter.jsonl"),
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
        if command.exists():
            value = command.read_text()
            for code in (0, 42):
                if value == f"exit-{code}:{pid}":
                    event("exit", code=code)
                    return code
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
        for name in ("pre-interpreter.jsonl", "python-entry.json"):
            try:
                fd = os.open(self.directory / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as stream:
                    require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "startup marker is not regular")
                    raw = stream.read(4097)
                require(len(raw) <= 4096, "startup marker exceeds bound")
                rows = [json.loads(line) for line in raw.splitlines()]
                result[name] = [{"pid": row["pid"], "phase": row["phase"]} for row in rows
                                if type(row.get("pid")) is int and row.get("phase") in {"pre-interpreter", "python-entry"}]
            except (QualificationError, OSError, ValueError, KeyError, TypeError, AttributeError):
                result[name] = "unavailable"
        if self.touched and platform.system() == "Darwin":
            try:
                current = self.observed_state()
                pid = current.get("pid") if current else None
                if pid:
                    process = native_process_snapshot().get(pid)
                    require(process is not None, "startup process disappeared before attribution")
                    result["live_process"] = {"pid": pid, **process}
                    # Only this still-live owned fixture is sampled. No grants,
                    # elevated attach, raw environment or production PID fallback.
                    sample = subprocess.run(["/usr/bin/sample", str(pid), "1", "10", "-mayDie",
                                             "-file", str(self.directory / "startup.sample")],
                                            capture_output=True, timeout=5, check=False)
                    info = {"exit": sample.returncode}
                    path = self.directory / "startup.sample"
                    if path.exists():
                        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                        with os.fdopen(fd, "rb") as stream:
                            require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "sample is not regular")
                            raw = stream.read(256 * 1024 + 1)
                        info.update({"bytes_observed": len(raw), "truncated": len(raw) > 256 * 1024,
                                     "sha256": hashlib.sha256(raw).hexdigest(),
                                     "frames": [line.strip() for line in raw.decode(errors="replace").splitlines()
                                                if re.match(r"^\s*\+*\s*\d+\s+\S", line)][:128]})
                    result["native_sample"] = info
                    after = native_process_snapshot().get(pid)
                    result["same_process_after_sample"] = after == process
                    cwd = subprocess.run(["/usr/sbin/lsof", "-nP", "-b", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                                         capture_output=True, text=True, timeout=5, check=False)
                    result["cwd"] = [line[1:] for line in cwd.stdout.splitlines() if line.startswith("n")][:1]
                    result["cwd_probe_exit"] = cwd.returncode
            except (QualificationError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
                result["live_attribution"] = "unavailable: " + type(exc).__name__
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
        return self.exited(pid, 42)

    def exited(self, pid, code):
        state = self.observed_state()
        require(state is not None, "fixture unexpectedly unloaded")
        exits = [item for item in self.events()
                 if item["event"] == "exit" and item["pid"] == pid and item.get("code") == code]
        return state["pid"] is None and state["last_exit"] == code and bool(exits)

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

    def qualify_startup(self):
        require(self.state() is None, "random fixture label already exists; refusing to adopt it")
        self.touched = True
        self.phase = "startup-only"
        self.launchctl("bootstrap", self.domain, str(self.directory / "job.plist"))
        pid = self.wait_for(self.running, 20, "fixture did not start with a fresh heartbeat")
        return {"startup_ready": True, "fixture_pids": [pid],
                "scope": "startup diagnostic only; no supervision, mount or upload-drain qualification"}

    def cleanup(self, preserve_files=False):
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
                    elif item["event"] == "capture-start":
                        self.remember_pid(item["pid"])
                        self.remember_pid(item["ppid"])
                self.wait_for(lambda: not any(pid_alive(pid) for pid in self.known_pids),
                              10, "fixture PID still exists; refusing file cleanup")
            except QualificationError as exc:
                errors.append(str(exc))
            require(not errors, "; ".join(errors))
        if not preserve_files:
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
        require(item.name in FIXTURE_FILES and not item.is_symlink() and
                (item.is_file() or (item.name == "capture.sock" and stat.S_ISSOCK(item.stat().st_mode))),
                "unexpected fixture artifact; refusing cleanup")
    for name in FIXTURE_FILES:
        (directory / name).unlink(missing_ok=True)
    directory.rmdir()


def legacy_capture_source(path):
    """Accept only the inspected immutable Neo script, never arbitrary shell input."""
    path = Path(path)
    require(path.is_absolute() and path.parent == Path("/nix/store") and not path.is_symlink(),
            "legacy watchdog must be an immutable /nix/store script")
    raw = path.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == LEGACY_WATCHDOG_SHA256,
            "legacy watchdog differs from the inspected source; review before qualification")
    text = raw.decode()
    capture = text.split("gdm_capture() { # pid\n", 1)[1].split("\n# Append-only,", 1)[0]
    handler = text.split("gdm_handle_wedge() {\n", 1)[1].split("\ngdm_note_failure()", 1)[0]
    interpreter = text.splitlines()[0][2:]
    runtime_path = next(line[12:] for line in text.splitlines() if line.startswith("export PATH="))
    require(Path(interpreter).is_file(), "installed watchdog interpreter unavailable")
    return interpreter, runtime_path, "gdm_capture() { # pid\n" + capture, "gdm_handle_wedge() {\n" + handler


def capture_child(directory):
    """Owned RC stand-in: record real timeout ancestry, never contact any server."""
    directory = worker_directory(directory)
    worker_event(directory, "capture-start", ppid=os.getppid(), pgid=os.getpgrp())
    if (directory / "capture-mode").read_text() == "positive":
        return 1
    require((directory / "capture-mode").read_text() == "blocked", "invalid capture mode")
    deadline = min(json.loads((directory / "lease.json").read_text())["expires"], time.monotonic() + 120)
    while time.monotonic() < deadline:
        time.sleep(0.5)
    return 1


def owned_descendants(processes, root_pid):
    require(root_pid in processes, "owned watchdog is absent from process snapshot")
    owned = {root_pid}
    while True:
        expanded = owned | {pid for pid, row in processes.items() if row["ppid"] in owned}
        if expanded == owned:
            return {pid: processes[pid] for pid in sorted(owned)}
        owned = expanded


def native_process_snapshot():
    try:
        result = subprocess.run(["/bin/ps", "-axo", "pid=,ppid=,pgid=,lstart=,comm="],
                                capture_output=True, text=True, timeout=COMMAND_TIMEOUT, check=False)
    except subprocess.TimeoutExpired as exc:
        raise QualificationError("native process snapshot timed out; descendant custody unavailable") from exc
    require(result.returncode == 0 and len(result.stdout) <= 2 * 1024 * 1024,
            "native process snapshot unavailable or exceeds bound")
    processes = {}
    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=8)
        require(len(fields) == 9 and all(value.isdigit() for value in fields[:3]),
                "native process snapshot is malformed")
        processes[int(fields[0])] = {"ppid": int(fields[1]), "pgid": int(fields[2]),
                                     "started": " ".join(fields[3:8]), "command": Path(fields[8]).name}
    return processes


def watchdog_fixture_script(directory, mount_pid, mount_label, interpreter, runtime_path, capture, handler,
                            watchdog_label=None, lease_expires=None):
    require(type(mount_pid) is int and mount_pid > 1, "invalid owned mount fixture PID")
    require(re.fullmatch(re.escape(PREFIX) + r"mount\.[a-f0-9]{32}", mount_label) is not None,
            "restart target must be the random owned mount fixture")
    require(isinstance(watchdog_label, str) and
            re.fullmatch(re.escape(PREFIX) + r"watchdog\.[a-f0-9]{32}", watchdog_label) is not None,
            "expiry cleanup target must be the random owned watchdog fixture")
    require(type(lease_expires) in (int, float) and 0 < lease_expires - time.monotonic() <= LEASE_SECONDS,
            "watchdog fixture lease is expired or exceeds its bound")
    expiry_epoch = int(time.time() + lease_expires - time.monotonic())
    q = shlex.quote
    # The inspected capture body runs unchanged. Native nfsstat is replaced by
    # an inert shell function and the RC executable/socket point only to this
    # fixture. The original handler keeps its real SIGQUIT and native
    # kickstart -k, with only its label changed to this owned mount fixture.
    handler = handler.replace("dev.tinyland.gdrive-mounts.sulliwood-root", mount_label)
    return (f"#!{interpreter}\nset -euo pipefail\nexport PATH={q(runtime_path)}\n"
            f"unit=owned-watchdog\nconsecutive=2\nprobe_timeout=10\n"
            f"capture={q(str(directory / 'capture.log'))}\n"
            f"sock={q(str(directory / 'capture.sock'))}\n"
            f"rclone_bin={q(str(directory / 'capture-client'))}\n"
            f"floor_file={q(str(directory / 'restart-floor'))}\n"
            f"fixture_expiry={expiry_epoch}\n"
            f"fixture_target={q('gui/' + str(os.getuid()) + '/' + watchdog_label)}\n"
            "gdm_fixture_active() { [ \"$(date +%s)\" -lt \"$fixture_expiry\" ]; }\n"
            "if ! gdm_fixture_active; then exec /bin/launchctl bootout \"$fixture_target\"; fi\n"
            "gdm_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }\n"
            "gdm_epoch() { date +%s; }\ngdm_pid_uptime() { printf 0; }\n"
            "gdm_floor_blocks() { return 1; }\ngdm_record() { :; }\n"
            "gdm_log() { :; }\nnfsstat() { :; }\n" + capture + handler +
            f"\ngdm_handle_wedge\nprintf '%s\\n' restart-intent > {q(str(directory / 'restart-intent'))}\n"
            # Preserve the unconditional KeepAlive shape, but bound abandoned
            # fixtures by unloading this exact random label at static expiry.
            "while gdm_fixture_active; do sleep 1; done\n"
            "exec /bin/launchctl bootout \"$fixture_target\"\n")


def qualify_watchdog_handover(parent, legacy_path):
    """Qualify native supervision only; an exit-0 marker is not an NFS callback."""
    interpreter, runtime_path, capture, handler = legacy_capture_source(legacy_path)
    source = Path(__file__).resolve().read_bytes()
    python = str(Path(sys.executable).resolve())
    fixtures = []
    listener = None
    def interrupted(_number, _frame):
        raise KeyboardInterrupt
    original_handlers = {number: signal.signal(number, interrupted)
                         for number in (signal.SIGHUP, signal.SIGTERM)}
    receipt = {"qualification": "failed", "sequence": "watchdog-handover",
               "source_sha256": hashlib.sha256(source).hexdigest(),
               "legacy_watchdog_sha256": LEGACY_WATCHDOG_SHA256,
               "legacy_capture_sha256": hashlib.sha256(capture.encode()).hexdigest(),
               "legacy_handler_sha256": hashlib.sha256(handler.encode()).hexdigest(),
               "macos": platform.mac_ver()[0], "host": platform.node(),
               "mount_keepalive": {"SuccessfulExit": False}, "watchdog_keepalive": True,
               "throttle_seconds": THROTTLE, "abandon_process_group": "native default",
               "exit_timeout": "native default", "interpreter": interpreter,
               "mount_interpreter": python, "mount_interpreter_isolated": True,
               "mount_entry_shell": "/bin/sh",
               "scope": "owned native supervision and simulated clean exit only; no NFS mount or upload-drain proof"}
    try:
        for role in ("mount", "watchdog"):
            directory = Path(tempfile.mkdtemp(prefix="gdrive-maintenance-", dir=parent))
            fixture = Fixture(directory, PREFIX + role + "." + uuid.uuid4().hex)
            fixtures.append(fixture)
            (directory / "worker.py").write_bytes(source)
            (directory / "lease.json").write_text(json.dumps({"expires": time.monotonic() + LEASE_SECONDS}))
        mount, watchdog = fixtures
        receipt["labels"] = [item.label for item in fixtures]
        receipt["scratch_directories"] = [str(item.directory) for item in fixtures]
        mount_plist = fixture_plist(mount.label, mount.directory, python, mount.directory / "worker.py")
        mount_plist.pop("ExitTimeOut")
        (mount.directory / "job.plist").write_bytes(plistlib.dumps(mount_plist))
        require(mount.state() is None and watchdog.state() is None, "fresh label already exists")
        mount.touched = True
        mount.launchctl("bootstrap", mount.domain, str(mount.directory / "job.plist"))
        first = mount.wait_for(mount.running, 20, "owned mount positive control did not start")
        directory = watchdog.directory
        client = "#!" + interpreter + "\nexec " + shlex.join([python, "-I", "-B", str(directory / "worker.py"), "--fixture-capture", str(directory)]) + "\n"
        (directory / "capture-client").write_text(client)
        (directory / "capture-client").chmod(0o700)
        listener = socket.socket(socket.AF_UNIX)
        # Unix socket names have a small sockaddr bound. Bind relative to this
        # exact private directory without moving any other process's cwd.
        original_cwd = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.chdir(directory)
            listener.bind("capture.sock")
        finally:
            os.fchdir(original_cwd)
            os.close(original_cwd)
        watchdog_plist = fixture_plist(watchdog.label, directory, python, directory / "worker.py")
        watchdog_plist.update({"KeepAlive": True, "ProgramArguments": ["/bin/sh", "-c", "exec " + shlex.quote(str(directory / "watchdog.sh"))]})
        watchdog_plist.pop("ExitTimeOut")
        (directory / "job.plist").write_bytes(plistlib.dumps(watchdog_plist))

        def start_capture(pid, mode):
            (directory / "capture-mode").write_text(mode)
            (directory / "watchdog.sh").write_text(watchdog_fixture_script(
                directory, pid, mount.label, interpreter, runtime_path, capture, handler,
                watchdog_label=watchdog.label,
                lease_expires=json.loads((directory / "lease.json").read_text())["expires"]))
            (directory / "watchdog.sh").chmod(0o700)
            watchdog.touched = True
            watchdog.launchctl("bootstrap", watchdog.domain, str(directory / "job.plist"))

        def remember_capture():
            watchdog.observed_state()
            rows = [row for row in watchdog.events() if row.get("event") == "capture-start"]
            for row in rows:
                watchdog.remember_pid(row["pid"])
                watchdog.remember_pid(row["ppid"])
            return rows

        def bootout_watchdog():
            remember_capture()
            current = watchdog.observed_state()
            require(current and current.get("pid") and current.get("runs") == 1,
                    "watchdog process ownership changed before bootout")
            tree = owned_descendants(native_process_snapshot(), current["pid"])
            receipt.setdefault("watchdog_process_snapshots", []).append(tree)
            for pid in tree:
                watchdog.remember_pid(pid)
            watchdog.launchctl("bootout", watchdog.target)
            watchdog.wait_for(lambda: watchdog.observed_state() is None, 10, "owned watchdog remained loaded")
            watchdog.wait_for(lambda: not any(pid_alive(pid) for pid in watchdog.known_pids),
                              20, "owned watchdog/capture descendant remains; containment unproved")

        receipt["phase"] = "positive-capture-tail"
        start_capture(first, "positive")
        mount.wait_for(lambda: any(row.get("event") == "signal" and row.get("number") == signal.SIGQUIT
                                  and row.get("pid") == first for row in mount.events()),
                       20, "positive capture did not deliver observable SIGQUIT")
        watchdog.wait_for(lambda: (directory / "restart-intent").is_file(), 10, "positive restart tail was not reached")
        bootout_watchdog()
        require(not pid_alive(first), "positive control mount worker did not exit")
        require(len(remember_capture()) == 5, "positive capture did not execute all five RC child calls")
        receipt["positive_capture_sigquit_and_restart_intent"] = True
        (directory / "restart-intent").unlink()
        second = mount.wait_for(lambda: mount.running(first), 20, "positive native restart did not replace the mount worker")
        prior_signals = [row for row in mount.events() if row.get("event") == "signal"]
        receipt["phase"] = "in-flight-capture-bootout"
        start_capture(second, "blocked")
        rows = watchdog.wait_for(lambda: (items if len(items := remember_capture()) == 6 else None),
                                 20, "in-flight RC child readiness was not observed")
        receipt["capture_ancestry"] = rows[-1]
        bootout_watchdog()

        def untouched_mount():
            state = mount.observed_state()
            require(state and state["pid"] == second and mount.heartbeat(second), "watchdog bootout disturbed owned mount")
            require([row for row in mount.events() if row.get("event") == "signal"] == prior_signals,
                    "late signal reached the owned mount")
            require(not (directory / "restart-intent").exists(), "late restart tail survived watchdog bootout")
        # Observe beyond this actual child start's timeout + kill-after window,
        # as well as proving every recorded parent/descendant PID has exited.
        mount.observe(untouched_mount, max(LIVE_SECONDS, rows[-1]["time"] + 18 - time.monotonic()))
        receipt["watchdog_and_capture_terminated_mount_preserved"] = True
        receipt["phase"] = "simulated-clean-exit"
        (mount.directory / "command").write_text(f"exit-0:{second}")
        mount.wait_for(lambda: mount.exited(second, 0), 10, "owned mount did not exit cleanly")
        def stopped():
            require(mount.exited(second, 0) and not pid_alive(second), "clean-exit fixture respawned or exit became ambiguous")
        mount.observe(stopped, NO_RESPAWN_SECONDS)
        receipt["clean_exit_no_respawn_seconds"] = NO_RESPAWN_SECONDS
        mount.launchctl("bootout", mount.target)
        mount.wait_for(lambda: mount.observed_state() is None, 10, "exited mount fixture did not unload")
        receipt["phase"] = "replacement-start"
        mount.launchctl("bootstrap", mount.domain, str(mount.directory / "job.plist"))
        third = mount.wait_for(lambda: mount.running(second), 20, "replacement fixture did not start")
        receipt.update({"qualification": "passed", "replacement_started": True,
                        "mount_pids": [first, second, third]})
    except (QualificationError, OSError, ValueError, KeyboardInterrupt) as exc:
        receipt["error"] = str(exc)
        receipt["diagnostics"] = [item.startup_diagnostics() for item in fixtures]
    finally:
        if listener is not None:
            listener.close()
        failures = []
        for fixture in reversed(fixtures):
            preserve_files = False
            try:
                if fixture is fixtures[-1] and len(fixtures) == 2 and fixture.touched:
                    current = fixture.observed_state()
                    if current and current.get("pid"):
                        for pid in owned_descendants(native_process_snapshot(), current["pid"]):
                            fixture.remember_pid(pid)
            except (QualificationError, OSError, ValueError, KeyboardInterrupt) as exc:
                # An unavailable process census must not skip the owned-label
                # unload, but unknown descendant custody prohibits file removal.
                preserve_files = True
                failures.append({"label": fixture.label, "directory": str(fixture.directory), "error": str(exc)})
            try:
                fixture.cleanup(preserve_files=preserve_files)
            except (QualificationError, OSError, ValueError, KeyboardInterrupt) as exc:
                failures.append({"label": fixture.label, "directory": str(fixture.directory), "error": str(exc)})
        receipt["cleanup"] = "exact owned services/PIDs/files removed" if not failures else failures
        if failures:
            receipt["qualification"] = "failed"
        for number, handler in original_handlers.items():
            signal.signal(number, handler)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attended", action="store_true", help="acknowledge disposable launchd mutations")
    parser.add_argument("--scratch-parent", required=True,
                        help="existing canonical, user-owned 0700 directory outside /tmp")
    parser.add_argument("--legacy-watchdog-script", help="qualify the inspected legacy capture/bootout handover instead of disable")
    parser.add_argument("--startup-only", action="store_true", help="diagnose owned interpreter startup without restart/disable tests")
    args = parser.parse_args(argv)
    require(platform.system() == "Darwin", "qualification is Darwin-only")
    require(args.attended, "requires explicit --attended approval acknowledgment")
    require(not (args.startup_only and args.legacy_watchdog_script), "select only one native experiment")
    require(os.getuid() != 0, "run as the ordinary GUI-session user, never root")
    parent = private_parent(args.scratch_parent)
    require(Path("/bin/launchctl").is_file(), "native launchctl unavailable")
    if args.legacy_watchdog_script:
        os.umask(0o077)
        receipt = qualify_watchdog_handover(parent, args.legacy_watchdog_script)
        print(json.dumps(receipt, sort_keys=True))
        return 0 if receipt["qualification"] == "passed" else 1
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
               "interpreter_isolated": True, "entry_shell": "/bin/sh",
               "declared_environment": {"PYTHONDONTWRITEBYTECODE": "1"},
               "working_directory": "native launchd default; observed on startup failure",
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
        receipt.update(fixture.qualify_startup() if args.startup_only else fixture.qualify())
        if args.startup_only:
            receipt["startup_diagnostics"] = fixture.startup_diagnostics()
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
    if len(sys.argv) == 3 and sys.argv[1] == "--fixture-capture":
        require(platform.system() == "Darwin", "capture worker is Darwin-only")
        sys.exit(capture_child(sys.argv[2]))
    if len(sys.argv) == 3 and sys.argv[1] == "--fixture-child":
        sys.exit(child_entry(sys.argv[2]))
    try:
        sys.exit(main())
    except (QualificationError, OSError, ValueError) as error:
        print(f"qualify-maintenance: {error}", file=sys.stderr)
        sys.exit(2)
