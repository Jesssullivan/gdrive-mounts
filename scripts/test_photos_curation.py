"""Pure synthetic curation/catalog tests: no images, mounts, secrets or files."""
import copy
import hashlib
import json
import unittest

import gftb_curation as curation


def sha(payload):
    return hashlib.sha256(payload).hexdigest()


def alias(path, key="original-a", original_sha=None, webp_key="webp-a",
          origin="ssd", drive_id=None, role="original", payload_sha=None):
    original_sha = original_sha or sha(b"original-a")
    return {"origin": origin, "role": role, "path": path, "drive_id": drive_id,
            "sha256": payload_sha or original_sha, "original_sha256": original_sha,
            "original_key": key, "webp_key": webp_key}


class CurationTests(unittest.TestCase):
    def setUp(self):
        self.bindings = [alias("originals/IMG_0001.HEIC"), alias("originals/IMG_0002.HEIC"),
                         alias("webp/IMG_0001.webp", role="accepted-webp", payload_sha=sha(b"webp-a"))]
        self.csv = b'name,cw,how\r\nIMG_0002.jpg,180,"QuickLook cue, not EXIF"\r\n'
        self.md = (b'# Historical choices\n\n## Design picks\n- **IMG_0002.jpg**\n'
                   b'## Faces or plates (consent review, do not publish)\n- IMG_0001.jpg\n')

    def parse(self, csv=None, md=None, bindings=None):
        return curation.parse_curation(self.csv if csv is None else csv,
                                       self.md if md is None else md,
                                       self.bindings if bindings is None else bindings)

    def evidence(self, parsed):
        verified = {"original-a": {"id": "original-id", "path": "IMG_0001.HEIC", "sha256": sha(b"original-a")},
                    "webp-a": {"id": "webp-id", "path": "webp/IMG_0001.webp", "sha256": sha(b"webp-a")}}
        provenance = []
        for name, source in parsed["sources"].items():
            row = {"id": name + "-id", "path": "webp/" + name, "sha256": source["sha256"]}
            verified[name] = dict(row)
            provenance.append(row)
        return verified, provenance

    def catalog(self, parsed=None):
        parsed = self.parse() if parsed is None else parsed
        verified, provenance = self.evidence(parsed)
        return curation.build_catalog(parsed, verified, "gftb-folder-id", provenance)

    def test_legacy_reference_resolves_nonkeeper_alias_by_original_hash_group(self):
        parsed = self.parse()
        record = parsed["orientation_records"][0]
        self.assertEqual(record["original_key"], "original-a")
        self.assertEqual(record["fields"], {"name": "IMG_0002.jpg", "cw": "180", "how": "QuickLook cue, not EXIF"})
        self.assertEqual(record["raw"], self.csv.decode().splitlines(keepends=True)[1])
        self.assertEqual(parsed["sources"][curation.ORIENTATION_NAME], {"sha256": sha(self.csv), "text": self.csv.decode()})
        self.assertEqual(parsed["orientation_semantics"], "historical-only")

    def test_csv_multiline_quotes_and_crlf_are_lossless(self):
        payload = b'name,cw,how\r\nIMG_0001.HEIC,270,"cue says ""rotate""\r\nsecond line"\r\n'
        parsed = self.parse(csv=payload)
        row = parsed["orientation_records"][0]
        self.assertEqual(row["line"], 2)
        self.assertEqual(row["fields"]["how"], 'cue says "rotate"\r\nsecond line')
        self.assertEqual(row["raw"], payload.decode().split("\r\n", 1)[1])

    def test_exact_placeholder_is_non_image_annotation(self):
        payload = self.csv + b'IMG_2630_placeholder,0,"tiebreak:no file, placeholder, no cue"\r\n'
        parsed = self.parse(csv=payload)
        self.assertEqual(len(parsed["orientation_records"]), 1)
        self.assertEqual(parsed["annotations"][0]["fields"]["name"], "IMG_2630_placeholder")
        self.assertIsNone(parsed["annotations"][0]["original_key"])
        self.assertEqual(len(json.loads(self.catalog(parsed))["originals"]), 1)

    def test_no_general_placeholder_or_different_note_exception(self):
        for name, cw, how in (("IMG_2631_placeholder", "0", "tiebreak:no file, placeholder, no cue"),
                              ("IMG_2630_placeholder", "180", "tiebreak:no file, placeholder, no cue"),
                              ("IMG_2630_placeholder", "0", "no file"),
                              ("IMG_2630_placeholder", "0", "no file, placeholder, no cue")):
            with self.subTest(name=name, cw=cw, how=how), self.assertRaises(curation.CurationError):
                self.parse(csv=f'name,cw,how\r\n{name},{cw},"{how}"\r\n'.encode())

    def test_duplicate_placeholder_refuses(self):
        row = b'IMG_2630_placeholder,0,"tiebreak:no file, placeholder, no cue"\r\n'
        with self.assertRaises(curation.CurationError):
            self.parse(csv=self.csv + row + row)

    def test_selected_and_restricted_are_independent_and_do_not_grant_consent(self):
        document = json.loads(self.catalog())
        original = document["originals"][0]
        self.assertTrue(original["selected"])
        self.assertTrue(original["restricted"])
        self.assertEqual(original["canonical"]["id"], "original-id")
        self.assertEqual(original["webp"]["sha256"], sha(b"webp-a"))
        self.assertNotIn("publish", original)
        self.assertNotIn("consent", original)
        self.assertEqual(document["curation"]["design_records"][0]["reference"], "IMG_0002.jpg")

    def test_blank_sections_allowed_without_fixed_historical_count(self):
        parsed = self.parse(csv=b"name,cw,how\r\n", md=(b"## Design picks\n\n"
                            b"## Faces or plates (consent review, do not publish)\n"))
        self.assertEqual(parsed["design_records"], [])
        self.assertEqual(parsed["restriction_records"], [])
        original = json.loads(self.catalog(parsed))["originals"][0]
        self.assertFalse(original["selected"])
        self.assertFalse(original["restricted"])

    def test_unbold_restricted_bullets_and_descriptions_are_preserved(self):
        md = self.md.replace(b"- IMG_0001.jpg", b"- IMG_0001.jpg: person\n- IMG_0002.jpg: plate")
        records = self.parse(md=md)["restriction_records"]
        self.assertEqual([r["reference"] for r in records], ["IMG_0001.jpg", "IMG_0002.jpg"])
        self.assertEqual(records[0]["raw"], "- IMG_0001.jpg: person\n")
        selected = self.parse(md=self.md.replace(b"**IMG_0002.jpg**", b"**IMG_0002.jpg**: scene"))
        self.assertEqual(selected["design_records"][0]["reference"], "IMG_0002.jpg")

    def test_unrecognized_section_records_do_not_silently_drop_restrictions(self):
        for record in (b"- IMG_0001: person", b"- IMG_0001.jgp: person", b"person only",
                       b"- IMG_0001.jpg, IMG_0002.jpg", b"- IMG_0001.jpg or IMG_0002.jgp"):
            with self.subTest(record=record), self.assertRaises(curation.CurationError):
                self.parse(md=self.md.replace(b"- IMG_0001.jpg", record))

    def test_missing_and_duplicate_headings_refuse(self):
        for md in (b"## Design picks\n", self.md + b"## Design picks\n"):
            with self.subTest(md=md), self.assertRaises(curation.CurationError):
                self.parse(md=md)

    def test_same_stem_different_originals_refuse_even_with_root_exact_alias(self):
        other = alias("album/IMG_0002.jpg", key="original-b", original_sha=sha(b"b"), webp_key=None)
        bindings = [*self.bindings, alias("IMG_0002.jpg"), other]
        with self.assertRaises(curation.CurationError):
            self.parse(bindings=bindings)

    def test_legacy_jpg_collision_with_distinct_heic_refuses(self):
        other = alias("originals/IMG_0002.JPG", key="original-b", original_sha=sha(b"b"), webp_key=None)
        with self.assertRaises(curation.CurationError):
            self.parse(bindings=self.bindings + [other])

    def test_explicit_relative_reference_avoids_basename_ambiguity(self):
        other = alias("album/IMG_0002.HEIC", key="original-b", original_sha=sha(b"b"), webp_key=None)
        parsed = self.parse(csv=self.csv.replace(b"IMG_0002.jpg", b"originals/IMG_0002.jpg"),
                            md=self.md.replace(b"IMG_0002.jpg", b"originals/IMG_0002.jpg"),
                            bindings=self.bindings + [other])
        self.assertEqual(parsed["design_records"][0]["original_key"], "original-a")

    def test_missing_reference_or_unreviewed_markdown_extension_refuses(self):
        for md in (self.md.replace(b"IMG_0002.jpg", b"IMG_9999.jpg"),
                   self.md.replace(b"IMG_0002.jpg", b"IMG_0002.heic")):
            with self.subTest(md=md), self.assertRaises(curation.CurationError):
                self.parse(md=md)

    def test_malformed_csv_encoding_size_and_paths_refuse(self):
        payloads = [b"name,how,cw\n", b"name,cw,how\nIMG_0001.jpg,45,cue\n",
                    b'name,cw,how\nIMG_0001.jpg,0,"unfinished\n', b"\xff",
                    b"x" * (64 * 1024 + 1), b"name,cw,how\n../IMG_0001.jpg,0,cue\n"]
        for payload in payloads:
            with self.subTest(payload=payload[:40]), self.assertRaises(curation.CurationError):
                self.parse(csv=payload)

    def test_alias_lineage_conflicts_refuse(self):
        for update in ({"original_sha256": sha(b"wrong")}, {"sha256": sha(b"wrong")},
                       {"webp_key": "other-webp"}, {"drive_id": "ssd-cannot-have-id"}):
            with self.subTest(update=update), self.assertRaises(curation.CurationError):
                self.parse(bindings=[*self.bindings, {**self.bindings[0], "path": "originals/alias.HEIC", **update}])

    def test_catalog_order_stable_no_timestamp_phase_or_self_identity(self):
        parsed = self.parse()
        other = self.parse(bindings=list(reversed(self.bindings)))
        verified, provenance = self.evidence(other)
        actual = curation.build_catalog(other, dict(reversed(list(verified.items()))), "gftb-folder-id", list(reversed(provenance)))
        self.assertEqual(actual, self.catalog(parsed))
        data = json.loads(actual)
        self.assertEqual(set(data), {"version", "drive_folder_id", "provenance", "curation", "originals"})
        self.assertEqual(data["drive_folder_id"], "gftb-folder-id")

    def test_accepted_aliases_survive_canonical_path_change_and_quarantine(self):
        bindings = self.bindings + [alias("old/IMG_0002.HEIC", origin="drive", drive_id="retired-alias-id")]
        parsed = self.parse(bindings=bindings)
        verified, provenance = self.evidence(parsed)
        verified["original-a"]["path"] = "renamed-canonical.HEIC"
        data = json.loads(curation.build_catalog(parsed, verified, "gftb-folder-id", provenance))
        self.assertEqual(data["originals"][0]["canonical"]["path"], "renamed-canonical.HEIC")
        self.assertIn("retired-alias-id", [r["drive_id"] for r in data["originals"][0]["aliases"]])

    def test_generated_webp_hash_comes_from_later_verified_evidence(self):
        parsed = self.parse(bindings=self.bindings[:2])
        verified, provenance = self.evidence(parsed)
        verified["webp-a"]["sha256"] = sha(b"freshly-generated")
        data = json.loads(curation.build_catalog(parsed, verified, "gftb-folder-id", provenance))
        self.assertEqual(data["originals"][0]["webp"]["sha256"], sha(b"freshly-generated"))

    def test_preserved_original_without_webp_remains_in_catalog(self):
        bindings = self.bindings + [alias("originals/companion.MOV", key="movie", original_sha=sha(b"movie"), webp_key=None)]
        parsed = self.parse(bindings=bindings)
        verified, provenance = self.evidence(parsed)
        verified["movie"] = {"id": "movie-id", "path": "companion.MOV", "sha256": sha(b"movie")}
        data = json.loads(curation.build_catalog(parsed, verified, "gftb-folder-id", provenance))
        movie = next(row for row in data["originals"] if row["original_key"] == "movie")
        self.assertIsNone(movie["webp"])

    def test_tampered_records_sources_and_versions_refuse(self):
        for kind in ("record", "source", "version"):
            parsed = self.parse()
            if kind == "record":
                parsed["design_records"][0]["original_key"] = "unrelated"
            elif kind == "source":
                parsed["sources"][curation.DESIGN_NAME]["sha256"] = sha(b"wrong")
            else:
                parsed["version"] = True
            with self.subTest(kind=kind), self.assertRaises(curation.CurationError):
                self.catalog(parsed)

    def test_missing_wrong_hash_duplicate_id_and_unsafe_canonical_path_refuse(self):
        parsed = self.parse()
        for key, updates in (("original-a", None), ("original-a", {"sha256": sha(b"wrong")}),
                             ("webp-a", {"sha256": sha(b"wrong")}),
                             ("webp-a", {"id": "original-id"}), ("original-a", {"path": "/outside/file"})):
            verified, provenance = self.evidence(parsed)
            if updates is None:
                del verified[key]
            else:
                verified[key].update(updates)
            with self.subTest(key=key, updates=updates), self.assertRaises(curation.CurationError):
                curation.build_catalog(parsed, verified, "gftb-folder-id", provenance)

    def test_both_unchanged_verified_sidecars_required(self):
        parsed = self.parse()
        for kind in ("missing", "wrong-bytes", "not-verified", "duplicate"):
            verified, provenance = self.evidence(parsed)
            if kind == "missing":
                provenance.pop()
            elif kind == "wrong-bytes":
                provenance[0]["sha256"] = sha(b"wrong")
            elif kind == "not-verified":
                del verified[curation.ORIENTATION_NAME]
            else:
                provenance.append(copy.deepcopy(provenance[0]))
            with self.subTest(kind=kind), self.assertRaises(curation.CurationError):
                curation.build_catalog(parsed, verified, "gftb-folder-id", provenance)


if __name__ == "__main__":
    unittest.main()
