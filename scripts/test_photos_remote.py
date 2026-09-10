#!/usr/bin/env python3
"""Production-adapter contracts using synthetic subprocess responses only.

No settings, credentials, mount, Drive object, or photo corpus is opened. Local
filesystem rename success cannot establish Apple NFS exclusive-rename support.
"""
import contextlib
import errno
import hashlib
import io
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
        self.assertEqual(str(caught.exception), "rclone backend query failed with exit 19")
        self.assertNotIn(marker, str(caught.exception))

    def test_failed_command_labels_only_allowlisted_operations(self):
        marker = "SYNTHETIC-PRIVATE-DIAGNOSTIC"
        for args, label in (
            (("backend", "copyid", marker, marker, marker), "backend copyid"),
            (("backend", "moveid", marker, marker, marker), "backend moveid"),
            (("backend", marker, marker), "backend"),
            (("rc", marker), "rc"),
            (("mkdir", marker), "mkdir"),
            ((marker,), "operation"),
        ):
            with self.subTest(label=label):
                self.process.side_effect = [subprocess.CompletedProcess([], 5, marker.encode(), marker.encode())]
                with self.assertRaises(photos.Refusal) as caught:
                    self.remote.run(*args)
                self.assertEqual(str(caught.exception), f"rclone {label} failed with exit 5")
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

    def test_empty_query_children_accept_null_or_empty_list(self):
        for value in (None, []):
            with self.subTest(value=value):
                self.respond(value)
                self.assertEqual(self.remote.children("empty-parent"), [])

    def test_query_rejects_other_non_list_responses(self):
        for value in ({"files": [self.row]}, "unexpected", 0, False):
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
        for rows in (None, [], [self.root, {**self.root, "id": "another-root"}]):
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
        self.respond([row], None, [row])
        with mock.patch.object(photos.tempfile, "TemporaryDirectory") as temporary:
            temporary.return_value.__enter__.return_value = "/synthetic/readback"
            with mock.patch.object(photos, "hash_file", return_value={"sha256": measured}) as hasher:
                self.assertEqual(self.remote.sha256(row), measured)
                hasher.assert_called_once_with(Path("/synthetic/readback/object"))
        self.assertEqual(self.process.call_count, 3)
        self.assertEqual(self.command(0)[1:3], ["backend", "query"])
        self.assertEqual(self.command(1)[2:5], ["copyid", "gdrive-sulliwood:", "file-one"])
        self.assertEqual(self.command(2)[1:3], ["backend", "query"])

    def test_sha256_never_hashes_after_failed_fingerprint_check(self):
        for responses, expected_calls in (([[]], 1),
                                          ([[self.row], None, [{**self.row, "size": "999"}]], 3)):
            with self.subTest(expected_calls=expected_calls):
                self.process.reset_mock()
                self.respond(*responses)
                with mock.patch.object(photos.tempfile, "TemporaryDirectory") as temporary:
                    temporary.return_value.__enter__.return_value = "/synthetic/readback"
                    with mock.patch.object(photos, "hash_file") as hasher:
                        with self.assertRaises(photos.Refusal):
                            self.remote.sha256(self.row)
                        hasher.assert_not_called()
                self.assertEqual(self.process.call_count, expected_calls)

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

    def plan_without_photo_io(self, local, rows, hashes):
        """Exercise planning against synthetic inventory, without opening files."""
        validator = mock.Mock(return_value={"width": 2000, "height": 1500})
        with mock.patch.object(photos, "local_inventory", return_value=local), \
                mock.patch.object(photos.Path, "stat", return_value=SimpleNamespace(st_dev=1, st_ino=2)), \
                mock.patch.object(self.remote, "root_folder", return_value="folder-root"), \
                mock.patch.object(self.remote, "inventory", return_value=rows), \
                mock.patch.object(self.remote, "sha256", side_effect=lambda row: hashes[row["id"]]), \
                mock.patch.object(photos, "codec_versions", return_value={"synthetic": "1"}) as codecs:
            plan = photos.make_plan("/synthetic/photos", "/synthetic/mount", self.remote, validator)
        self.process.assert_not_called()
        return plan, validator, codecs

    def test_plan_blocks_remote_corrected_webp_when_local_output_missing(self):
        original_sha, corrected_sha = "1" * 64, "2" * 64
        output = {**object_row("corrected-id", "a.webp", mime="image/webp"), "path": "webp/a.webp"}
        plan, validator, codecs = self.plan_without_photo_io(
            {"originals/a.HEIC": {"sha256": original_sha}}, [self.row, output],
            {self.row["id"]: original_sha, output["id"]: corrected_sha})
        self.assertEqual(len(plan["blockers"]), 1)
        self.assertIn("accepted-output decision", plan["blockers"][0])
        self.assertIn("corrected-id", plan["blockers"][0])
        self.assertIn(corrected_sha, plan["blockers"][0])
        self.assertFalse(any(entry["kind"] == "webp" for entry in plan["entries"]))
        self.assertFalse(any(item["row"]["id"] == output["id"] for item in plan["retirement"]))
        self.assertIn(output, plan["remote_inventory"])
        validator.assert_not_called()
        codecs.assert_not_called()

    def test_plan_preserves_all_ambiguous_remote_derivative_ids(self):
        original_sha = "1" * 64
        outputs = [{**object_row(identifier, "a.webp", mime="image/webp"), "path": "webp/a.webp"}
                   for identifier in ("corrected-one", "corrected-two")]
        hashes = {self.row["id"]: original_sha, "corrected-one": "2" * 64, "corrected-two": "3" * 64}
        plan, _, codecs = self.plan_without_photo_io(
            {"originals/a.HEIC": {"sha256": original_sha}}, [self.row, *outputs], hashes)
        self.assertEqual(len(plan["blockers"]), 1)
        for output in outputs:
            self.assertIn(output["id"], plan["blockers"][0])
            self.assertIn(hashes[output["id"]], plan["blockers"][0])
        self.assertEqual(plan["retirement"], [])
        self.assertFalse(any(entry["kind"] == "webp" for entry in plan["entries"]))
        codecs.assert_not_called()

    def test_plan_checks_local_and_remote_original_aliases_for_remote_webp(self):
        original_sha, corrected_sha = "1" * 64, "2" * 64
        original = {**self.row, "path": "event/b.HEIC", "name": "b.HEIC"}
        for path in ("webp/event/a.webp", "webp/event/b.webp", "webp/event/A.WEBP"):
            with self.subTest(path=path):
                output = {**object_row("corrected-id", Path(path).name, mime="image/webp"), "path": path}
                plan, _, codecs = self.plan_without_photo_io(
                    {"originals/event/a.HEIC": {"sha256": original_sha}}, [original, output],
                    {original["id"]: original_sha, output["id"]: corrected_sha})
                self.assertTrue(plan["blockers"])
                self.assertEqual(plan["retirement"], [])
                self.assertFalse(any(entry["kind"] == "webp" for entry in plan["entries"]))
                codecs.assert_not_called()

    def test_plan_checks_remote_webp_before_original_collision_rename(self):
        first_sha, second_sha = "1" * 64, "2" * 64
        other = {**self.row, "id": "second-original"}
        output = {**object_row("corrected-id", "a.webp", mime="image/webp"), "path": "webp/a.webp"}
        plan, _, codecs = self.plan_without_photo_io(
            {"originals/a.HEIC": {"sha256": second_sha}}, [self.row, other, output],
            {self.row["id"]: first_sha, other["id"]: second_sha, output["id"]: "3" * 64})
        self.assertTrue(any(entry["rename_from"] for entry in plan["entries"] if entry["kind"] == "original"))
        self.assertEqual(len(plan["blockers"]), 2)
        self.assertEqual(plan["retirement"], [])
        self.assertFalse(any(entry["kind"] == "webp" for entry in plan["entries"]))
        codecs.assert_not_called()

    def test_plan_still_adopts_accepted_local_webp_bytes(self):
        original_sha, corrected_sha = "1" * 64, "2" * 64
        output = {**object_row("corrected-id", "a.webp", mime="image/webp"), "path": "webp/a.webp"}
        plan, validator, codecs = self.plan_without_photo_io(
            {"originals/a.HEIC": {"sha256": original_sha}, "webp/a.webp": {"sha256": corrected_sha}},
            [self.row, output], {self.row["id"]: original_sha, output["id"]: corrected_sha})
        entry = next(entry for entry in plan["entries"] if entry["kind"] == "webp")
        self.assertEqual(plan["blockers"], [])
        self.assertEqual(entry["keep_id"], "corrected-id")
        self.assertEqual(entry["recipe"]["operation"], "adopt-corrected-webp")
        validator.assert_called_once_with(Path("/synthetic/photos/webp/a.webp"))
        codecs.assert_not_called()

    def test_plan_reuses_accepted_remote_webp_at_proven_original_alias(self):
        original_sha, corrected_sha = "1" * 64, "2" * 64
        original_alias = {**object_row("original-alias", "b.HEIC"), "path": "b.HEIC"}
        output = {**object_row("accepted-alias", "b.webp", mime="image/webp"), "path": "webp/b.webp"}
        plan, _, codecs = self.plan_without_photo_io(
            {"originals/a.HEIC": {"sha256": original_sha}, "webp/a.webp": {"sha256": corrected_sha}},
            [self.row, original_alias, output],
            {self.row["id"]: original_sha, original_alias["id"]: original_sha, output["id"]: corrected_sha})
        entry = next(entry for entry in plan["entries"] if entry["kind"] == "webp")
        self.assertEqual(plan["blockers"], [])
        self.assertEqual((entry["path"], entry["keep_id"]), ("webp/b.webp", "accepted-alias"))
        self.assertEqual(entry["source"]["local"], "webp/a.webp")
        self.assertFalse(any(item["row"]["id"] == "accepted-alias" for item in plan["retirement"]))
        codecs.assert_not_called()

    def test_plan_prefers_adopted_path_and_retires_equal_byte_alias_derivatives(self):
        original_sha, corrected_sha = "1" * 64, "2" * 64
        outputs = [{**object_row(identifier, Path(path).name, mime="image/webp"), "path": path}
                   for identifier, path in (("z-wanted", "webp/a.webp"), ("a-alias", "webp/b.webp"))]
        plan, _, _ = self.plan_without_photo_io(
            {"originals/a.HEIC": {"sha256": original_sha}, "originals/b.HEIC": {"sha256": original_sha},
             "webp/a.webp": {"sha256": corrected_sha}},
            [self.row, *outputs], {self.row["id"]: original_sha, **{row["id"]: corrected_sha for row in outputs}})
        entry = next(entry for entry in plan["entries"] if entry["kind"] == "webp")
        self.assertEqual((entry["path"], entry["keep_id"]), ("webp/a.webp", "z-wanted"))
        self.assertEqual([(item["row"]["id"], item["reason"], item["sha256"], item["canonical_key"])
                          for item in plan["retirement"]],
                         [("a-alias", "verified-webp-copy", corrected_sha, entry["key"])])

    def test_plan_alias_webp_fallback_is_deterministic_by_existing_path_and_id(self):
        original_sha, corrected_sha = "1" * 64, "2" * 64
        outputs = [{**object_row(identifier, Path(path).name, mime="image/webp"), "path": path}
                   for identifier, path in (("z-at-b", "webp/b.webp"), ("a-at-b", "webp/b.webp"),
                                            ("0-at-c", "webp/c.webp"))]
        local = {f"originals/{name}.HEIC": {"sha256": original_sha} for name in ("a", "b", "c")}
        local["webp/a.webp"] = {"sha256": corrected_sha}
        for rows in ([self.row, *outputs], [*reversed(outputs), self.row]):
            with self.subTest(order=[row["id"] for row in rows]):
                plan, _, _ = self.plan_without_photo_io(
                    local, rows, {self.row["id"]: original_sha, **{row["id"]: corrected_sha for row in outputs}})
                entry = next(entry for entry in plan["entries"] if entry["kind"] == "webp")
                self.assertEqual((entry["path"], entry["keep_id"]), ("webp/b.webp", "a-at-b"))
                self.assertEqual({item["row"]["id"] for item in plan["retirement"]}, {"z-at-b", "0-at-c"})
                self.assertTrue(all(item["reason"] == "verified-webp-copy" and
                                    item["sha256"] == corrected_sha and item["canonical_key"] == entry["key"]
                                    for item in plan["retirement"]))

    def test_plan_reused_webp_keeps_actual_inventory_path_spelling(self):
        original_sha, corrected_sha = "1" * 64, "2" * 64
        output = {**object_row("accepted-case", "A.WEBP", mime="image/webp"), "path": "webp/A.WEBP"}
        plan, _, _ = self.plan_without_photo_io(
            {"originals/a.HEIC": {"sha256": original_sha}, "webp/a.webp": {"sha256": corrected_sha}},
            [self.row, output], {self.row["id"]: original_sha, output["id"]: corrected_sha})
        entry = next(entry for entry in plan["entries"] if entry["kind"] == "webp")
        self.assertEqual((entry["path"], entry["keep_id"]), ("webp/A.WEBP", "accepted-case"))
        self.assertEqual(plan["retirement"], [])

    def test_plan_preserves_different_byte_alias_and_unmapped_equal_byte_webps(self):
        original_sha, corrected_sha = "1" * 64, "2" * 64
        outputs = [{**object_row(identifier, Path(path).name, mime="image/webp"), "path": path}
                   for identifier, path in (("obsolete-wanted", "webp/a.webp"), ("accepted-b", "webp/b.webp"),
                                            ("different-c", "webp/c.webp"), ("unmapped", "webp/000-unmapped.webp"))]
        local = {f"originals/{name}.HEIC": {"sha256": original_sha} for name in ("a", "b", "c")}
        local["webp/a.webp"] = {"sha256": corrected_sha}
        plan, _, _ = self.plan_without_photo_io(
            local, [self.row, *outputs], {self.row["id"]: original_sha, "obsolete-wanted": "3" * 64,
                                        "accepted-b": corrected_sha, "different-c": "4" * 64,
                                        "unmapped": corrected_sha})
        entry = next(entry for entry in plan["entries"] if entry["kind"] == "webp")
        self.assertEqual((entry["path"], entry["keep_id"]), ("webp/b.webp", "accepted-b"))
        self.assertEqual([(item["row"]["id"], item["reason"], item["canonical_key"])
                          for item in plan["retirement"]],
                         [("obsolete-wanted", "superseded-webp", entry["key"])])
        self.assertTrue(all(output in plan["remote_inventory"] for output in outputs))

    def test_plan_still_encodes_when_no_matching_remote_derivative_exists(self):
        original_sha = "1" * 64
        unrelated = {**object_row("other-webp", "other.webp", mime="image/webp"), "path": "webp/other.webp"}
        plan, validator, codecs = self.plan_without_photo_io(
            {"originals/a.HEIC": {"sha256": original_sha}}, [self.row, unrelated],
            {self.row["id"]: original_sha, unrelated["id"]: "2" * 64})
        entry = next(entry for entry in plan["entries"] if entry["kind"] == "webp")
        self.assertEqual(plan["blockers"], [])
        self.assertEqual(entry["recipe"]["operation"], "encode-original")
        self.assertEqual(plan["retirement"], [])
        validator.assert_not_called()
        codecs.assert_called_once_with()

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


