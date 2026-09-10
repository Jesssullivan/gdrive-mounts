#!/usr/bin/env python3
"""Tiny, synthetic, serial tests. Never use a real Drive, SSD corpus or token."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import gdrive_photos as photos


class FakeRemote:
    def __init__(self):
        self.rows = {}
        self.payloads = {}
        self.quarantined = {}
        self.queue = False
        self.moves = []
        self.counter = 0

    def add(self, path, content, identifier=None, mime="application/octet-stream"):
        self.counter += 1
        identifier = identifier or f"object-{self.counter}"
        row = {"id": identifier, "name": Path(path).name, "path": path,
               "parent": "folder", "mimeType": mime, "size": str(len(content)),
               "md5Checksum": hashlib.md5(content).hexdigest(),
               "modifiedTime": "2026-09-09T00:00:00Z", "sha256Checksum": None}
        self.rows[identifier] = row
        self.payloads[identifier] = content
        return row

    def root_folder(self):
        return "folder"

    def inventory(self, _):
        return [dict(row) for row in self.rows.values()]

    def current(self, row):
        now = self.rows.get(row["id"])
        if now != row:
            raise photos.Refusal("remote fingerprint changed")
        return now

    def rename(self, row, destination):
        self.current(row)
        if any(r["path"] == destination for r in self.rows.values()):
            raise photos.Refusal("rename conflict")
        self.rows[row["id"]] = {**row, "path": destination, "name": Path(destination).name}

    def refresh(self, _):
        pass

    def sha256(self, row):
        return hashlib.sha256(self.payloads[row["id"]]).hexdigest()

    def download(self, row, path):
        Path(path).write_bytes(self.payloads[row["id"]])

    def uploads_empty(self):
        if self.queue:
            raise photos.Refusal("uploads queued")

    def retire(self, row, key):
        self.moves.append(row["id"])
        moved = self.rows.pop(row["id"])
        moved = {**moved, "path": f".gdm-retired/{key}/{row['id']}-{row['name']}"}
        self.quarantined[(key, row["id"])] = moved

    def retired(self, row, key):
        return self.quarantined.get((key, row["id"]))


class FakeMount:
    def __init__(self, remote):
        self.remote = remote
        self.writes = 0
        self.live = True

    def __enter__(self):
        if not self.live:
            raise photos.Refusal("mount absent")
        return self

    def __exit__(self, *_):
        pass

    def read_to(self, path, destination, expected_sha):
        found = [r for r in self.remote.rows.values() if r["path"] == path]
        if not self.live or not found or any(self.remote.sha256(r) != expected_sha for r in found):
            raise photos.Refusal("mounted source differs from expected original")
        Path(destination).write_bytes(self.remote.payloads[found[0]["id"]])

    def publish(self, source, path, expected, temporary=None):
        if not self.live:
            raise photos.Refusal("mount detached")
        photos.assert_source(source, expected)
        found = [r for r in self.remote.rows.values() if r["path"] == path]
        if found:
            if len(found) != 1 or self.remote.sha256(found[0]) != expected["sha256"]:
                raise photos.Refusal("destination conflict")
            return "reused"
        self.remote.add(path, Path(source).read_bytes())
        self.writes += 1
        return "published"

    def stage(self, source, path, expected):
        found = [r for r in self.remote.rows.values() if r["path"] == path]
        if len(found) == 1 and self.remote.sha256(found[0]) != expected["sha256"]:
            raise photos.IncompleteStage("preserve partial staging bytes")
        outcome = self.publish(source, path, expected)
        return "staged" if outcome == "published" else "stage-reused"


class LocalMount(photos.MountedIO):
    """Exercise production FD-relative I/O on a disposable local filesystem."""
    def __init__(self, root):
        super().__init__(root)
        self.generation = "test-generation-one"

    def table(self):
        return self.generation

    def daemon_pid(self):
        return 1

    def __enter__(self):
        self.table_line = self.table()
        self.fd = os.open(self.point, os.O_RDONLY | os.O_DIRECTORY)
        self.device = os.fstat(self.fd).st_dev
        self.inode = os.fstat(self.fd).st_ino
        self.pid = self.daemon_pid()
        return self


class MountedAdmissionTests(unittest.TestCase):
    def test_darwin_nfsstat_system_path_and_soft_admission(self):
        point = Path("/synthetic/mount")
        mount = photos.MountedIO(point)
        with mock.patch.object(mount, "table", return_value="synthetic-nfs"), \
                mock.patch.object(photos.subprocess, "run", return_value=mock.Mock(
                    stdout=f"{point}\nsoft,intr\n\n")) as run, \
                mock.patch.object(photos.os, "open", side_effect=RuntimeError("admission passed")) as opened:
            with self.assertRaisesRegex(RuntimeError, "admission passed"):
                mount.__enter__()
            run.assert_called_once_with(["/usr/bin/nfsstat", "-m"], capture_output=True,
                                        text=True, check=True, timeout=10)
            opened.assert_called_once_with(point, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def test_unsafe_nfs_options_refuse_before_open(self):
        point = Path("/synthetic/mount")
        for options in ("hard,intr", "soft,nointr", "soft,intr,not responding"):
            with self.subTest(options=options):
                mount = photos.MountedIO(point)
                with mock.patch.object(mount, "table", return_value="synthetic-nfs"), \
                        mock.patch.object(photos.subprocess, "run", return_value=mock.Mock(
                            stdout=f"{point}\n{options}\n\n")), \
                        mock.patch.object(photos.os, "open") as opened:
                    with self.assertRaisesRegex(photos.Refusal, "soft semantics"):
                        mount.__enter__()
                    opened.assert_not_called()


class PhotosTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gdm-photos-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source"
        (self.source / "originals").mkdir(parents=True)
        (self.source / "webp").mkdir()
        (self.source / "originals/a.HEIC").write_bytes(b"synthetic-original-one")
        (self.source / "webp/a.webp").write_bytes(b"synthetic-corrected-webp")
        self.remote = FakeRemote()
        self.remote.add("a.HEIC", b"synthetic-original-one", "original-a")
        self.state = photos.State(self.root / "state")
        self.mount = FakeMount(self.remote)
        self.validator = lambda _: {"width": 2, "height": 1}

    def plan(self):
        plan = photos.make_plan(self.source, self.root / "mount", self.remote,
                                validator=self.validator)
        return plan, self.state.save(plan)

    def apply(self, plan, key, encoder=None):
        kwargs = {"validator": self.validator}
        if encoder:
            kwargs["encoder"] = encoder
        return photos.apply_plan(plan, key, self.state, self.remote, self.mount, **kwargs)

    def test_private_immutable_manifest_and_digest(self):
        plan, key = self.plan()
        self.assertEqual(self.state.load(key), plan)
        path = self.state.root / f"plan-{key}.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o400)
        self.assertEqual(self.state.root.stat().st_mode & 0o777, 0o700)
        path.chmod(0o600)
        path.write_text("{}\n")
        path.chmod(0o400)
        with self.assertRaisesRegex(photos.Refusal, "digest"):
            self.state.load(key)

    def test_state_rejects_symlinks_and_public_permissions(self):
        public = self.root / "public"
        public.mkdir(mode=0o755)
        with self.assertRaises(photos.Refusal):
            photos.State(public)
        link = self.root / "linked"
        link.symlink_to(self.state.root, target_is_directory=True)
        with self.assertRaises(photos.Refusal):
            photos.State(link)

    def test_source_symlink_refused(self):
        (self.source / "originals/alias.HEIC").symlink_to(self.source / "originals/a.HEIC")
        with self.assertRaises((OSError, photos.Refusal)):
            self.plan()

    def test_duplicate_names_are_grouped_by_bytes_and_id(self):
        self.remote.add("a.HEIC", b"synthetic-original-one", "original-b")
        self.remote.add("a.HEIC", b"different-original", "original-c")
        plan, _ = self.plan()
        originals = [e for e in plan["entries"] if e["kind"] == "original"]
        self.assertEqual(len(originals), 2)
        self.assertEqual(len({e["path"] for e in originals}), 2)
        self.assertEqual({e["keep_id"] for e in originals}, {"original-a", "original-c"})
        self.assertEqual({r["row"]["id"] for r in plan["retirement"]}, {"original-b"})

    def test_same_stem_different_originals_cannot_claim_one_webp(self):
        (self.source / "originals/a.jpg").write_bytes(b"different-photo")
        with self.assertRaisesRegex(photos.Refusal, "same-stem"):
            self.plan()

    def test_multiple_duplicate_payload_groups_block_before_mount_writes(self):
        self.remote.add("a.HEIC", b"different-original", "original-c")
        self.remote.add("a.HEIC", b"different-original", "original-d")
        plan, key = self.plan()
        self.assertTrue(plan["blockers"])
        with self.assertRaises(photos.Refusal):
            self.apply(plan, key)
        self.assertEqual(self.remote.rows["original-c"]["path"], "a.HEIC")
        self.assertEqual(self.mount.writes, 0)

    def test_duplicate_directories_and_shortcuts_are_refused(self):
        self.remote.add("photos", b"", "dir-a", mime=photos.FOLDER_MIME)
        self.remote.add("photos", b"", "dir-b", mime=photos.FOLDER_MIME)
        with self.assertRaisesRegex(photos.Refusal, "directory"):
            self.plan()
        self.remote.rows.pop("dir-a")
        self.remote.rows.pop("dir-b")
        self.remote.add("elsewhere", b"", "shortcut", mime="application/vnd.google-apps.shortcut")
        with self.assertRaisesRegex(photos.Refusal, "shortcut"):
            self.plan()

    def test_existing_conflicting_output_gets_content_suffix(self):
        self.remote.add("webp/a.webp", b"old-unrotated-output", "old-webp")
        plan, key = self.plan()
        output = next(e for e in plan["entries"] if e["kind"] == "webp")
        self.assertNotEqual(output["path"], "webp/a.webp")
        self.assertEqual(plan["retirement"][0]["reason"], "superseded-webp")
        self.apply(plan, key)
        self.assertEqual(self.remote.payloads["old-webp"], b"old-unrotated-output")

    def test_apply_resume_uses_hash_not_mtime(self):
        plan, key = self.plan()
        self.apply(plan, key)
        self.assertEqual(self.mount.writes, 1)
        self.apply(plan, key)
        self.assertEqual(self.mount.writes, 1)
        self.assertEqual(len(photos.verify_plan(plan, key, self.state, self.remote)), 2)

    def test_changed_source_refused_before_publication(self):
        plan, key = self.plan()
        path = self.source / "webp/a.webp"
        before = path.stat()
        path.write_bytes(b"tampered-webp")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaisesRegex(photos.Refusal, "source"):
            self.apply(plan, key)
        self.assertEqual(self.mount.writes, 0)

    def test_queued_uploads_do_not_verify(self):
        plan, key = self.plan()
        self.apply(plan, key)
        self.remote.queue = True
        with self.assertRaises(photos.Refusal):
            photos.verify_plan(plan, key, self.state, self.remote)
        self.assertFalse(any(e["event"] == "verified" for e in self.state.events(key)))

    def test_staging_waits_for_drain_then_promotes_same_id_on_resume(self):
        plan, key = self.plan()
        stage = self.mount.stage
        def queued(source, path, expected):
            result = stage(source, path, expected)
            self.remote.queue = True
            return result
        self.mount.stage = queued
        with self.assertRaises(photos.Refusal):
            self.apply(plan, key)
        staged = next(r for r in self.remote.rows.values() if ".gdm-stage-" in r["path"])
        self.assertFalse(any(r["path"] == "webp/a.webp" for r in self.remote.rows.values()))
        self.mount.stage = stage
        self.remote.queue = False
        self.apply(plan, key)
        self.assertEqual(self.remote.rows[staged["id"]]["path"], "webp/a.webp")
        self.assertEqual(self.mount.writes, 1)

    def test_crash_after_promotion_resumes_without_new_id_or_rewrite(self):
        plan, key = self.plan()
        rename = self.remote.rename
        def interrupted(row, destination):
            rename(row, destination)
            raise photos.Refusal("interrupted after promotion")
        self.remote.rename = interrupted
        with self.assertRaisesRegex(photos.Refusal, "interrupted"):
            self.apply(plan, key)
        self.remote.rename = rename
        promoted = next(r["id"] for r in self.remote.rows.values() if r["path"] == "webp/a.webp")
        self.apply(plan, key)
        self.assertEqual(self.mount.writes, 1)
        self.assertIn(promoted, self.remote.rows)

    def test_concurrent_final_name_preserves_staged_and_other_ids(self):
        plan, key = self.plan()
        rename = self.remote.rename
        def concurrent(row, destination):
            self.remote.add(destination, b"other-writer-bytes", "concurrent-object")
            rename(row, destination)
        self.remote.rename = concurrent
        with self.assertRaises(photos.Refusal):
            self.apply(plan, key)
        self.assertEqual(self.remote.payloads["concurrent-object"], b"other-writer-bytes")
        self.assertTrue(any(".gdm-stage-" in r["path"] for r in self.remote.rows.values()))

    def test_duplicate_staging_name_refuses_promotion_preserving_both_ids(self):
        plan, key = self.plan()
        stage = self.mount.stage
        def duplicate(source, path, expected):
            result = stage(source, path, expected)
            self.remote.add(path, b"other-stage", "duplicate-stage")
            return result
        self.mount.stage = duplicate
        with self.assertRaisesRegex(photos.Refusal, "ambiguous"):
            self.apply(plan, key)
        self.assertEqual(len([r for r in self.remote.rows.values() if ".gdm-stage-" in r["path"]]), 2)
        self.assertFalse(any(r["path"] == "webp/a.webp" for r in self.remote.rows.values()))

    def test_partial_stage_resume_preserves_old_id_and_uses_new_uuid(self):
        plan, key = self.plan()
        stage = self.mount.stage
        def partial(source, path, expected):
            self.remote.add(path, b"incomplete", "partial-stage")
            raise photos.Refusal("simulated write interruption")
        self.mount.stage = partial
        with self.assertRaisesRegex(photos.Refusal, "interruption"):
            self.apply(plan, key)
        self.mount.stage = stage
        self.apply(plan, key)
        self.assertEqual(self.remote.payloads["partial-stage"], b"incomplete")
        self.assertIn(".gdm-stage-", self.remote.rows["partial-stage"]["path"])
        self.assertTrue(any(r["path"] == "webp/a.webp" for r in self.remote.rows.values()))
        self.assertTrue(any(e["event"] == "incomplete-stage-preserved" for e in self.state.events(key)))

    def test_equal_count_wrong_bytes_does_not_verify(self):
        plan, key = self.plan()
        self.apply(plan, key)
        output = next(r for r in self.remote.rows.values() if r["path"] == "webp/a.webp")
        self.remote.payloads[output["id"]] = b"different-bytes-same-count"
        with self.assertRaisesRegex(photos.Refusal, "SHA256"):
            photos.verify_plan(plan, key, self.state, self.remote)

    def test_unjournaled_same_bytes_target_cannot_verify(self):
        plan, key = self.plan()
        self.remote.add("webp/a.webp", b"synthetic-corrected-webp", "unapproved-target")
        with self.assertRaisesRegex(photos.Refusal, "journaled"):
            photos.verify_plan(plan, key, self.state, self.remote)

    def test_same_bytes_replacement_id_cannot_verify_or_authorize_retirement(self):
        plan, key = self.plan()
        self.apply(plan, key)
        output = next(r for r in self.remote.rows.values() if r["path"] == "webp/a.webp")
        self.remote.rows.pop(output["id"])
        self.remote.add("webp/a.webp", b"synthetic-corrected-webp", "substituted-target")
        with self.assertRaisesRegex(photos.Refusal, "journaled ID"):
            photos.verify_plan(plan, key, self.state, self.remote)
        with self.assertRaisesRegex(photos.Refusal, "journaled ID"):
            photos.retire_plan(plan, key, key, self.state, self.remote)
        self.assertTrue((self.source / "originals/a.HEIC").exists())
        self.assertEqual(self.remote.moves, [])

    def test_unplanned_same_name_alias_cannot_verify_or_authorize_retirement(self):
        plan, key = self.plan()
        self.apply(plan, key)
        for alias_path, payload in (
            ("a.HEIC", b"synthetic-original-one"),
            ("a.HEIC", b"unrelated-photo"),
            ("A.HEIC", b"synthetic-original-one"),
        ):
            with self.subTest(path=alias_path, payload=payload):
                self.remote.add(alias_path, payload, "unplanned-alias")
                with self.assertRaisesRegex(photos.Refusal, "unplanned.*alias"):
                    photos.verify_plan(plan, key, self.state, self.remote)
                with self.assertRaisesRegex(photos.Refusal, "unplanned.*alias"):
                    photos.retire_plan(plan, key, key, self.state, self.remote)
                self.remote.rows.pop("unplanned-alias")
        self.assertEqual(self.remote.moves, [])
        self.assertTrue((self.source / "originals/a.HEIC").exists())

    def test_planned_same_name_duplicate_verifies_until_retired(self):
        self.remote.add("a.HEIC", b"synthetic-original-one", "original-b")
        plan, key = self.plan()
        self.apply(plan, key)
        self.assertEqual(len(photos.verify_plan(plan, key, self.state, self.remote)), 2)
        photos.retire_plan(plan, key, key, self.state, self.remote)
        self.assertEqual(self.remote.moves, ["original-b"])
        self.assertEqual(len(photos.verify_plan(plan, key, self.state, self.remote)), 2)

    def test_verification_reconciles_same_id_after_interrupted_promotion(self):
        plan, key = self.plan()
        rename = self.remote.rename
        def interrupted(row, destination):
            rename(row, destination)
            raise photos.Refusal("interrupted after server-side promotion")
        self.remote.rename = interrupted
        with self.assertRaisesRegex(photos.Refusal, "interrupted"):
            self.apply(plan, key)
        self.assertFalse(any(e["event"] == "promoted" for e in self.state.events(key)))
        verified = photos.verify_plan(plan, key, self.state, self.remote)
        output = next(e for e in plan["entries"] if e["kind"] == "webp")
        proof = next(e for e in self.state.events(key) if e["event"] == "stage-verified")
        self.assertEqual(verified[output["key"]]["id"], proof["id"])

    def test_publication_proof_must_bind_exact_entry_hash_and_target(self):
        plan, key = self.plan()
        self.apply(plan, key)
        events = self.state.events(key)
        for event_type, field, replacement in (
            ("stage-intent", "target", "webp/other-private-name.webp"),
            ("stage-verified", "sha256", "0" * 64),
            ("stage-verified", "entry", "other-entry"),
        ):
            altered = [{**event, field: replacement} if event["event"] == event_type else event for event in events]
            with self.subTest(field=field), mock.patch.object(self.state, "events", return_value=altered):
                with self.assertRaisesRegex(photos.Refusal, "journaled"):
                    photos.verify_plan(plan, key, self.state, self.remote)

    def test_cli_plan_reports_counts_without_private_warning_paths(self):
        plan, _ = self.plan()
        plan["warnings"] = ["private-photo-name.HEIC needs review"]
        plan["blockers"] = ["/private/corpus/secret-photo-path"]
        output = io.StringIO()
        config = (self.root / "mount", self.root / "runtime", self.state.root, self.root / "socket")
        with mock.patch.object(photos, "configuration", return_value=config), \
                mock.patch.object(photos, "RcloneRemote", return_value=self.remote), \
                mock.patch.object(photos, "make_plan", return_value=plan), \
                contextlib.redirect_stdout(output):
            self.assertEqual(photos.main(["plan"]), 0)
        summary = json.loads(output.getvalue())
        self.assertEqual(summary["warning_count"], 1)
        self.assertEqual(summary["blocker_count"], 1)
        self.assertNotIn("private-photo", output.getvalue())
        self.assertNotIn("/private/corpus", output.getvalue())
        self.assertEqual(self.state.load(summary["plan"])["warnings"], plan["warnings"])

    def test_missing_generated_recipe_cannot_verify(self):
        (self.source / "webp/a.webp").unlink()
        plan, key = self.plan()
        with self.assertRaisesRegex(photos.Refusal, "prepared"):
            photos.verify_plan(plan, key, self.state, self.remote)

    def test_encoder_resume_refuses_changed_output(self):
        (self.source / "webp/a.webp").unlink()
        plan, key = self.plan()
        self.apply(plan, key, encoder=lambda _src, dst: dst.write_bytes(b"encoded-v1"))
        with self.assertRaisesRegex(photos.Refusal, "encoder output changed"):
            self.apply(plan, key, encoder=lambda _src, dst: dst.write_bytes(b"encoded-v2"))

    def test_retirement_requires_exact_digest_and_remote_proof(self):
        plan, key = self.plan()
        with self.assertRaisesRegex(photos.Refusal, "approval"):
            photos.retire_plan(plan, key, key[:12], self.state, self.remote)
        with self.assertRaises(photos.Refusal):
            photos.retire_plan(plan, key, key, self.state, self.remote)
        self.assertTrue((self.source / "originals/a.HEIC").exists())

    def test_recoverable_retirement_and_resume_preserve_every_byte(self):
        self.remote.add("a.HEIC", b"synthetic-original-one", "original-b")
        plan, key = self.plan()
        self.apply(plan, key)
        result = photos.retire_plan(plan, key, key, self.state, self.remote)
        self.assertEqual(result["status"], "recoverable-quarantine")
        self.assertFalse(result["space_reclaimed"])
        self.assertEqual(self.remote.moves, ["original-b"])
        self.assertIn("original-a", self.remote.rows)
        moved = self.source / ".gdm-retired" / key / "originals/a.HEIC"
        self.assertEqual(moved.read_bytes(), b"synthetic-original-one")
        photos.retire_plan(plan, key, key, self.state, self.remote)
        self.assertEqual(self.remote.moves, ["original-b"])

    def test_apply_after_retirement_is_a_verified_noop(self):
        plan, key = self.plan()
        self.apply(plan, key)
        photos.retire_plan(plan, key, key, self.state, self.remote)
        self.assertFalse((self.source / "webp/a.webp").exists())
        writes = self.mount.writes
        self.apply(plan, key)
        self.assertEqual(self.mount.writes, writes)
        self.assertEqual(len(photos.verify_plan(plan, key, self.state, self.remote)), 2)
        self.assertTrue(any(event["event"] == "apply-noop" for event in self.state.events(key)))

    def test_post_retirement_noop_still_requires_mount_and_canonical_id(self):
        plan, key = self.plan()
        self.apply(plan, key)
        photos.retire_plan(plan, key, key, self.state, self.remote)
        self.mount.live = False
        with self.assertRaisesRegex(photos.Refusal, "mount absent"):
            self.apply(plan, key)
        self.mount.live = True
        output = next(row for row in self.remote.rows.values() if row["path"] == "webp/a.webp")
        self.remote.rows.pop(output["id"])
        self.remote.add("webp/a.webp", b"synthetic-corrected-webp", "replacement-id")
        with self.assertRaisesRegex(photos.Refusal, "journaled ID"):
            self.apply(plan, key)
        self.assertEqual(self.mount.writes, 1)
        self.assertFalse(any(event["event"] == "apply-noop" for event in self.state.events(key)))

    def test_canonical_removed_during_remote_retirement_keeps_local_original(self):
        self.remote.add("a.HEIC", b"synthetic-original-one", "original-b")
        plan, key = self.plan()
        self.apply(plan, key)
        retire = self.remote.retire

        def removed_canonical(row, digest):
            retire(row, digest)
            self.remote.rows.pop("original-a")

        self.remote.retire = removed_canonical
        with self.assertRaises(photos.Refusal):
            photos.retire_plan(plan, key, key, self.state, self.remote)
        self.assertEqual((self.source / "originals/a.HEIC").read_bytes(), b"synthetic-original-one")

    def test_canonical_bytes_changed_during_retirement_keeps_local_original(self):
        self.remote.add("a.HEIC", b"synthetic-original-one", "original-b")
        plan, key = self.plan()
        self.apply(plan, key)
        retire = self.remote.retire

        def changed_canonical(row, digest):
            retire(row, digest)
            self.remote.payloads["original-a"] = b"changed-with-unchanged-metadata"

        self.remote.retire = changed_canonical
        with self.assertRaisesRegex(photos.Refusal, "canonical bytes changed"):
            photos.retire_plan(plan, key, key, self.state, self.remote)
        self.assertEqual((self.source / "originals/a.HEIC").read_bytes(), b"synthetic-original-one")

    def test_changed_local_retirement_source_aborts_before_remote_move(self):
        self.remote.add("a.HEIC", b"synthetic-original-one", "original-b")
        plan, key = self.plan()
        self.apply(plan, key)
        (self.source / "originals/a.HEIC").write_bytes(b"changed")
        with self.assertRaises(photos.Refusal):
            photos.retire_plan(plan, key, key, self.state, self.remote)
        self.assertEqual(self.remote.moves, [])

    def test_unique_movie_is_preserved_and_unknown_derivatives_stay_local(self):
        (self.source / "originals/a.MOV").write_bytes(b"unique-live-photo-video")
        (self.source / "webp/orphan.webp").write_bytes(b"unknown-output-provenance")
        (self.source / "tmp").mkdir()
        (self.source / "tmp/experiment.png").write_bytes(b"unique-scratch-bytes")
        plan, key = self.plan()
        self.assertTrue(any(e["path"] == "a.MOV" and e["kind"] == "original" for e in plan["entries"]))
        self.assertNotIn("webp/orphan.webp", plan["local_retirement"])
        self.assertNotIn("tmp/experiment.png", plan["local_retirement"])
        self.assertEqual(len(plan["unresolved"]), 2)
        self.apply(plan, key)
        photos.retire_plan(plan, key, key, self.state, self.remote)
        self.assertTrue((self.source / "webp/orphan.webp").exists())
        self.assertTrue((self.source / "tmp/experiment.png").exists())
        self.assertTrue(any(r["path"] == "a.MOV" for r in self.remote.rows.values()))

    def test_drive_root_substitution_refused_before_write(self):
        plan, key = self.plan()
        self.remote.root_folder = lambda: "different-drive-folder"
        with self.assertRaisesRegex(photos.Refusal, "root identity"):
            self.apply(plan, key)
        self.assertEqual(self.mount.writes, 0)

    def test_destination_ancestor_id_substitution_refused(self):
        self.remote.add("webp", b"", "webp-dir", mime=photos.FOLDER_MIME)
        plan, key = self.plan()
        self.remote.rows.pop("webp-dir")
        self.remote.add("webp", b"", "new-webp-dir", mime=photos.FOLDER_MIME)
        with self.assertRaisesRegex(photos.Refusal, "ancestor Drive ID"):
            self.apply(plan, key)
        self.assertEqual(self.mount.writes, 0)

    def test_unique_original_name_reconciliation_preserves_drive_id(self):
        self.remote.add("a.HEIC", b"different-original", "original-c")
        plan, key = self.plan()
        self.apply(plan, key, encoder=lambda _src, dst: dst.write_bytes(b"encoded-different-original"))
        self.assertIn("original-c", self.remote.rows)
        self.assertNotEqual(self.remote.rows["original-c"]["path"], "a.HEIC")
        self.assertFalse(any(r["row"]["id"] == "original-c" for r in plan["retirement"]))
        photos.verify_plan(plan, key, self.state, self.remote)

    def test_crash_after_original_rename_reconciles_by_id(self):
        self.remote.add("a.HEIC", b"different-original", "original-c")
        plan, key = self.plan()
        rename = self.remote.rename
        def interrupted(row, destination):
            rename(row, destination)
            raise photos.Refusal("simulated interruption after rename")
        self.remote.rename = interrupted
        with self.assertRaisesRegex(photos.Refusal, "simulated"):
            self.apply(plan, key)
        self.remote.rename = rename
        self.apply(plan, key, encoder=lambda _src, dst: dst.write_bytes(b"encoded-different-original"))
        self.assertIn("original-c", self.remote.rows)

    def test_crash_after_remote_retirement_reconciles_by_id(self):
        self.remote.add("a.HEIC", b"synthetic-original-one", "original-b")
        plan, key = self.plan()
        self.apply(plan, key)
        retire = self.remote.retire
        def interrupted(row, digest):
            retire(row, digest)
            raise photos.Refusal("simulated interruption after move")
        self.remote.retire = interrupted
        with self.assertRaisesRegex(photos.Refusal, "simulated"):
            photos.retire_plan(plan, key, key, self.state, self.remote)
        self.remote.retire = retire
        photos.retire_plan(plan, key, key, self.state, self.remote)
        self.assertEqual(self.remote.moves, ["original-b"])

    def test_encoder_cannot_publish_under_changed_source_hash(self):
        (self.source / "webp/a.webp").unlink()
        plan, key = self.plan()
        def changed_source(source, destination):
            source.chmod(0o600)
            source.write_bytes(b"changed-private-source")
            destination.write_bytes(b"encoded-from-changed-bytes")
        with self.assertRaisesRegex(photos.Refusal, "source"):
            self.apply(plan, key, encoder=changed_source)
        self.assertEqual(self.mount.writes, 0)
        self.assertEqual((self.source / "originals/a.HEIC").read_bytes(), b"synthetic-original-one")

    def test_oversized_local_candidate_refused_before_hash(self):
        with mock.patch.object(photos, "MAX_BYTES", 1), mock.patch.object(photos, "hash_file") as hasher:
            with self.assertRaisesRegex(photos.Refusal, "budget"):
                self.plan()
            hasher.assert_not_called()

    def test_retirement_parent_symlink_escape_refused(self):
        plan, key = self.plan()
        self.apply(plan, key)
        outside = self.root / "outside"
        outside.mkdir()
        (self.source / ".gdm-retired").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            photos.retire_plan(plan, key, key, self.state, self.remote)
        self.assertTrue((self.source / "originals/a.HEIC").exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_conflicting_provenance_is_a_blocker(self):
        (self.source / "design-picks.md").write_text("version one")
        (self.source / "webp/design-picks.md").write_text("version two")
        plan, key = self.plan()
        self.assertTrue(plan["blockers"])
        with self.assertRaisesRegex(photos.Refusal, "provenance"):
            self.apply(plan, key)

    def test_journal_interrupted_record_is_not_ignored(self):
        _, key = self.plan()
        with (self.state.root / f"journal-{key}.jsonl").open("ab") as f:
            f.write(b'{"partial":')
        with self.assertRaisesRegex(photos.Refusal, "partial"):
            self.state.events(key)

    def test_placeholder_orientation_is_reported_and_curation_preserved(self):
        (self.source / "orientation-judged.csv").write_text("name,cw,how\na,180,agreed\nmissing_placeholder,0,no file\n")
        (self.source / "design-picks.md").write_text("one curated pick; no public publication")
        plan, _ = self.plan()
        self.assertTrue(any("missing_placeholder" in w for w in plan["warnings"]))
        self.assertEqual(len([e for e in plan["entries"] if e["kind"] == "provenance"]), 2)

    def test_selected_and_restricted_nonkeeper_aliases_survive_retirement(self):
        # Opaque fixture text, not an invented parser for the live sidecars.
        curation = b"Selected: selected-alias.webp\nDo not publish: restricted-alias.webp\n"
        (self.source / "design-picks.md").write_bytes(curation)
        for alias in ("selected-alias", "restricted-alias"):
            (self.source / f"originals/{alias}.HEIC").write_bytes(b"synthetic-original-one")
            (self.source / f"webp/{alias}.webp").write_bytes(b"synthetic-corrected-webp")
        plan, key = self.plan()
        original = next(e for e in plan["entries"] if e["kind"] == "original")
        derivative = next(e for e in plan["entries"] if e["kind"] == "webp")
        self.assertEqual(plan["alias_bindings_version"], 1)
        for alias in ("selected-alias", "restricted-alias"):
            binding = next(b for b in plan["alias_bindings"] if b["origin"] == "ssd" and
                           b["path"] == f"webp/{alias}.webp")
            self.assertEqual(binding["role"], "accepted-webp")
            self.assertEqual(binding["original_key"], original["key"])
            self.assertEqual(binding["webp_key"], derivative["key"])
            self.assertEqual(binding["original_sha256"], original["sha256"])
            self.assertEqual(binding["sha256"], derivative["sha256"])
            self.assertEqual(photos.alias_candidates(plan, f"{alias}.webp"), [original["key"]])
        self.apply(plan, key)
        photos.retire_plan(plan, key, key, self.state, self.remote)
        for alias in ("selected-alias", "restricted-alias"):
            self.assertFalse((self.source / f"webp/{alias}.webp").exists())
            self.assertEqual(photos.alias_candidates(plan, f"{alias}.webp"), [original["key"]])
        self.assertEqual((self.source / "design-picks.md").read_bytes(), curation)
        preserved = next(row for row in self.remote.rows.values() if row["path"] == "webp/design-picks.md")
        self.assertEqual(self.remote.payloads[preserved["id"]], curation)
        proof = [e for e in self.state.events(key) if e["event"] == "verified"][-1]
        self.assertEqual(proof["alias_bindings_sha256"], photos.digest(plan["alias_bindings"]))
        self.assertEqual(proof["entries"][derivative["key"]]["sha256"], derivative["sha256"])

    def test_remote_alias_binding_keeps_pre_retirement_id_and_path(self):
        alias = self.remote.add("alias.HEIC", b"synthetic-original-one", "retired-alias-id")
        plan, key = self.plan()
        binding = next(b for b in plan["alias_bindings"] if b["drive_id"] == alias["id"])
        self.assertEqual((binding["origin"], binding["role"], binding["path"]),
                         ("drive", "original", "alias.HEIC"))
        self.apply(plan, key)
        photos.retire_plan(plan, key, key, self.state, self.remote)
        self.assertIn((key, alias["id"]), self.remote.quarantined)
        self.assertEqual(binding["drive_id"], "retired-alias-id")
        self.assertEqual(photos.alias_candidates(plan, "alias.HEIC"), [binding["original_key"]])

    def test_existing_accepted_remote_alias_is_reused_without_new_webp_write(self):
        self.remote.add("alias.HEIC", b"synthetic-original-one", "alias-original")
        accepted = self.remote.add("webp/alias.webp", b"synthetic-corrected-webp", "accepted-alias")
        plan, key = self.plan()
        output = next(e for e in plan["entries"] if e["kind"] == "webp")
        self.assertEqual((output["keep_id"], output["path"]), (accepted["id"], accepted["path"]))
        self.apply(plan, key)
        self.assertEqual(self.mount.writes, 0)
        self.assertFalse(any(row["path"] == "webp/a.webp" for row in self.remote.rows.values()))
        proof = photos.verify_plan(plan, key, self.state, self.remote)
        self.assertEqual(proof[output["key"]]["id"], accepted["id"])
        self.assertEqual(photos.alias_candidates(plan, "alias.webp"), [output["original_key"]])

    def test_known_same_byte_remote_alias_retires_to_existing_canonical_id(self):
        self.remote.add("alias.HEIC", b"synthetic-original-one", "alias-original")
        keep = self.remote.add("webp/a.webp", b"synthetic-corrected-webp", "accepted-keeper")
        duplicate = self.remote.add("webp/alias.webp", b"synthetic-corrected-webp", "accepted-duplicate")
        plan, key = self.plan()
        output = next(e for e in plan["entries"] if e["kind"] == "webp")
        item = next(r for r in plan["retirement"] if r["row"]["id"] == duplicate["id"])
        self.assertEqual(item["reason"], "verified-webp-copy")
        self.assertEqual(item["canonical_key"], output["key"])
        self.apply(plan, key)
        photos.retire_plan(plan, key, key, self.state, self.remote)
        self.assertIn(keep["id"], self.remote.rows)
        self.assertIn((key, duplicate["id"]), self.remote.quarantined)
        self.assertEqual(len([r for r in self.remote.rows.values() if r["path"].endswith(".webp")]), 1)
        self.assertEqual(photos.alias_candidates(plan, "alias.webp"), [output["original_key"]])
        binding = next(b for b in plan["alias_bindings"] if b["drive_id"] == duplicate["id"])
        self.assertEqual(binding["webp_key"], output["key"])
        proof = photos.verify_plan(plan, key, self.state, self.remote)
        self.assertEqual(proof[output["key"]]["id"], keep["id"])

    def test_reused_webp_keeper_cannot_be_superseded_by_another_original(self):
        (self.source / "originals/b.HEIC").write_bytes(b"distinct original B")
        (self.source / "webp/b.webp").write_bytes(b"accepted output B")
        self.remote.add("b.JPG", b"synthetic-original-one", "original-a-alias")
        self.remote.add("b.HEIC", b"distinct original B", "original-b")
        keeper = self.remote.add("webp/b.webp", b"synthetic-corrected-webp", "accepted-a-alias")
        plan, key = self.plan()
        self.assertEqual(plan["blockers"], [])
        self.assertFalse(any(item["row"]["id"] == keeper["id"] for item in plan["retirement"]))
        outputs = [entry for entry in plan["entries"] if entry["kind"] == "webp"]
        self.assertEqual(len(outputs), 2)
        kept = next(entry for entry in outputs if entry["keep_id"] == keeper["id"])
        other = next(entry for entry in outputs if entry["key"] != kept["key"])
        self.assertEqual(kept["path"], "webp/b.webp")
        self.assertNotEqual(other["path"], kept["path"])
        self.apply(plan, key)
        photos.retire_plan(plan, key, key, self.state, self.remote)
        self.assertIn(keeper["id"], self.remote.rows)
        self.assertNotIn((key, keeper["id"]), self.remote.quarantined)
        proof = photos.verify_plan(plan, key, self.state, self.remote)
        self.assertEqual(proof[kept["key"]]["id"], keeper["id"])
        self.assertNotEqual(proof[other["key"]]["id"], keeper["id"])
        self.assertEqual({proof[entry["key"]]["sha256"] for entry in outputs},
                         {hashlib.sha256(content).hexdigest() for content in
                          (b"synthetic-corrected-webp", b"accepted output B")})

    def test_bare_alias_does_not_prefer_root_over_distinct_nested_original(self):
        (self.source / "originals/album").mkdir()
        (self.source / "webp/album").mkdir()
        (self.source / "originals/album/a.HEIC").write_bytes(b"different nested original")
        (self.source / "webp/album/a.webp").write_bytes(b"different nested accepted derivative")
        plan, _ = self.plan()
        originals = {e["key"] for e in plan["entries"] if e["kind"] == "original"}
        self.assertEqual(len(originals), 2)
        for reference in ("a", "a.HEIC", "a.webp"):
            self.assertEqual(set(photos.alias_candidates(plan, reference)), originals)
        exact = photos.alias_candidates(plan, "originals/album/a.HEIC")
        self.assertEqual(len(exact), 1)
        nested = next(e for e in plan["entries"] if e["path"] == "album/a.HEIC")
        self.assertEqual(exact, [nested["key"]])

    def test_pre_rename_aliases_keep_distinct_original_hashes(self):
        other = self.remote.add("a.HEIC", b"different bytes at same pathname", "z-other-original")
        plan, _ = self.plan()
        bindings = [b for b in plan["alias_bindings"] if b["origin"] == "drive" and b["path"] == "a.HEIC"]
        self.assertEqual({b["drive_id"] for b in bindings}, {"original-a", other["id"]})
        self.assertEqual(len({b["original_sha256"] for b in bindings}), 2)
        renamed = next(e for e in plan["entries"] if e.get("keep_id") == other["id"])
        self.assertNotEqual(renamed["path"], "a.HEIC")
        binding = next(b for b in bindings if b["drive_id"] == other["id"])
        self.assertEqual(binding["original_key"], renamed["key"])
        self.assertEqual(len(photos.alias_candidates(plan, "a.HEIC")), 2)

    def test_alias_binding_consistency_refuses_wrong_keys_hashes_and_identities(self):
        plan, _ = self.plan()
        cases = (("original_key", "missing-entry"), ("webp_key", "missing-entry"),
                 ("sha256", "0" * 64), ("original_sha256", "0" * 64),
                 ("origin", "unknown-source"), ("path", "unobserved/original.HEIC"),
                 ("drive_id", "substituted-id"))
        for field, value in cases:
            altered = json.loads(json.dumps(plan))
            binding = next(b for b in altered["alias_bindings"] if b["origin"] == "drive")
            binding[field] = value
            with self.subTest(field=field), self.assertRaises(photos.Refusal):
                photos.validate_alias_bindings(altered)
        altered = json.loads(json.dumps(plan))
        altered["alias_bindings"].append(dict(altered["alias_bindings"][0]))
        with self.assertRaises(photos.Refusal):
            photos.validate_alias_bindings(altered)

    def test_unknown_scratch_and_superseded_outputs_are_not_accepted_aliases(self):
        (self.source / "tmp").mkdir()
        (self.source / "tmp/scratch.webp").write_bytes(b"unproven scratch output")
        old = self.remote.add("webp/a.webp", b"superseded derivative", "superseded-id")
        plan, _ = self.plan()
        self.assertFalse(any(b["path"] == "tmp/scratch.webp" for b in plan["alias_bindings"]))
        self.assertFalse(any(b["drive_id"] == old["id"] for b in plan["alias_bindings"]))

    def test_generated_derivative_alias_binds_recipe_then_verified_output(self):
        (self.source / "webp/a.webp").unlink()
        plan, key = self.plan()
        derivative = next(e for e in plan["entries"] if e["kind"] == "webp")
        self.assertIsNone(derivative["sha256"])
        self.assertTrue(all(b["role"] == "original" for b in plan["alias_bindings"]))
        self.assertTrue(all(b["webp_key"] == derivative["key"] for b in plan["alias_bindings"]))
        self.apply(plan, key, encoder=lambda _src, dst: dst.write_bytes(b"generated output"))
        verified = photos.verify_plan(plan, key, self.state, self.remote)
        self.assertEqual(verified[derivative["key"]]["sha256"], hashlib.sha256(b"generated output").hexdigest())
        proof = [e for e in self.state.events(key) if e["event"] == "verified"][-1]
        self.assertEqual(proof["alias_bindings_sha256"], photos.digest(plan["alias_bindings"]))

    def test_legacy_manifest_resumes_without_claiming_alias_evidence(self):
        plan, _ = self.plan()
        plan.pop("alias_bindings")
        plan.pop("alias_bindings_version")
        key = self.state.save(plan)
        photos.validate_alias_bindings(plan)
        self.apply(plan, key)
        photos.verify_plan(plan, key, self.state, self.remote)
        proof = [e for e in self.state.events(key) if e["event"] == "verified"][-1]
        self.assertNotIn("alias_bindings_sha256", proof)

    def test_partial_or_unknown_alias_schema_refused(self):
        plan, _ = self.plan()
        for missing in ("alias_bindings", "alias_bindings_version"):
            altered = {k: v for k, v in plan.items() if k != missing}
            with self.subTest(missing=missing), self.assertRaises(photos.Refusal):
                photos.validate_alias_bindings(altered)
        altered = {**plan, "alias_bindings_version": 900}
        with self.assertRaises(photos.Refusal):
            photos.validate_alias_bindings(altered)

    def test_relative_paths_reject_traversal(self):
        for path in ("../a", "/a", "a/../b", "a//b", "a/./b", "a\\b", ""):
            with self.subTest(path=path), self.assertRaises(photos.Refusal):
                photos.relative(path)

    def test_dirfd_publication_is_atomic_no_clobber_and_resumable(self):
        point = self.root / "real-fd-mount"
        (point / photos.FOLDER).mkdir(parents=True)
        source = self.source / "webp/a.webp"
        expected = photos.hash_file(source)
        with LocalMount(point) as mount:
            self.assertEqual(mount.publish(source, "webp/a.webp", expected), "published")
            self.assertEqual(mount.publish(source, "webp/a.webp", expected), "reused")
            (point / photos.FOLDER / "webp/a.webp").write_bytes(b"conflict")
            with self.assertRaises(photos.Refusal):
                mount.publish(source, "webp/a.webp", expected)
        self.assertEqual((point / photos.FOLDER / "webp/a.webp").read_bytes(), b"conflict")
        self.assertFalse(list(point.rglob("*.tmp")))

    def test_detach_refuses_without_shadow_write(self):
        point = self.root / "real-fd-mount"
        (point / photos.FOLDER).mkdir(parents=True)
        source = self.source / "webp/a.webp"
        with LocalMount(point) as mount:
            mount.generation = "new-generation"
            with self.assertRaisesRegex(photos.Refusal, "mount identity"):
                mount.publish(source, "webp/a.webp", photos.hash_file(source))
        self.assertFalse((point / photos.FOLDER / "webp").exists())

    def test_dirfd_publication_refuses_symlink_escape(self):
        point = self.root / "real-fd-mount"
        (point / photos.FOLDER).mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir()
        (point / photos.FOLDER / "webp").symlink_to(outside, target_is_directory=True)
        source = self.source / "webp/a.webp"
        with LocalMount(point) as mount, self.assertRaises(OSError):
            mount.publish(source, "webp/a.webp", photos.hash_file(source))
        self.assertEqual(list(outside.iterdir()), [])

    def test_ambiguous_rename_preserves_journal_owned_temporary_and_resumes(self):
        point = self.root / "real-fd-mount"
        (point / photos.FOLDER).mkdir(parents=True)
        source = self.source / "webp/a.webp"
        expected = photos.hash_file(source)
        temporary = ".gdm-photo-owned-by-test.tmp"
        with LocalMount(point) as mount:
            with mock.patch.object(photos, "rename_noreplace", side_effect=OSError(5, "simulated EIO")):
                with self.assertRaises(OSError):
                    mount.publish(source, "webp/a.webp", expected, temporary=temporary)
            path = point / photos.FOLDER / "webp" / temporary
            self.assertEqual(path.read_bytes(), source.read_bytes())
            self.assertEqual(mount.publish(source, "webp/a.webp", expected, temporary=temporary), "published")
            self.assertFalse(path.exists())

    def test_nfs_staging_uses_no_exclusive_rename_or_truncation(self):
        point = self.root / "real-fd-mount"
        (point / photos.FOLDER).mkdir(parents=True)
        source = self.source / "webp/a.webp"
        expected = photos.hash_file(source)
        stage_path = "webp/.gdm-stage-" + "a" * 32 + ".tmp"
        with LocalMount(point) as mount, mock.patch.object(photos, "rename_noreplace", side_effect=OSError(45, "NFS unsupported")) as rename:
            self.assertEqual(mount.stage(source, stage_path, expected), "staged")
            self.assertEqual(mount.stage(source, stage_path, expected), "stage-reused")
            rename.assert_not_called()
        self.assertFalse((point / photos.FOLDER / "webp/a.webp").exists())

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow available in photos package")
    def test_tiny_real_webp_validation(self):
        from PIL import Image
        image = self.root / "one.webp"
        Image.new("RGB", (2, 1), "blue").save(image, "WEBP", quality=80)
        self.assertEqual(photos.validate_webp(image), {"width": 2, "height": 1})
        image.write_bytes(b"RIFF-broken")
        with self.assertRaises(Exception):
            photos.validate_webp(image)

    def test_required_codec_runtime(self):
        """Pinned gates must not turn absent native codecs into a green skip."""
        if os.environ.get("GDM_PHOTOS_REQUIRE_CODECS") != "1":
            return
        try:
            from PIL import Image, features
            import pillow_heif
        except ImportError as exc:
            self.fail(f"required photo codec runtime unavailable: {exc}")
        self.assertTrue(features.check("webp"), "required Pillow WebP feature unavailable")
        pillow_heif.register_heif_opener()
        self.assertIn("HEIF", Image.OPEN, "required HEIF decoder is not registered")
        self.assertIn("HEIF", Image.SAVE, "required synthetic HEIF encoder is not registered")

    @unittest.skipUnless(
        importlib.util.find_spec("PIL") and importlib.util.find_spec("pillow_heif"),
        "Pillow and HEIF available in photos package",
    )
    def test_tiny_real_heif_to_webp_conversion(self):
        """Exercise the actual codec pipeline, orientation, and metadata removal."""
        from PIL import Image
        import pillow_heif

        source = self.root / "synthetic.heic"
        destination = self.root / "synthetic.webp"
        image = Image.new("RGB", (32, 16), "red")
        image.paste("blue", (16, 0, 32, 16))
        exif = Image.Exif()
        exif[274] = 6  # Clockwise quarter turn: red above blue after decoding.
        exif[270] = "synthetic private metadata"
        with mock.patch.object(pillow_heif.options, "DECODE_THREADS", 1):
            pillow_heif.register_heif_opener()
            image.save(
                source, "HEIF", quality=90, exif=exif.tobytes(),
                xmp=b'<x:xmpmeta xmlns:x="adobe:ns:meta/">synthetic-only</x:xmpmeta>',
                enc_params={"preset": "ultrafast", "x265:pools": "none", "x265:frame-threads": "1"},
            )
            with Image.open(source) as encoded:
                self.assertTrue(encoded.info.get("exif"), "fixture must contain metadata to strip")
                self.assertEqual(encoded.getexif().get(270), "synthetic private metadata")
            original_bytes = source.read_bytes()
            photos.convert(source, destination)
        self.assertEqual(source.read_bytes(), original_bytes, "conversion must preserve the original")
        self.assertEqual(photos.validate_webp(destination), {"width": 16, "height": 32})
        with Image.open(destination) as converted:
            self.assertFalse(any(converted.info.get(key) for key in ("exif", "xmp", "icc_profile")))
            red = converted.getpixel((8, 4))
            blue = converted.getpixel((8, 28))
            self.assertGreater(red[0], red[2] + 100)
            self.assertGreater(blue[2], blue[0] + 100)


if __name__ == "__main__":
    unittest.main()
