"""Offline tests: all launchd/process observations are mocked, including cleanup."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch


SOURCE = Path(__file__).with_name("qualify-maintenance.py")
SPEC = importlib.util.spec_from_file_location("qualify_maintenance", SOURCE)
q = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(q)


class SimulatedFixture(q.Fixture):
    """Virtual time and owned fake PIDs; no launchctl, signals, or disk access."""

    def __init__(self, behavior="supported"):
        super().__init__(Path("/unused/gdrive-maintenance-synthetic"), q.PREFIX + "test")
        self.now = 0
        self.clock = lambda: self.now
        self.sleep = self.advance
        self.behavior = behavior
        self.loaded = False
        self.disabled = False
        self.pid = None
        self.previous_pid = 500000
        self.last_exit = None
        self.restart_at = None
        self.records = []
        self.calls = []

    def advance(self, seconds):
        self.now += seconds

    def start(self):
        self.previous_pid += 1
        self.pid = self.previous_pid
        self.records.append({"event": "start", "pid": self.pid})
        self.restart_at = None

    def state(self):
        if self.restart_at is not None and self.now >= self.restart_at:
            self.start()
        return {"pid": self.pid, "last_exit": self.last_exit} if self.loaded else None

    def launchctl(self, *args, **_kwargs):
        self.calls.append(args)
        verb = args[0]
        if verb == "bootstrap":
            self.loaded = True
            self.start()
        elif verb == "disable":
            if self.behavior == "unsupported":
                raise q.QualificationError("launchctl disable refused")
            self.disabled = True
            if self.behavior == "signal-on-disable":
                self.records.append({"event": "signal", "pid": self.pid, "number": 15})
            if self.behavior == "stop-on-disable":
                self.pid = None
        elif verb == "enable":
            self.disabled = False
        elif verb == "kickstart":
            if self.behavior != "reenable-broken":
                self.start()
        elif verb == "bootout":
            self.loaded = False
            self.pid = None
            self.restart_at = None
        else:
            raise AssertionError(args)
        return ""

    def events(self):
        return self.records

    def heartbeat(self, pid):
        return self.pid == pid and not (self.disabled and self.behavior == "stalled-heartbeat")

    def request_failure(self, pid):
        self.records.append({"event": "exit", "pid": pid, "code": 42})
        self.pid = None
        self.last_exit = 42 if self.behavior != "wrong-exit-code" else 0
        if ((not self.disabled and self.behavior != "no-positive-restart")
                or self.behavior == "disable-ignored"):
            self.restart_at = self.now + q.THROTTLE


class QualificationTests(unittest.TestCase):
    def test_supported_observations_and_exact_target(self):
        fixture = SimulatedFixture()
        result = fixture.qualify()
        self.assertTrue(result["enabled_failure_restarted"])
        self.assertEqual(result["disabled_no_respawn_seconds"], 45)
        self.assertGreater(result["disabled_no_respawn_seconds"], q.THROTTLE)
        self.assertGreaterEqual(fixture.now, q.THROTTLE + q.LIVE_SECONDS + q.NO_RESPAWN_SECONDS)
        self.assertEqual([call[0] for call in fixture.calls],
                         ["bootstrap", "disable", "enable", "kickstart"])
        self.assertTrue(all(call[1] == fixture.target for call in fixture.calls[1:]))
        self.assertNotIn("-k", [arg for call in fixture.calls for arg in call])

    def test_failure_paths_cannot_pass(self):
        for behavior in ("signal-on-disable", "stop-on-disable", "stalled-heartbeat",
                         "disable-ignored", "unsupported", "no-positive-restart",
                         "wrong-exit-code", "reenable-broken"):
            with self.subTest(behavior=behavior):
                fixture = SimulatedFixture(behavior)
                with self.assertRaises(q.QualificationError):
                    fixture.qualify()
                self.assertLess(fixture.now, 150)

    def test_existing_label_is_not_adopted_or_changed(self):
        fixture = SimulatedFixture()
        fixture.loaded = True
        with self.assertRaisesRegex(q.QualificationError, "already exists"):
            fixture.qualify()
        self.assertFalse(fixture.touched)
        self.assertEqual(fixture.calls, [])

    def test_cleanup_only_targets_fixture_and_checks_absence(self):
        fixture = SimulatedFixture()
        fixture.qualify()
        with patch.object(q, "pid_alive", return_value=False), patch.object(q, "remove_fixture_files") as remove:
            fixture.cleanup()
        self.assertEqual(fixture.calls[-2:], [("enable", fixture.target), ("bootout", fixture.target)])
        self.assertFalse(fixture.loaded)
        remove.assert_called_once_with(fixture.directory)

    def test_cleanup_retains_files_when_pid_still_exists(self):
        fixture = SimulatedFixture()
        fixture.qualify()
        with patch.object(q, "pid_alive", return_value=True), patch.object(q, "remove_fixture_files") as remove:
            with self.assertRaisesRegex(q.QualificationError, "PID still exists"):
                fixture.cleanup()
        remove.assert_not_called()

    def test_cleanup_checks_observed_pid_without_heartbeat_or_start_event(self):
        for observed_before_cleanup in (False, True):
            with self.subTest(observed_before_cleanup=observed_before_cleanup):
                fixture = SimulatedFixture()
                fixture.loaded, fixture.touched, fixture.pid = True, True, 70454
                fixture.heartbeat = Mock(return_value=False)
                self.assertEqual(fixture.events(), [])
                if observed_before_cleanup:
                    self.assertIsNone(fixture.running())
                    self.assertIn(70454, fixture.known_pids)
                with patch.object(q, "pid_alive", return_value=True) as alive, \
                        patch.object(q, "remove_fixture_files") as remove:
                    with self.assertRaisesRegex(q.QualificationError, "PID still exists"):
                        fixture.cleanup()
                self.assertFalse(fixture.loaded)
                self.assertIn(("bootout", fixture.target), fixture.calls)
                self.assertIn(70454, fixture.known_pids)
                alive.assert_called_with(70454)
                remove.assert_not_called()

    def test_cleanup_tracks_pid_first_seen_while_waiting_for_unload(self):
        fixture = SimulatedFixture()
        fixture.touched = True
        fixture.state = Mock(side_effect=[{"pid": None, "last_exit": 42},
                                         {"pid": 70455, "last_exit": 42}, None])
        with patch.object(q, "pid_alive", return_value=True) as alive, \
                patch.object(q, "remove_fixture_files") as remove:
            with self.assertRaisesRegex(q.QualificationError, "PID still exists"):
                fixture.cleanup()
        self.assertIn(70455, fixture.known_pids)
        alive.assert_called_with(70455)
        remove.assert_not_called()

    def test_invalid_observed_pid_is_never_accepted_or_probed(self):
        for pid in (True, 0, 1, -1, "70454", os.getpid()):
            with self.subTest(pid=pid):
                fixture = SimulatedFixture()
                fixture.loaded, fixture.pid = True, pid
                with self.assertRaisesRegex(q.QualificationError, "invalid fixture PID"):
                    fixture.running()
                self.assertEqual(fixture.known_pids, set())

    def test_child_startup_pid_without_start_event_blocks_file_cleanup(self):
        fixture = SimulatedFixture()
        fixture.touched = True
        fixture.records = [{"event": "child-startup", "pid": 70456}]
        with patch.object(q, "pid_alive", return_value=True) as alive, \
                patch.object(q, "remove_fixture_files") as remove:
            with self.assertRaisesRegex(q.QualificationError, "PID still exists"):
                fixture.cleanup()
        alive.assert_called_with(70456)
        remove.assert_not_called()

    def test_cleanup_refusal_is_not_ignored(self):
        fixture = SimulatedFixture()
        fixture.qualify()
        fixture.launchctl = Mock(side_effect=q.QualificationError("cleanup refused"))
        with patch.object(q, "remove_fixture_files") as remove:
            with self.assertRaisesRegex(q.QualificationError, "cleanup refused"):
                fixture.cleanup()
        remove.assert_not_called()

    def test_failed_enable_still_attempts_owned_job_removal(self):
        fixture = SimulatedFixture()
        fixture.qualify()
        original = fixture.launchctl

        def fail_enable(*args, **kwargs):
            if args[0] == "enable":
                raise q.QualificationError("enable refused")
            return original(*args, **kwargs)

        fixture.launchctl = fail_enable
        with patch.object(q, "pid_alive", return_value=False), patch.object(q, "remove_fixture_files") as remove:
            with self.assertRaisesRegex(q.QualificationError, "enable refused"):
                fixture.cleanup()
        self.assertFalse(fixture.loaded)
        self.assertEqual(fixture.calls[-1], ("bootout", fixture.target))
        remove.assert_not_called()

    def test_plist_matches_mount_supervision_shape_without_mount_command(self):
        item = q.fixture_plist(q.PREFIX + "test", Path("/private-fixture"), "/python", SOURCE)
        self.assertEqual(item["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(item["ProcessType"], "Background")
        self.assertTrue(item["RunAtLoad"])
        self.assertEqual(item["ThrottleInterval"], 30)
        self.assertEqual(item["ProgramArguments"], ["/python", str(SOURCE), "--fixture-child", "/private-fixture"])
        self.assertNotIn("rclone", json.dumps(item))

    def test_launchctl_only_accepts_exact_missing_service_error(self):
        fixture = q.Fixture(Path("/unused"), q.PREFIX + "test")
        for error in ("Permission denied", "Unsupported subcommand", 'Could not find service "other"'):
            with self.subTest(error=error), patch.object(q.subprocess, "run", return_value=subprocess.CompletedProcess([], 113, "", error)):
                with self.assertRaises(q.QualificationError):
                    fixture.state()
        missing = f'Could not find service "{fixture.label}" in domain for user gui: 501'
        with patch.object(q.subprocess, "run", return_value=subprocess.CompletedProcess([], 113, "", missing)):
            self.assertIsNone(fixture.state())

    def test_launchctl_timeout_does_not_claim_no_action(self):
        fixture = q.Fixture(Path("/unused"), q.PREFIX + "test")
        with patch.object(q.subprocess, "run", side_effect=subprocess.TimeoutExpired("launchctl", 5)):
            with self.assertRaisesRegex(q.QualificationError, "outcome unknown"):
                fixture.launchctl("disable", fixture.target)

    def test_state_parser_does_not_expose_environment(self):
        fixture = q.Fixture(Path("/unused"), q.PREFIX + "test")
        output = "\tpid = 412\n\tlast exit code = 42\n\tenvironment = { PRIVATE_VALUE = withheld }\n"
        with patch.object(q.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output, "")):
            self.assertEqual(fixture.state(), {"pid": 412, "last_exit": 42})

    def test_state_parser_allowlists_startup_fields_and_reason_values(self):
        fixture = q.Fixture(Path("/unused"), q.PREFIX + "test")
        output = ("\tstate = exited\n\tlast exit code = 2\n\truns = 1\n"
                  "\tlast spawn error = 13\n\treason = PRIVATE_ENV_VALUE\n"
                  "\tenvironment = { PRIVATE_VALUE = withheld }\n")
        with patch.object(q.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output, "")):
            evidence = fixture.state()
        self.assertEqual(evidence, {"pid": None, "last_exit": 2, "state": "exited", "runs": 1,
                                    "spawn_errno": 13, "reason": "unrecognized"})
        self.assertNotIn("PRIVATE", json.dumps(evidence))

    def test_startup_timeout_retains_last_exit_without_claiming_never_exec(self):
        fixture = q.Fixture(Path("/unused/gdrive-maintenance-synthetic"), q.PREFIX + "test")
        moment = [0]
        fixture.clock = lambda: moment[0]
        fixture.sleep = lambda seconds: moment.__setitem__(0, moment[0] + seconds)
        failed = {"pid": None, "last_exit": 2, "state": "exited", "runs": 1}
        fixture.state = Mock(side_effect=[None, *([failed] * 20)])
        fixture.launchctl = Mock()
        fixture.events = Mock(return_value=[])
        with self.assertRaisesRegex(q.QualificationError, "fresh heartbeat"):
            fixture.qualify()
        with patch.object(q.Path, "is_file", return_value=False):
            evidence = fixture.startup_diagnostics()
        self.assertEqual(evidence["last_observed_state"], failed)
        self.assertEqual(evidence["event_count"], 0)
        self.assertEqual(evidence["child_startup_evidence"], "unavailable")
        self.assertEqual(evidence["phase"], "initial-start")
        fixture.launchctl.assert_called_once_with("bootstrap", fixture.domain, str(fixture.directory / "job.plist"))

    def test_non_darwin_and_unattended_refuse_before_mkdtemp_or_launchctl(self):
        for system, attended in (("Linux", True), ("Darwin", False)):
            with self.subTest(system=system, attended=attended):
                args = ["--scratch-parent", "/unused"] + (["--attended"] if attended else [])
                with patch.object(q.platform, "system", return_value=system), patch.object(q.tempfile, "mkdtemp") as create, patch.object(q.subprocess, "run") as run:
                    with self.assertRaises(q.QualificationError):
                        q.main(args)
                create.assert_not_called()
                run.assert_not_called()

    def test_root_refuses(self):
        with patch.object(q.platform, "system", return_value="Darwin"), patch.object(q.os, "getuid", return_value=0):
            with self.assertRaisesRegex(q.QualificationError, "never root"):
                q.main(["--attended", "--scratch-parent", "/unused"])


class PrivateFileTests(unittest.TestCase):
    def setUp(self):
        # Tests create only tiny ephemeral data under the source/runfiles cwd,
        # not /tmp. No real mount paths, services, credentials, or photo bytes.
        self.temporary = tempfile.TemporaryDirectory(prefix="gdrive-maintenance-tests-", dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name).resolve()

    def test_private_parent_and_exact_file_cleanup(self):
        self.assertEqual(q.private_parent(str(self.parent)), self.parent)
        directory = Path(tempfile.mkdtemp(prefix="gdrive-maintenance-", dir=self.parent))
        for name in q.FIXTURE_FILES:
            (directory / name).write_text("fixture")
        q.remove_fixture_files(directory)
        self.assertFalse(directory.exists())
        self.assertTrue(self.parent.exists())

    def test_parent_permissions_and_symlinks_refuse(self):
        self.parent.chmod(0o755)
        with self.assertRaisesRegex(q.QualificationError, "0700"):
            q.private_parent(str(self.parent))
        self.parent.chmod(0o700)
        link = self.parent / "link"
        link.symlink_to(self.parent, target_is_directory=True)
        with self.assertRaisesRegex(q.QualificationError, "canonical"):
            q.private_parent(str(link))

    def test_tmp_and_relative_paths_refuse(self):
        with self.assertRaises(q.QualificationError):
            q.private_parent("relative")
        for directory in ("/tmp", "/private/tmp"):
            with self.subTest(directory=directory), patch.object(q.Path, "resolve", return_value=Path(directory)):
                with self.assertRaisesRegex(q.QualificationError, "outside /tmp"):
                    q.private_parent(directory)

    def test_unknown_file_is_preserved_without_partial_cleanup(self):
        directory = Path(tempfile.mkdtemp(prefix="gdrive-maintenance-", dir=self.parent))
        (directory / "job.plist").write_text("fixture")
        (directory / "other-work").write_text("preserve")
        with self.assertRaisesRegex(q.QualificationError, "unexpected fixture artifact"):
            q.remove_fixture_files(directory)
        self.assertEqual((directory / "other-work").read_text(), "preserve")
        self.assertTrue((directory / "job.plist").exists())

    def test_symlink_artifact_is_not_followed(self):
        directory = Path(tempfile.mkdtemp(prefix="gdrive-maintenance-", dir=self.parent))
        target = self.parent / "keep"
        target.write_text("preserve")
        (directory / "job.plist").symlink_to(target)
        with self.assertRaisesRegex(q.QualificationError, "unexpected fixture artifact"):
            q.remove_fixture_files(directory)
        self.assertEqual(target.read_text(), "preserve")

    def worker_fixture(self):
        directory = Path(tempfile.mkdtemp(prefix="gdrive-maintenance-", dir=self.parent))
        (directory / "lease.json").write_text(json.dumps({"expires": time.monotonic() + 20}))
        return directory

    def test_worker_runs_in_ordinary_subprocess_and_writes_real_heartbeat(self):
        directory = self.worker_fixture()
        if q.platform.system() == "Darwin":
            # Exercise the exact public child argv on its supported platform.
            command = [sys.executable, str(SOURCE.resolve()), "--fixture-child", str(directory)]
        else:
            # Linux CI executes the same real worker body, not fake heartbeat,
            # PID, clock or filesystem observations. The CLI Darwin guard is
            # tested independently and is never relaxed in production.
            script = ("import importlib.util,sys; "
                      "s=importlib.util.spec_from_file_location('worker',sys.argv[1]); "
                      "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                      "sys.exit(m._worker(sys.argv[2]))")
            command = [sys.executable, "-c", script, str(SOURCE.resolve()), str(directory)]
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, env=environment) as process:
            (directory / "command").write_text(f"exit-42:{process.pid}")
            stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 42, stderr)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")
        fixture = q.Fixture(directory, q.PREFIX + "test")
        events = fixture.events()
        self.assertEqual([row["event"] for row in events], ["child-startup", "start", "exit"])
        self.assertTrue(all(row["pid"] == process.pid for row in events))
        self.assertTrue(fixture.heartbeat(process.pid))
        self.assertEqual(events[-1]["code"], 42)
        self.assertEqual((directory / "events.jsonl").stat().st_mode & 0o777, 0o600)
        self.assertEqual((directory / "heartbeat.json").stat().st_mode & 0o777, 0o600)

    def test_child_failure_is_structured_without_exception_text(self):
        directory = self.worker_fixture()
        (directory / "lease.json").write_text("PRIVATE_MALFORMED_LEASE_VALUE")
        with patch.object(q.platform, "system", return_value="Darwin"), patch.object(q.sys, "stderr"):
            self.assertEqual(q.child_entry(str(directory)), 2)
        fixture = q.Fixture(directory, q.PREFIX + "test")
        events = fixture.events()
        self.assertEqual([row["event"] for row in events], ["child-startup", "startup-error"])
        evidence = fixture.startup_diagnostics()
        self.assertEqual(evidence["child_startup_evidence"], "observed")
        self.assertEqual(evidence["startup_error"], {"category": "invalid-input"})
        self.assertNotIn("PRIVATE", json.dumps(events) + json.dumps(evidence))
        self.assertFalse(evidence["heartbeat_present"])

    def test_child_platform_refusal_never_enters_worker_or_writes_events(self):
        directory = self.worker_fixture()
        with patch.object(q.platform, "system", return_value="Linux"), \
                patch.object(q, "_worker") as worker, patch.object(q.sys, "stderr"):
            self.assertEqual(q.child_entry(str(directory)), 2)
        worker.assert_not_called()
        self.assertFalse((directory / "events.jsonl").exists())

    def test_event_observation_is_bounded_and_refuses_symlinks(self):
        directory = self.worker_fixture()
        event_path = directory / "events.jsonl"
        event_path.write_bytes(b"x" * (q.MAX_EVENTS_BYTES + 1))
        fixture = q.Fixture(directory, q.PREFIX + "test")
        with self.assertRaisesRegex(q.QualificationError, "observation bound"):
            fixture.events()
        evidence = fixture.startup_diagnostics()
        self.assertFalse(evidence["events_available"])
        event_path.unlink()
        event_path.symlink_to(directory / "lease.json")
        with self.assertRaises(q.QualificationError):
            fixture.events()

    def test_startup_error_category_is_allowlisted(self):
        directory = self.worker_fixture()
        (directory / "events.jsonl").write_text(json.dumps({"event": "startup-error", "pid": 1,
                                                          "category": "PRIVATE_VALUE", "errno": 13}) + "\n")
        fixture = q.Fixture(directory, q.PREFIX + "test")
        evidence = fixture.startup_diagnostics()
        self.assertEqual(evidence["startup_error"], {"category": "unrecognized", "errno": 13})
        self.assertNotIn("PRIVATE", json.dumps(evidence))

    def test_interpreter_stderr_retains_bounded_categories_without_raw_values(self):
        directory = self.worker_fixture()
        path = directory / "startup.err"
        path.write_bytes(b"PRIVATE_TOKEN Permission denied\n" + b"x" * 20000)
        fixture = q.Fixture(directory, q.PREFIX + "test")
        evidence = fixture.startup_diagnostics()["interpreter_stderr"]
        self.assertEqual(evidence["categories"], ["permission-denied"])
        self.assertEqual(evidence["bytes_observed"], 16385)
        self.assertTrue(evidence["truncated"])
        self.assertNotIn("PRIVATE_TOKEN", json.dumps(evidence))
        path.unlink()
        path.symlink_to(directory / "lease.json")
        self.assertEqual(fixture.startup_diagnostics()["interpreter_stderr"], {"available": False})
        path.unlink()
        os.mkfifo(path)
        self.assertEqual(fixture.startup_diagnostics()["interpreter_stderr"], {"available": False})


if __name__ == "__main__":
    unittest.main()