class CLIReportingTests(unittest.TestCase):
    def run_cli(self, command):
        plan = {"source_root": str(photos.SSD_ROOT), "mount_point": "/synthetic/mount",
                "drive_root_id": "canonical-folder-id", "entries": [{"private": "private-photo-name"}],
                "retirement": [], "warnings": [], "unresolved": [{"path": "private-unresolved-name"}],
                "blockers": []}
        state = mock.MagicMock()
        state.save.return_value = "plan-digest"
        state.load.return_value = plan
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            for name, value in {
                "configuration": (Path("/synthetic/mount"), Path("/synthetic/runtime"),
                                  Path("/synthetic/private"), "/synthetic/rc.sock"),
                "State": state, "RcloneRemote": mock.Mock(), "MountedIO": mock.Mock(),
                "make_plan": plan, "apply_plan": 1, "verified_location": 1,
                "retire_plan": {"quarantine": "/private/quarantine", "local_files": 1,
                                "permanent_deletion": False, "space_reclaimed": False},
            }.items():
                stack.enter_context(mock.patch.object(photos, name, return_value=value))
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(contextlib.redirect_stderr(errors))
            args = [command] if command == "plan" else [command, "plan-digest"]
            if command == "retire":
                args += ["--approve-digest", "plan-digest"]
            self.assertEqual(photos.main(args), 0, errors.getvalue())
        return json.loads(output.getvalue()), output.getvalue()

    def test_phase_reports_location_without_leaking_private_inventory(self):
        for command in ("plan", "apply", "verify", "retire"):
            with self.subTest(command=command):
                result, raw = self.run_cli(command)
                self.assertEqual(result["phase"], command)
                self.assertEqual(result["plan"], "plan-digest")
                self.assertEqual(result["canonical_location"], {
                    "absolute_path": "/synthetic/mount/GFTB Stuff",
                    "drive_folder_id": "canonical-folder-id",
                    "mounted_directory_verified": command == "verify",
                    "mounted_payloads_verified": False,
                    "drive_contents_verified": command in {"verify", "retire"},
                })
                self.assertEqual(result["unresolved_files"], 1)
                self.assertEqual(result["blocker_count"], 0)
                for private in ("private-photo-name", "private-unresolved-name", "/private/quarantine"):
                    self.assertNotIn(private, raw)
                self.assertNotIn("deduplication_complete", result)

    def location_fixture(self):
        mount = mock.MagicMock()
        mount.parent.return_value.__enter__.return_value = (71, ".gdm-location-check")
        return mount

    def test_verified_location_checks_directory_before_and_after_drive_readback(self):
        mount = self.location_fixture()
        identity = SimpleNamespace(st_dev=11, st_ino=23)
        with mock.patch.object(photos.os, "fstat", return_value=identity):
            with mock.patch.object(photos, "verify_plan", return_value={"original": {}, "webp": {}}) as verify:
                self.assertEqual(photos.verified_location({}, "digest", "state", "remote", mount), 2)
        verify.assert_called_once_with({}, "digest", "state", "remote")
        self.assertEqual(mount.parent.call_args_list,
                         [mock.call(".gdm-location-check"), mock.call(".gdm-location-check")])
        self.assertEqual(mount.check.call_count, 2)

    def test_verified_location_refuses_directory_replacement(self):
        mount = self.location_fixture()
        identities = [SimpleNamespace(st_dev=11, st_ino=23), SimpleNamespace(st_dev=11, st_ino=24)]
        with mock.patch.object(photos.os, "fstat", side_effect=identities):
            with mock.patch.object(photos, "verify_plan", return_value={}):
                with self.assertRaisesRegex(photos.Refusal, "directory changed"):
                    photos.verified_location({}, "digest", "state", "remote", mount)

    def test_verified_location_refuses_absent_mount_before_drive_queries(self):
        mount = self.location_fixture()
        mount.__enter__.side_effect = photos.Refusal("mount absent")
        with mock.patch.object(photos, "verify_plan") as verify:
            with self.assertRaisesRegex(photos.Refusal, "mount absent"):
                photos.verified_location({}, "digest", "state", "remote", mount)
        verify.assert_not_called()

    def test_verified_location_propagates_failed_drive_evidence(self):
        mount = self.location_fixture()
        with mock.patch.object(photos.os, "fstat", return_value=SimpleNamespace(st_dev=11, st_ino=23)):
            with mock.patch.object(photos, "verify_plan", side_effect=photos.Refusal("uploads unknown")):
                with self.assertRaisesRegex(photos.Refusal, "uploads unknown"):
                    photos.verified_location({}, "digest", "state", "remote", mount)


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
