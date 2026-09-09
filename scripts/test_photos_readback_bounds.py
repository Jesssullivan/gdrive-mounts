"""Bounded mounted-read and provenance-spelling regressions on tiny fixtures."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import gdrive_photos as photos
from test_gdrive_photos import LocalMount
import test_photos_catalog as catalog_fixtures


class ReadbackBoundsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gdm-readback-bounds-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.point = self.root / "mount"
        self.corpus = self.point / photos.FOLDER
        self.corpus.mkdir(parents=True)
        self.payload = self.corpus / "payload"
        self.destination = self.root / "private-copy"
        self.mount = LocalMount(self.point)

    def observed_reader(self, calls, grow=False):
        original_fdopen = os.fdopen
        payload = self.payload

        class Reader:
            def __init__(self, source):
                self.source = source
                self.grown = False

            def __enter__(self):
                self.source.__enter__()
                return self

            def __exit__(self, *args):
                return self.source.__exit__(*args)

            def fileno(self):
                return self.source.fileno()

            def read(self, size):
                calls.append(size)
                value = self.source.read(size)
                if grow and value and not self.grown:
                    self.grown = True
                    with payload.open("ab") as growing_fixture:
                        growing_fixture.write(b"overflow")
                return value

        return lambda *args, **kwargs: Reader(original_fdopen(*args, **kwargs))

    def test_initial_size_mismatch_refuses_before_any_payload_read(self):
        self.payload.write_bytes(b"x" * 1024)
        calls = []
        with self.mount, mock.patch.object(photos.os, "fdopen", side_effect=self.observed_reader(calls)):
            with self.assertRaisesRegex(photos.Refusal, "size differs"):
                self.mount.read_to("payload", self.destination, hashlib.sha256(b"tiny").hexdigest(),
                                   expected_size=4)
        self.assertEqual(calls, [])
        self.assertEqual(self.destination.stat().st_size, 0)

    def test_growth_after_initial_stat_cannot_write_beyond_verified_length(self):
        self.payload.write_bytes(b"tiny")
        calls = []
        with self.mount, mock.patch.object(photos.os, "fdopen", side_effect=self.observed_reader(calls, grow=True)):
            with self.assertRaisesRegex(photos.Refusal, "exceeds"):
                self.mount.read_to("payload", self.destination, hashlib.sha256(b"tiny").hexdigest(),
                                   expected_size=4)
        self.assertEqual(self.destination.read_bytes(), b"tiny")
        self.assertEqual(calls, [5, 1])  # Remaining bytes plus one bounded overflow probe.
        self.assertGreater(self.payload.stat().st_size, self.destination.stat().st_size)

    def test_legacy_read_still_enforces_global_budget(self):
        self.payload.write_bytes(b"oversized")
        calls = []
        with self.mount, mock.patch.object(photos, "MAX_BYTES", 4), \
                mock.patch.object(photos.os, "fdopen", side_effect=self.observed_reader(calls)):
            with self.assertRaisesRegex(photos.Refusal, "size differs"):
                self.mount.read_to("payload", self.destination, hashlib.sha256(b"oversized").hexdigest())
        self.assertEqual(calls, [])
        self.assertEqual(self.destination.stat().st_size, 0)

    def test_noninteger_negative_or_unbounded_verified_lengths_refuse(self):
        self.payload.write_bytes(b"tiny")
        with self.mount:
            for size in (True, "4", 4.0, -1, photos.MAX_BYTES + 1):
                with self.subTest(size=size), self.assertRaisesRegex(photos.Refusal, "bounded verified"):
                    self.mount.read_to("payload", self.destination, hashlib.sha256(b"tiny").hexdigest(),
                                       expected_size=size)
        self.assertFalse(self.destination.exists())


class ProvenanceSpellingTests(unittest.TestCase):
    def setUp(self):
        # Reuse the existing strict whole-collection fixture, not its test class.
        catalog_fixtures.CatalogIntegrationTests.setUp(self)

    def test_casevariant_sidecars_block_before_publication_or_retirement(self):
        variants = (("webp/Design-Picks.md", catalog_fixtures.DESIGN),
                    ("Orientation-Judged.csv", catalog_fixtures.ORIENTATION))
        for path, payload in variants:
            with self.subTest(path=path):
                row = self.remote.add(path, payload, "casevariant-provenance")
                before = self.remote.inventory("folder")
                plan = photos.make_plan(self.source, self.point, self.remote,
                                        validator=self.validator, require_curation=True)
                self.assertTrue(any("case-variant provenance path" in value for value in plan["blockers"]))
                key = self.state.save(plan)
                with self.assertRaises(photos.Refusal):
                    photos.apply_plan(plan, key, self.state, self.remote, self.mount,
                                      encoder=self.encoder, validator=self.validator)
                with self.assertRaises(photos.Refusal):
                    photos.retire_plan(plan, key, key, self.state, self.remote)
                self.assertEqual(self.mount.writes, 0)
                self.assertEqual(self.remote.moves, [])
                self.assertEqual(self.remote.inventory("folder"), before)
                self.remote.rows.pop(row["id"])
                self.remote.payloads.pop(row["id"])


if __name__ == "__main__":
    unittest.main()
