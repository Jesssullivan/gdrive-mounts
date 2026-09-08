#!/usr/bin/env python3
"""Production-adapter contracts using synthetic subprocess responses only.

No settings, credentials, mount, Drive object, or photo corpus is opened. Local
filesystem rename success cannot establish Apple NFS exclusive-rename support.
"""
import contextlib
import errno
import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest import mock

import gdrive_photos as photos


def object_row(identifier="file-one", name="a.HEIC", *, parent="folder-root",
               mime="image/heic", size=3):
    return {
        "id": identifier, "name": name, "parent": parent,
        "path": name, "mimeType": mime, "size": str(size),
        "md5Checksum": hashlib.md5(b"abc").hexdigest(),
        "sha256Checksum": None, "modifiedTime": "2026-09-09T00:00:00Z",
    }


class RemoteAdapterTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(photos.subprocess, "run")
        self.process = patcher.start()
        self.addCleanup(patcher.stop)
        self.process.side_effect = AssertionError("unconfigured synthetic subprocess response")
        self.remote = photos.RcloneRemote(
            "/synthetic/unread-rendered.conf",
            SimpleNamespace(root=Path("/synthetic/private-state")),
            "/synthetic/rc.sock", binary="synthetic-rclone",
        )
        self.root = object_row("folder-root", photos.FOLDER, mime=photos.FOLDER_MIME, size=0)
        self.row = object_row()

    def respond(self, *values):
        self.process.side_effect = [
            subprocess.CompletedProcess([], 0, json.dumps(value).encode(), b"")
            for value in values
        ]

    def command(self, index=-1):
        return self.process.call_args_list[index].args[0]

    def test_run_uses_argv_serial_limits_and_external_deadline(self):
        self.respond([])
        self.remote.run("backend", "query", "gdrive-sulliwood:", "name = 'a; $(false)'")
        command = self.command()
        self.assertEqual(command[:5], ["synthetic-rclone", "backend", "query",
                                     "gdrive-sulliwood:", "name = 'a; $(false)'"])
        for flag, expected in {
            "--config": "/synthetic/unread-rendered.conf", "--log-level": "ERROR",
            "--retries": "1", "--low-level-retries": "1", "--timeout": "30s",
            "--contimeout": "15s", "--transfers": "1", "--checkers": "1",
            "--multi-thread-streams": "0",
        }.items():
            self.assertEqual(command[command.index(flag) + 1], expected)
        self.assertEqual(self.process.call_args.kwargs,
                         {"capture_output": True, "timeout": 120, "check": False})

    def test_failed_command_does_not_echo_diagnostics_or_arguments(self):
        marker = "SYNTHETIC-PRIVATE-DIAGNOSTIC"
        self.process.side_effect = [subprocess.CompletedProcess([], 19, marker.encode(), marker.encode())]
        with self.assertRaises(photos.Refusal) as caught:
            self.remote.run("backend", "query", marker)
        self.assertEqual(str(caught.exception), "rclone backend failed with exit 19")
        self.assertNotIn(marker, str(caught.exception))

    def test_timeout_does_not_echo_partial_output_or_command(self):
        marker = "SYNTHETIC-PRIVATE-DIAGNOSTIC"
        self.process.side_effect = subprocess.TimeoutExpired(
            [marker], 120, output=marker.encode(), stderr=marker.encode())
        with self.assertRaises(photos.Refusal) as caught:
            self.remote.run("backend", "query", marker)
        self.assertEqual(str(caught.exception), "rclone exceeded its operation deadline")
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_children_escapes_query_literals_without_shell_interpretation(self):
        self.respond([self.row])
        self.assertEqual(self.remote.children("a'b\\c"), [self.row])
        self.assertEqual(self.command()[4], "'a\\'b\\\\c' in parents and trashed = false")

    def test_query_rejects_non_list_response(self):
        for value in ({"files": [self.row]}, None, "unexpected"):
            with self.subTest(value=value):
                self.respond(value)
                with self.assertRaisesRegex(photos.Refusal, "unexpected Drive query"):
                    self.remote.query("synthetic query")

    def test_root_query_is_scoped_and_ambiguity_fails_closed(self):
        self.respond([self.root])
        self.assertEqual(self.remote.root_folder(), "folder-root")
        query = self.command()[4]
        self.assertIn("'root' in parents", query)
        self.assertIn("name = 'GFTB Stuff'", query)
        self.assertIn("mimeType = 'application/vnd.google-apps.folder'", query)
        self.assertIn("trashed = false", query)
        for rows in ([], [self.root, {**self.root, "id": "another-root"}]):
            with self.subTest(rows=rows):
                self.respond(rows)
                with self.assertRaisesRegex(photos.Refusal, "missing or ambiguous"):
                    self.remote.root_folder()

    def test_inventory_preserves_duplicate_names_and_their_parent_ids(self):
        folder = object_row("nested-folder", "nested", mime=photos.FOLDER_MIME, size=0)
        duplicate = {**self.row, "id": "file-two"}
        child = object_row("child-file", "child.jpg", parent="nested-folder")
        self.respond([duplicate, folder, self.row], [child])
        rows = self.remote.inventory("folder-root")
        duplicates = [r for r in rows if r["path"] == "a.HEIC"]
        self.assertEqual([r["id"] for r in duplicates], ["file-one", "file-two"])
        self.assertTrue(all(r["parent"] == "folder-root" for r in duplicates))
        nested = next(r for r in rows if r["id"] == "child-file")
        self.assertEqual((nested["path"], nested["parent"]), ("nested/child.jpg", "nested-folder"))
        self.assertEqual(self.command()[4], "'nested-folder' in parents and trashed = false")

    def test_inventory_rejects_names_that_escape_the_filesystem_path(self):
        for name in ("../outside", "embedded/slash", "/absolute", "..", "back\\slash"):
            with self.subTest(name=name):
                self.respond([{**self.row, "name": name}])
                with self.assertRaises(photos.Refusal):
                    self.remote.inventory("folder-root")

    def test_inventory_count_and_cycle_limits_stop_further_queries(self):
        self.respond([self.row, {**self.row, "id": "second"}])
        with mock.patch.object(photos, "MAX_FILES", 1):
            with self.assertRaisesRegex(photos.Refusal, "file budget"):
                self.remote.inventory("folder-root")
        self.process.reset_mock()
        self.respond([object_row("folder-root", "cycle", mime=photos.FOLDER_MIME, size=0)])
        with self.assertRaisesRegex(photos.Refusal, "cyclic"):
            self.remote.inventory("folder-root")
        self.assertEqual(self.process.call_count, 1)

    def test_inventory_depth_is_bounded(self):
        self.respond(*[[object_row(f"folder-{i + 1}", f"nested-{i}",
                                  mime=photos.FOLDER_MIME, size=0)] for i in range(17)])
        with self.assertRaisesRegex(photos.Refusal, "depth limit"):
            self.remote.inventory("folder-root")
        self.assertEqual(self.process.call_count, 17)

    def test_namespace_rejects_shortcuts_from_real_inventory_adapter(self):
        shortcut = object_row("shortcut-id", "photos", mime="application/vnd.google-apps.shortcut")
        self.respond([self.root], [shortcut])
        plan = {"drive_root_id": "folder-root", "remote_inventory": [], "entries": [], "retirement": []}
        with self.assertRaisesRegex(photos.Refusal, "shortcut"):
            photos.namespace(plan, self.remote)

    def test_current_selects_exact_id_among_duplicate_names(self):
        other = {**self.row, "id": "other-id", "size": "999"}
        self.respond([other, self.row])
        self.assertEqual(self.remote.current(self.row), self.row)
        self.assertEqual(self.command()[4], "'folder-root' in parents and trashed = false")

    def test_current_rejects_disappearance_and_changed_fingerprints(self):
        variants = [[], [{**self.row, "id": "different-id"}]]
        variants.extend([{**self.row, field: "changed"}]
                        for field in ("name", "size", "md5Checksum", "modifiedTime"))
        for rows in variants:
            with self.subTest(rows=rows):
                self.respond(rows)
                with self.assertRaises(photos.Refusal):
                    self.remote.current(self.row)

    def test_download_uses_copyid_and_checks_before_and_after(self):
        self.respond([self.row], None, [self.row])
        self.remote.download(self.row, Path("/synthetic/download-target"))
        self.assertEqual(self.command(1)[:7], ["synthetic-rclone", "backend", "copyid",
                                             "gdrive-sulliwood:", "file-one",
                                             "/synthetic/download-target", "--immutable"])
        self.assertEqual(self.process.call_count, 3)

    def test_download_refuses_before_copy_and_detects_post_copy_change(self):
        self.respond([])
        with self.assertRaises(photos.Refusal):
            self.remote.download(self.row, "/synthetic/download-target")
        self.assertEqual(self.process.call_count, 1)
        self.process.reset_mock()
        self.respond([self.row], None, [{**self.row, "size": "999"}])
        with self.assertRaisesRegex(photos.Refusal, "fingerprint"):
            self.remote.download(self.row, "/synthetic/download-target")
        self.assertEqual(self.process.call_count, 3)

    def test_sha256_requires_download_even_with_an_advertised_hash(self):
        row = {**self.row, "sha256Checksum": "0" * 64}
        measured = hashlib.sha256(b"synthetic independent readback").hexdigest()
        self.respond([row], [row], None, [row])
        with mock.patch.object(photos.tempfile, "TemporaryDirectory") as temporary:
            temporary.return_value.__enter__.return_value = "/synthetic/readback"
            with mock.patch.object(photos, "hash_file", return_value={"sha256": measured}) as hasher:
                self.assertEqual(self.remote.sha256(row), measured)
                hasher.assert_called_once_with(Path("/synthetic/readback/object"))
        self.assertEqual(self.command(2)[2:5], ["copyid", "gdrive-sulliwood:", "file-one"])

    def test_remote_byte_budget_refuses_before_download(self):
        oversized = {**self.row, "size": "11"}
        self.respond([self.root], [oversized])
        with mock.patch.object(photos, "local_inventory", return_value={"originals/a.HEIC": {}}):
            with mock.patch.object(photos, "MAX_BYTES", 10):
                with mock.patch.object(self.remote, "sha256") as download:
                    with self.assertRaisesRegex(photos.Refusal, "byte budget"):
                        photos.make_plan("/synthetic/source", "/synthetic/mount", self.remote)
                    download.assert_not_called()

    def test_upload_queue_requires_all_fields_and_zero_errors(self):
        good = {"uploadsQueued": 0, "uploadsInProgress": 0,
                "erroredFiles": 0, "outOfSpace": False}
        values = [None, [], {}, {"diskCache": None}, {"diskCache": []}, {"diskCache": {}}]
        for field in good:
            values.append({"diskCache": {k: v for k, v in good.items() if k != field}})
        for field in ("uploadsQueued", "uploadsInProgress", "erroredFiles"):
            for invalid in (False, True, None, "0", -1, 0.0, 1):
                values.append({"diskCache": {**good, field: invalid}})
        for invalid in (None, 0, 1, "false", True):
            values.append({"diskCache": {**good, "outOfSpace": invalid}})
        for value in values:
            with self.subTest(value=value):
                self.respond(value)
                with self.assertRaises(photos.Refusal):
                    self.remote.uploads_empty()
        self.respond({"diskCache": good})
        self.remote.uploads_empty()
        self.assertEqual(self.command()[:5], ["synthetic-rclone", "rc", "--unix-socket",
                                            "/synthetic/rc.sock", "vfs/stats"])

    def test_rename_preserves_id_and_refuses_cross_parent_or_collision(self):
        self.respond([self.row], [], None)
        self.remote.rename(self.row, "a-content-hash.HEIC")
        self.assertEqual(self.command()[:7], ["synthetic-rclone", "backend", "moveid",
                                            "gdrive-sulliwood:", "file-one",
                                            "gdrive-sulliwood:GFTB Stuff/a-content-hash.HEIC", "--immutable"])
        self.respond([self.row])
        with self.assertRaisesRegex(photos.Refusal, "sibling"):
            self.remote.rename(self.row, "elsewhere/a.HEIC")
        self.respond([self.row], [{**self.row, "id": "collision", "name": "A-CONTENT-HASH.HEIC"}])
        with self.assertRaisesRegex(photos.Refusal, "already exists"):
            self.remote.rename(self.row, "a-content-hash.HEIC")

    def test_quarantine_refuses_shortcuts_and_duplicate_folder_names(self):
        shortcut = object_row("shortcut", ".gdm-retired", mime="application/vnd.google-apps.shortcut")
        folder = {**shortcut, "mimeType": photos.FOLDER_MIME}
        for rows in ([shortcut], [folder, {**folder, "id": "duplicate"}]):
            with self.subTest(rows=rows):
                self.respond([self.root], rows)
                with self.assertRaisesRegex(photos.Refusal, "shortcut, or ambiguous"):
                    self.remote.quarantine_parent("a" * 64)

    def test_retired_lookup_uses_same_id_not_same_name(self):
        key = "a" * 64
        quarantine = object_row("quarantine", ".gdm-retired", mime=photos.FOLDER_MIME)
        run = object_row("run-folder", key, mime=photos.FOLDER_MIME)
        retired = {**self.row, "name": "file-one-a.HEIC"}
        self.respond([quarantine], [run], [{**retired, "id": "unrelated"}])
        self.assertIsNone(self.remote.retired(self.row, key))
        self.respond([quarantine], [run], [retired])
        found = self.remote.retired(self.row, key)
        self.assertEqual((found["id"], found["parent"]), ("file-one", "run-folder"))
        self.assertEqual(found["path"], f".gdm-retired/{key}/file-one-a.HEIC")

    def test_nested_retirement_stays_below_original_parent_and_preserves_id(self):
        key = "a" * 64
        folder = object_row("event-folder", "event", mime=photos.FOLDER_MIME, size=0)
        original = {**self.row, "parent": "event-folder", "path": "event/a.HEIC"}
        quarantine = object_row("quarantine", ".gdm-retired", parent="event-folder",
                                mime=photos.FOLDER_MIME, size=0)
        run = object_row("run-folder", key, parent="quarantine", mime=photos.FOLDER_MIME, size=0)
        self.respond([original], [self.root], [folder], [original],
                     [], None, [quarantine], [], None, [run], [], None)
        self.remote.retire(original, key)
        destination = f"gdrive-sulliwood:GFTB Stuff/event/.gdm-retired/{key}/file-one-a.HEIC"
        self.assertEqual(self.command()[:7], ["synthetic-rclone", "backend", "moveid",
                                            "gdrive-sulliwood:", "file-one", destination, "--immutable"])
        mkdirs = [call.args[0][2] for call in self.process.call_args_list if call.args[0][1] == "mkdir"]
        self.assertEqual(mkdirs, ["gdrive-sulliwood:GFTB Stuff/event/.gdm-retired",
                                 f"gdrive-sulliwood:GFTB Stuff/event/.gdm-retired/{key}"])
        retired = {**original, "name": "file-one-a.HEIC"}
        self.respond([quarantine], [run], [retired])
        found = self.remote.retired(original, key)
        self.assertEqual((found["id"], found["parent"]), ("file-one", "run-folder"))
        self.assertEqual(found["path"], f"event/.gdm-retired/{key}/file-one-a.HEIC")
        self.assertEqual(self.command(-3)[4], "'event-folder' in parents and trashed = false")

    def test_nested_inventory_rejects_reserved_shortcuts_duplicates_and_case_variants(self):
        folder = object_row("event-folder", "event", mime=photos.FOLDER_MIME, size=0)
        reserved = object_row("quarantine", ".gdm-retired", parent="event-folder",
                              mime=photos.FOLDER_MIME, size=0)
        variants = ([{**reserved, "mimeType": "application/vnd.google-apps.shortcut"}],
                    [reserved, {**reserved, "id": "second-quarantine"}],
                    [{**reserved, "name": ".GDM-RETIRED"}])
        for rows in variants:
            with self.subTest(rows=rows):
                self.respond([folder], rows)
                with self.assertRaises(photos.Refusal):
                    self.remote.inventory("folder-root")
        self.respond([folder], [reserved])
        rows = self.remote.inventory("folder-root")
        self.assertEqual([row["id"] for row in rows], ["event-folder"])

    @contextlib.contextmanager
    def promotion_fixture(self, inject=None):
        """Run orchestration with the real CLI adapter and no filesystem I/O."""
        webp = object_row("webp-folder", "webp", mime=photos.FOLDER_MIME, size=0)
        world = SimpleNamespace(rows={webp["id"]: webp}, bodies={}, events=[], moves=0, stages=0)
        expected = {"sha256": hashlib.sha256(b"abc").hexdigest(), "fingerprint": {}}
        entry = {"key": "synthetic-entry", "kind": "webp", "keep_id": None,
                 "source": {"local": "webp/a.webp", "remote": None},
                 "sha256": expected["sha256"], "path": "webp/a.webp",
                 "recipe": {"operation": "adopt-corrected-webp"}}
        plan = {"entries": [entry], "drive_root_id": "folder-root", "remote_inventory": [webp],
                "retirement": []}

        def event(key, action, **values):
            world.events.append({"event": action, **values})
            if inject:
                inject("event:" + action, world)

        state = SimpleNamespace(root=Path("/synthetic/private-state"),
                                events=lambda key: list(world.events), event=event)

        def stage(source, path, measured):
            world.stages += 1
            row = object_row("staged-id", Path(path).name, parent="webp-folder")
            world.rows[row["id"]] = row
            world.bodies[row["id"]] = b"abc"
            return "staged"

        def run(command, **kwargs):
            if command[1:3] == ["backend", "query"]:
                query = command[4]
                if query.startswith("'root' in parents"):
                    result = [self.root]
                else:
                    parent = query.split("'", 2)[1]
                    result = [r for r in world.rows.values() if r["parent"] == parent]
            elif command[1:3] == ["backend", "moveid"]:
                world.moves += 1
                row = world.rows[command[4]]
                row["name"] = command[5].rsplit("/", 1)[-1]
                if inject:
                    inject("moved", world)
                result = None
            elif command[1] == "rc" and "vfs/stats" in command:
                result = {"diskCache": {"uploadsQueued": 0, "uploadsInProgress": 0,
                                        "erroredFiles": 0, "outOfSpace": False}}
            elif command[1] == "rc" and "vfs/refresh" in command:
                result = {}
            else:
                raise AssertionError("unexpected adapter operation in promotion fixture")
            return subprocess.CompletedProcess(command, 0, json.dumps(result).encode(), b"")

        self.process.side_effect = run
        mount = mock.MagicMock()
        mount.__enter__.return_value = mount
        mount.__exit__.return_value = False
        mount.stage.side_effect = stage
        with mock.patch.object(photos, "materialized", side_effect=lambda *args: contextlib.nullcontext(Path("/synthetic/source"))):
            with mock.patch.object(photos, "hash_file", return_value=expected):
                with mock.patch.object(self.remote, "sha256", side_effect=lambda row: hashlib.sha256(world.bodies[row["id"]]).hexdigest()):
                    yield world, lambda: photos.apply_plan(plan, "a" * 64, state, self.remote, mount,
                                                           validator=lambda path: {"width": 1, "height": 1})

    def test_promotion_checks_a_concurrent_name_before_issuing_moveid(self):
        def concurrent(action, world):
            if action == "event:promotion-started":
                world.rows["other-id"] = object_row("other-id", "a.webp", parent="webp-folder")
                world.bodies["other-id"] = b"unrelated existing bytes"

        with self.promotion_fixture(concurrent) as (world, apply):
            with self.assertRaisesRegex(photos.Refusal, "already exists"):
                apply()
            self.assertEqual(world.moves, 0)
            self.assertEqual(world.bodies["other-id"], b"unrelated existing bytes")
            self.assertTrue(world.rows["staged-id"]["name"].startswith(".gdm-stage-"))
            self.assertFalse(any(e["event"] == "promoted" for e in world.events))

    def test_promotion_rejects_post_move_duplicate_ids_without_claiming_success(self):
        def concurrent(action, world):
            if action == "moved":
                world.rows["other-id"] = object_row("other-id", "a.webp", parent="webp-folder")
                world.bodies["other-id"] = b"unrelated existing bytes"

        with self.promotion_fixture(concurrent) as (world, apply):
            with self.assertRaisesRegex(photos.Refusal, "promotion identity"):
                apply()
            self.assertEqual(world.moves, 1)
            self.assertEqual(world.bodies["other-id"], b"unrelated existing bytes")
            self.assertEqual(world.bodies["staged-id"], b"abc")
            self.assertFalse(any(e["event"] == "promoted" for e in world.events))

    def test_promotion_rejects_post_move_content_change(self):
        def changed(action, world):
            if action == "moved":
                world.bodies["staged-id"] = b"unexpected content"

        with self.promotion_fixture(changed) as (world, apply):
            with self.assertRaisesRegex(photos.Refusal, "promotion identity"):
                apply()
            self.assertFalse(any(e["event"] == "promoted" for e in world.events))

    def test_interrupted_promotion_reconciles_same_id_without_a_second_move(self):
        def interrupted(action, world):
            if action == "moved":
                raise subprocess.TimeoutExpired(["synthetic moveid"], 120)

        with self.promotion_fixture(interrupted) as (world, apply):
            with self.assertRaisesRegex(photos.Refusal, "deadline"):
                apply()
            self.assertEqual(world.moves, 1)
            self.assertTrue(any(e["event"] == "stage-verified" for e in world.events))
            self.assertFalse(any(e["event"] == "promoted" for e in world.events))
            self.assertEqual(apply(), 1)
            self.assertEqual((world.moves, world.stages), (1, 1))
            receipt = next(e for e in world.events if e["event"] == "promoted")
            self.assertEqual(receipt["id"], "staged-id")
            self.assertTrue(receipt["reconciled"])


class NFSRenameContractTests(unittest.TestCase):
    def test_darwin_enotsup_is_not_retried_with_a_clobbering_rename(self):
        # Apple NFS lacks RENAME_EXCL. This tests refusal, never filesystem support.
        exclusive = mock.Mock(return_value=-1)
        libc = SimpleNamespace(renameatx_np=exclusive)
        with mock.patch.object(photos.sys, "platform", "darwin"):
            with mock.patch.object(photos.ctypes, "CDLL", return_value=libc):
                with mock.patch.object(photos.ctypes, "get_errno", return_value=errno.ENOTSUP):
                    with mock.patch.object(photos.os, "rename") as ordinary:
                        with self.assertRaises(OSError) as caught:
                            photos.rename_noreplace("staged", "final", 7, 8)
                        self.assertEqual(caught.exception.errno, errno.ENOTSUP)
                        ordinary.assert_not_called()
        exclusive.assert_called_once_with(7, b"staged", 8, b"final", 4)


if __name__ == "__main__":
    unittest.main()
