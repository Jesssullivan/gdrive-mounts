#!/usr/bin/env python3
"""Synthetic strict-curation/catalog integration; never opens live media.

LocalMount exercises production directory-FD reads on disposable local files.
It does not claim that a local filesystem proves production NFS behavior.
"""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import gdrive_photos as photos
from test_gdrive_photos import FakeMount, FakeRemote, LocalMount


ORIENTATION = (
    'name,cw,how\r\n'
    'IMG_0001,0,earlier judgment\r\n'
    'IMG_0002,90,earlier alias judgment\r\n'
    'IMG_0003,180,earlier judgment\r\n'
    'IMG_2630_placeholder,0,"tiebreak:no file, placeholder, no cue"\r\n'
).encode()
DESIGN = (
    '# Synthetic design notes\n'
    '## Design picks\n'
    '- **IMG_0002.jpg**: selected alias\n'
    '- IMG_0003.jpg: selected image\n'
    '## Faces or plates (consent review, do not publish)\n'
    '- IMG_0002.jpg: remains restricted\n'
).encode()


class CatalogIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gdm-catalog-fixture-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source"
        self.point = self.root / "mount"
        (self.source / "originals").mkdir(parents=True)
        (self.source / "webp").mkdir()
        self.remote = FakeRemote()
        originals = {
            "IMG_0001": b"synthetic-original-one",
            "IMG_0002": b"synthetic-original-one",
            "IMG_0003": b"synthetic-original-three",
        }
        for stem, body in originals.items():
            (self.source / "originals" / f"{stem}.HEIC").write_bytes(body)
            self.remote.add(f"{stem}.HEIC", body, f"original-{stem}")
            corrected = b"synthetic-corrected-one" if stem != "IMG_0003" else b"synthetic-corrected-three"
            (self.source / "webp" / f"{stem}.webp").write_bytes(corrected)
        for name, body in zip(photos.CURATION_NAMES, (ORIENTATION, DESIGN)):
            (self.source / name).write_bytes(body)
            (self.source / "webp" / name).write_bytes(body)
            self.remote.add(name, body, f"root-{name}")
        self.state = photos.State(self.root / "state")
        self.mount = FakeMount(self.remote)
        self.validator = mock.Mock(return_value={"width": 2000, "height": 1500})
        self.encoder = mock.Mock(side_effect=AssertionError("accepted corrected bytes must not be re-encoded"))

    def plan(self):
        plan = photos.make_plan(self.source, self.point, self.remote,
                                validator=self.validator, require_curation=True)
        self.assertEqual(plan["blockers"], [])
        return plan, self.state.save(plan)

    def apply(self, plan, key):
        return photos.apply_plan(plan, key, self.state, self.remote, self.mount,
                                 encoder=self.encoder, validator=self.validator)

    def applied(self):
        plan, key = self.plan()
        self.apply(plan, key)
        return plan, key

    def retired(self):
        plan, key = self.applied()
        photos.retire_plan(plan, key, key, self.state, self.remote)
        return plan, key

    def catalog_row(self):
        rows = [row for row in self.remote.rows.values() if row["path"] == photos.CATALOG_PATH]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def mounted_copy(self):
        """Mirror only this test's synthetic remote objects into local fixtures."""
        corpus = self.point / photos.FOLDER
        corpus.mkdir(parents=True, exist_ok=True)
        for row in self.remote.rows.values():
            path = corpus / photos.relative(row["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.remote.payloads[row["id"]])
        return LocalMount(self.point)

    def assert_no_complete_readback(self, key):
        self.assertFalse(any(event["event"] == "mounted-readback-verified"
                             for event in self.state.events(key)))

    def test_strict_plan_binds_raw_curation_and_deferred_catalog(self):
        plan, key = self.plan()
        photos.validate_curation(plan)
        self.assertEqual(self.state.load(key), plan)
        self.assertEqual(plan["curation"]["orientation_semantics"], "historical-only")
        self.assertEqual(len(plan["curation"]["annotations"]), 1)
        for name, body in zip(photos.CURATION_NAMES, (ORIENTATION, DESIGN)):
            self.assertEqual(plan["curation_sources"][name]["text"].encode(), body)
            self.assertEqual(plan["curation_sources"][name]["sha256"], hashlib.sha256(body).hexdigest())
        catalogs = [entry for entry in plan["entries"] if entry["kind"] == "catalog"]
        self.assertEqual(len(catalogs), 1)
        self.assertEqual(catalogs[0]["path"], photos.CATALOG_PATH)
        self.assertIsNone(catalogs[0]["sha256"])
        self.assertIsNone(catalogs[0]["keep_id"])
        self.assertEqual(self.mount.writes, 0)

    def test_catalog_is_deterministic_and_matches_verified_media(self):
        plan, key = self.applied()
        verified = photos.verify_plan(plan, key, self.state, self.remote, include_catalog=False)
        first = photos.catalog_content(plan, verified)
        with mock.patch.object(photos.time, "time", return_value=9999999999):
            reordered = dict(reversed(list(verified.items())))
            self.assertEqual(photos.catalog_content(plan, reordered), first)
        row = self.catalog_row()
        self.assertEqual(self.remote.payloads[row["id"]], first)
        document = json.loads(first)
        self.assertEqual(document["drive_folder_id"], plan["drive_root_id"])
        self.assertEqual(len(document["originals"]), 2)
        self.assertNotIn(str(self.root), first.decode())
        self.assertNotIn(row["id"], first.decode())
        self.encoder.assert_not_called()

    def test_catalog_promotion_crash_resumes_same_id_without_rewrites(self):
        plan, key = self.plan()
        catalog = next(entry for entry in plan["entries"] if entry["kind"] == "catalog")
        original_event = self.state.event

        def interrupt_receipt(digest, action, **values):
            if action == "promoted" and values.get("entry") == catalog["key"]:
                raise RuntimeError("synthetic crash after catalog promotion")
            return original_event(digest, action, **values)

        with mock.patch.object(self.state, "event", side_effect=interrupt_receipt):
            with self.assertRaisesRegex(RuntimeError, "after catalog promotion"):
                self.apply(plan, key)
        before = dict(self.catalog_row())
        before_bytes = self.remote.payloads[before["id"]]
        writes = self.mount.writes
        self.apply(self.state.load(key), key)
        self.assertEqual(self.catalog_row()["id"], before["id"])
        self.assertEqual(self.remote.payloads[before["id"]], before_bytes)
        self.assertEqual(self.mount.writes, writes)
        photos.verify_plan(plan, key, self.state, self.remote)

    def test_media_verification_failure_prevents_catalog_publication(self):
        plan, key = self.plan()
        catalog_key = next(entry["key"] for entry in plan["entries"] if entry["kind"] == "catalog")
        original_verify = photos.verify_plan

        def fail_media(*args, **kwargs):
            if kwargs.get("include_catalog") is False:
                raise photos.Refusal("synthetic media proof unavailable")
            return original_verify(*args, **kwargs)

        with mock.patch.object(photos, "verify_plan", side_effect=fail_media):
            with self.assertRaisesRegex(photos.Refusal, "media proof unavailable"):
                self.apply(plan, key)
        self.assertFalse(any(row["path"] == photos.CATALOG_PATH for row in self.remote.rows.values()))
        self.assertFalse(any(event.get("entry") == catalog_key for event in self.state.events(key)))

    def test_preexisting_catalog_blocks_plan_without_retiring_existing_id(self):
        old = self.remote.add(photos.CATALOG_PATH, b"unreviewed existing catalog", "existing-catalog")
        plan = photos.make_plan(self.source, self.point, self.remote,
                                validator=self.validator, require_curation=True)
        self.assertTrue(any("existing catalog" in blocker for blocker in plan["blockers"]))
        self.assertFalse(any(item["row"]["id"] == old["id"] for item in plan["retirement"]))
        key = self.state.save(plan)
        with self.assertRaises(photos.Refusal):
            self.apply(plan, key)
        self.assertEqual(self.remote.payloads[old["id"]], b"unreviewed existing catalog")
        self.assertEqual(self.mount.writes, 0)

    def test_catalog_created_after_plan_is_not_overwritten(self):
        plan, key = self.plan()
        old = self.remote.add(photos.CATALOG_PATH, b"concurrent catalog", "concurrent-catalog")
        with self.assertRaisesRegex(photos.Refusal, "occupied|concurrently"):
            self.apply(plan, key)
        self.assertEqual(self.catalog_row()["id"], old["id"])
        self.assertEqual(self.remote.payloads[old["id"]], b"concurrent catalog")

    def test_restricted_nonkeeper_alias_survives_in_portable_catalog(self):
        plan, key = self.retired()
        self.assertFalse((self.source / "webp/IMG_0002.webp").exists())
        restricted_key = plan["curation"]["restriction_records"][0]["original_key"]
        row = self.catalog_row()
        document = json.loads(self.remote.payloads[row["id"]])
        original = next(item for item in document["originals"] if item["original_key"] == restricted_key)
        self.assertTrue(original["selected"])
        self.assertTrue(original["restricted"])
        self.assertEqual(original["canonical"]["id"], "original-IMG_0001")
        self.assertTrue(any(alias["path"] == "webp/IMG_0002.webp" for alias in original["aliases"]))
        self.assertTrue(any(alias["drive_id"] == "original-IMG_0002" for alias in original["aliases"]))
        self.assertEqual(document["curation"]["sources"]["design-picks.md"]["text"].encode(), DESIGN)
        writes = self.mount.writes
        self.apply(self.state.load(key), key)
        self.assertEqual(self.mount.writes, writes)

    def test_only_exact_two_root_sidecars_retire_and_unrelated_files_stay(self):
        unrelated = ("notes.txt", "design-picks.md.backup", "orientation-judged.csv.extra", "nested/design-picks.md")
        for name in unrelated:
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(DESIGN)
        plan, key = self.retired()
        for name, body in zip(photos.CURATION_NAMES, (ORIENTATION, DESIGN)):
            self.assertIn(name, plan["local_retirement"])
            self.assertFalse((self.source / name).exists())
            self.assertEqual((self.source / ".gdm-retired" / key / name).read_bytes(), body)
            canonical = next(row for row in self.remote.rows.values() if row["path"] == f"webp/{name}")
            self.assertEqual(self.remote.payloads[canonical["id"]], body)
        for name in unrelated:
            self.assertNotIn(name, plan["local_retirement"])
            self.assertEqual((self.source / name).read_bytes(), DESIGN)

    def test_drive_ssd_provenance_difference_blocks_without_retiring_either_copy(self):
        identifier = "root-design-picks.md"
        self.remote.rows.pop(identifier)
        different = DESIGN + b"\nDifferent Drive-side authority.\n"
        self.remote.add("design-picks.md", different, identifier)
        plan = photos.make_plan(self.source, self.point, self.remote,
                                validator=self.validator, require_curation=True)
        self.assertTrue(any("Drive/SSD provenance differs" in blocker for blocker in plan["blockers"]))
        self.assertFalse(any(item["row"]["id"] == identifier for item in plan["retirement"]))
        key = self.state.save(plan)
        with self.assertRaises(photos.Refusal):
            self.apply(plan, key)
        self.assertEqual(self.remote.payloads[identifier], different)
        self.assertEqual((self.source / "design-picks.md").read_bytes(), DESIGN)
        self.assertEqual(self.remote.moves, [])
        self.assertEqual(self.mount.writes, 0)

    def test_same_byte_unrelated_original_cannot_mask_missing_canonical_provenance(self):
        self.remote.add("unrelated-notes.txt", DESIGN, "unrelated-notes")
        plan, key = self.applied()
        provenance_key = plan["curation_sources"]["design-picks.md"]["entry_key"]
        verified = photos.verify_plan(plan, key, self.state, self.remote)
        canonical = verified[provenance_key]
        self.assertEqual(canonical["sha256"], self.remote.sha256(self.remote.rows["unrelated-notes"]))
        final_remote_move = plan["retirement"][-1]["row"]["id"]
        original_retire = self.remote.retire

        def remove_canonical_after_remote_moves(row, digest):
            original_retire(row, digest)
            if row["id"] == final_remote_move:
                self.remote.rows.pop(canonical["id"])

        with mock.patch.object(self.remote, "retire", side_effect=remove_canonical_after_remote_moves):
            with self.assertRaisesRegex(photos.Refusal, "fingerprint|canonical"):
                photos.retire_plan(plan, key, key, self.state, self.remote)
        self.assertEqual((self.source / "design-picks.md").read_bytes(), DESIGN)
        self.assertIn("unrelated-notes", self.remote.rows)
        self.assertFalse(any(event["event"] == "local-retired" and event.get("path") == "design-picks.md"
                             for event in self.state.events(key)))

    def test_strict_mounted_readback_requires_completed_retirement(self):
        plan, key = self.applied()
        mount = self.mounted_copy()
        with mock.patch.object(mount, "read_to", wraps=mount.read_to) as reader:
            with self.assertRaisesRegex(photos.Refusal, "completed approved retirement"):
                photos.verified_location(plan, key, self.state, self.remote, mount, mounted_readback=True)
        reader.assert_not_called()
        self.assert_no_complete_readback(key)

    def test_strict_mounted_readback_covers_all_payloads_after_retirement(self):
        plan, key = self.retired()
        mount = self.mounted_copy()
        with mock.patch.object(mount, "read_to", wraps=mount.read_to) as reader:
            count = photos.verified_location(plan, key, self.state, self.remote, mount, mounted_readback=True)
        self.assertEqual(count, len(plan["entries"]))
        self.assertEqual({call.args[0] for call in reader.call_args_list}, {entry["path"] for entry in plan["entries"]})
        self.assertEqual(reader.call_count, len(plan["entries"]))
        self.assertTrue(any(event["event"] == "mounted-readback-verified" for event in self.state.events(key)))
        self.assertFalse((self.source / "originals/IMG_0001.HEIC").exists())

    def test_strict_mounted_readback_refuses_corrupt_payload_and_missing_file(self):
        plan, key = self.retired()
        for failure in ("corrupt", "missing"):
            with self.subTest(failure=failure):
                mount = self.mounted_copy()
                target = self.point / photos.FOLDER / photos.CATALOG_PATH
                if failure == "corrupt":
                    target.write_bytes(b"wrong mounted catalog bytes")
                else:
                    target.unlink()
                with self.assertRaises((photos.Refusal, FileNotFoundError)):
                    photos.verified_location(plan, key, self.state, self.remote, mount, mounted_readback=True)
                self.assert_no_complete_readback(key)
        mount = self.mounted_copy()
        self.assertEqual(photos.verified_location(plan, key, self.state, self.remote, mount, mounted_readback=True),
                         len(plan["entries"]))

    def test_strict_mounted_readback_rejects_same_byte_substituted_canonical_id(self):
        plan, key = self.retired()
        mount = self.mounted_copy()
        old = self.catalog_row()
        self.remote.rows.pop(old["id"])
        self.remote.add(old["path"], self.remote.payloads[old["id"]], "substituted-catalog")
        with mock.patch.object(mount, "read_to", wraps=mount.read_to) as reader:
            with self.assertRaises(photos.Refusal):
                photos.verified_location(plan, key, self.state, self.remote, mount, mounted_readback=True)
        reader.assert_not_called()
        self.assert_no_complete_readback(key)

    def test_strict_mounted_readback_rechecks_id_after_payload_reads(self):
        plan, key = self.retired()
        mount = self.mounted_copy()
        original_read = mount.read_to
        old = dict(self.catalog_row())

        def substitute_after_read(path, destination, expected_sha, **kwargs):
            original_read(path, destination, expected_sha, **kwargs)
            if path == photos.CATALOG_PATH:
                self.remote.rows.pop(old["id"])
                self.remote.add(path, self.remote.payloads[old["id"]], "substituted-during-readback")

        with mock.patch.object(mount, "read_to", side_effect=substitute_after_read):
            with self.assertRaises(photos.Refusal):
                photos.verified_location(plan, key, self.state, self.remote, mount, mounted_readback=True)
        self.assert_no_complete_readback(key)


if __name__ == "__main__":
    unittest.main()
