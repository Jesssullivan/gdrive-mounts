"""Offline tests: all launchd/process observations are mocked, including cleanup."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
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


if __name__ == "__main__":
    unittest.main()
