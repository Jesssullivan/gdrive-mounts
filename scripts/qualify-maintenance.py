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
FIXTURE_FILES = ("job.plist", "worker.py", "lease.json", "events.jsonl", "heartbeat.json", "heartbeat-next", "command")


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
        "StandardErrorPath": "/dev/null",
    }


def child(directory):
    """Disposable worker; unsuccessful exits are requested through its file."""
    require(platform.system() == "Darwin", "fixture worker is Darwin-only")
    directory = private_parent(directory)
    require(directory.name.startswith("gdrive-maintenance-"), "unexpected fixture directory name")
    lease = json.loads((directory / "lease.json").read_text())
    require(lease["expires"] <= time.monotonic() + LEASE_SECONDS, "fixture lease exceeds its bound")
    pid = os.getpid()

    def event(kind, **fields):
        with (directory / "events.jsonl").open("a") as stream:
            stream.write(json.dumps({"event": kind, "pid": pid,
                                     "time": time.monotonic(), **fields}) + "\n")

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
        return {"pid": int(pid[1]) if pid else None,
                "last_exit": int(last_exit[1]) if last_exit else None}

    def events(self):
        path = self.directory / "events.jsonl"
        if not path.exists():
            return []
        # An incomplete last write is unavailable observation, never a clean pass.
        try:
            return [json.loads(line) for line in path.read_text().splitlines()]
        except (ValueError, OSError) as exc:
            raise QualificationError("fixture event log unavailable") from exc

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

    def running(self, previous=None):
        state = self.state()
        if state and state["pid"] and state["pid"] != previous and self.heartbeat(state["pid"]):
            pid = state["pid"]
            require(pid > 1 and pid != os.getpid(), "invalid fixture PID")
            self.known_pids.add(pid)
            return pid
        return None

    def assert_live(self, pid):
        state = self.state()
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
        state = self.state()
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
        self.launchctl("bootstrap", self.domain, str(self.directory / "job.plist"))
        first = self.wait_for(self.running, 20, "fixture did not start with a fresh heartbeat")
        self.request_failure(first)
        self.wait_for(lambda: self.failed(first), 10, "positive control did not report exit 42")
        second = self.wait_for(lambda: self.running(first), 45,
                               "enabled unsuccessful-exit restart was not observed")
        require(sum(item["event"] == "start" for item in self.events()) == 2,
                "unexpected positive-control launch count")
        self.launchctl("disable", self.target)
        self.observe(lambda: self.assert_live(second), LIVE_SECONDS)
        self.request_failure(second)
        self.wait_for(lambda: self.failed(second), 10, "disabled fixture did not report exit 42")
        self.observe(lambda: self.assert_no_respawn(second, 2), NO_RESPAWN_SECONDS)
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
                if self.state() is not None:
                    self.launchctl("bootout", self.target)
                self.wait_for(lambda: self.state() is None, 10, "fixture remained loaded after cleanup")
                for item in self.events():
                    if item["event"] == "start":
                        self.known_pids.add(item["pid"])
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
        sys.exit(child(sys.argv[2]))
    try:
        sys.exit(main())
    except (QualificationError, OSError, ValueError) as error:
        print(f"qualify-maintenance: {error}", file=sys.stderr)
        sys.exit(2)
