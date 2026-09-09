"""Pure parsing and portable catalog construction for the reviewed GFTB sidecars.

Rotation records describe earlier judgments, not new conversion instructions.
A design selection is independent of restrictions and never grants consent.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import PurePosixPath
import re


ORIENTATION_NAME = "orientation-judged.csv"
DESIGN_NAME = "design-picks.md"
PLACEHOLDER = "IMG_2630_placeholder"
PLACEHOLDER_NOTE = "tiebreak:no file, placeholder, no cue"
MAX_SIDECAR_BYTES = 64 * 1024
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_BULLET = re.compile(r"^\s*-\s+(?:\*\*([\w./-]+\.jpg)\*\*|([\w./-]+\.jpg))"
                     r"(?:\s*:\s*[^\r\n]*)?\s*$", re.I)
_HEADINGS = {"Design picks": "design_records",
             "Faces or plates (consent review, do not publish)": "restriction_records"}


class CurationError(ValueError):
    """The observed records cannot be bound unambiguously to verified media."""


def _canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _hash(value):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise CurationError("invalid SHA256 in curation evidence")
    return value


def _string(value):
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise CurationError("invalid identity in curation evidence")
    return value


def _path(value):
    _string(value)
    path = PurePosixPath(value)
    if (not path.parts or path.is_absolute() or str(path) != value or "\\" in value
            or any(part in {"", ".", ".."} for part in path.parts)):
        raise CurationError("invalid relative curation path")
    return path


def _groups(bindings):
    """Check internal lineage; the caller validates inventory proof separately."""
    if not isinstance(bindings, list) or not bindings:
        raise CurationError("curation requires original alias evidence")
    groups, hashes, identities, webp_owners = {}, {}, set(), {}
    for row in bindings:
        required = {"origin", "role", "path", "drive_id", "sha256",
                    "original_sha256", "original_key", "webp_key"}
        if not isinstance(row, dict) or set(row) != required:
            raise CurationError("invalid alias binding fields")
        if row["origin"] not in {"ssd", "drive"} or row["role"] not in {"original", "accepted-webp"}:
            raise CurationError("invalid alias origin or role")
        _path(row["path"])
        if row["origin"] == "drive":
            _string(row["drive_id"])
        elif row["drive_id"] is not None:
            raise CurationError("SSD aliases cannot carry a Drive ID")
        identity = (row["origin"], row["path"], row["drive_id"])
        if identity in identities:
            raise CurationError("duplicate alias binding")
        identities.add(identity)
        key, sha = _string(row["original_key"]), _hash(row["original_sha256"])
        _hash(row["sha256"])
        if hashes.setdefault(sha, key) != key:
            raise CurationError("one original hash has multiple canonical keys")
        group = groups.setdefault(key, {"sha256": sha, "webp_key": row["webp_key"],
                                        "webp_sha256": None, "originals": 0, "aliases": []})
        if group["sha256"] != sha or group["webp_key"] != row["webp_key"]:
            raise CurationError("inconsistent original group lineage")
        webp_key = row["webp_key"]
        if webp_key is not None:
            _string(webp_key)
            if webp_key == key or webp_owners.setdefault(webp_key, key) != key:
                raise CurationError("WebP key belongs to multiple original groups")
        if row["role"] == "original":
            if row["sha256"] != sha:
                raise CurationError("original alias hash differs from its group")
            group["originals"] += 1
        else:
            if webp_key is None or group["webp_sha256"] not in {None, row["sha256"]}:
                raise CurationError("inconsistent accepted WebP evidence")
            group["webp_sha256"] = row["sha256"]
        group["aliases"].append(dict(row))
    if any(not g["originals"] for g in groups.values()) or set(groups) & set(webp_owners):
        raise CurationError("canonical original and WebP identities overlap or lack originals")
    for group in groups.values():
        group["aliases"].sort(key=lambda row: (row["origin"], row["path"], row["drive_id"] or ""))
    return groups


def _reference_index(bindings):
    exact, basenames, stems, path_stems = {}, {}, {}, {}
    for row in bindings:
        path, key = _path(row["path"]), row["original_key"]
        for index, name in ((exact, str(path)), (basenames, path.name),
                            (stems, path.stem), (path_stems, str(path.with_suffix("")))):
            index.setdefault(name, set()).add(key)
    return exact, basenames, stems, path_stems


def _resolve(reference, indexes):
    path = _path(reference)
    exact, basenames, stems, path_stems = indexes
    # A bare token never prefers a root-level alias over a nested candidate.
    candidates = set((exact if "/" in reference else basenames).get(reference, ()))
    if path.suffix.lower() == ".jpg":
        candidates.update((path_stems if "/" in reference else stems).get(
            str(path.with_suffix("")) if "/" in reference else path.stem, ()))
    elif not path.suffix:
        candidates.update((path_stems if "/" in reference else stems).get(reference, ()))
    if len(candidates) != 1:
        raise CurationError("curation reference is missing or ambiguous")
    return next(iter(candidates))


def _document(payload):
    if not isinstance(payload, bytes) or len(payload) > MAX_SIDECAR_BYTES:
        raise CurationError("curation sidecar must be bounded bytes")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CurationError("curation sidecar is not UTF-8") from exc
    if "\x00" in text:
        raise CurationError("curation sidecar contains a NUL byte")
    return {"sha256": hashlib.sha256(payload).hexdigest(), "text": text}


def parse_curation(orientation_bytes, design_bytes, alias_bindings):
    """Resolve the reviewed CSV/Markdown grammar without reading any files.

    Callers first validate alias_bindings against their immutable inventory.
    The exact no-file placeholder is an annotation, never an image reference.
    """
    groups = _groups(alias_bindings)
    bindings = sorted((dict(row) for group in groups.values() for row in group["aliases"]),
                      key=lambda row: (row["origin"], row["path"], row["drive_id"] or ""))
    indexes = _reference_index(bindings)
    sources = {ORIENTATION_NAME: _document(orientation_bytes), DESIGN_NAME: _document(design_bytes)}
    result = {"version": 1, "sources": sources, "alias_bindings": bindings,
              "orientation_semantics": "historical-only", "orientation_records": [],
              "design_records": [], "restriction_records": [], "annotations": []}
    text = sources[ORIENTATION_NAME]["text"]
    lines = text.splitlines(keepends=True)
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        if next(reader, None) != ["name", "cw", "how"]:
            raise CurationError("orientation header must be name,cw,how")
        previous = reader.line_num
        placeholder_seen = False
        for fields in reader:
            first, last = previous + 1, reader.line_num
            previous = last
            if len(fields) != 3 or fields[1] not in {"0", "90", "180", "270"}:
                raise CurationError("invalid orientation row")
            record = {"line": first, "raw": "".join(lines[first - 1:last]),
                      "fields": dict(zip(("name", "cw", "how"), fields)), "cw": int(fields[1])}
            if fields[0] == PLACEHOLDER:
                if placeholder_seen or fields[1:] != ["0", PLACEHOLDER_NOTE]:
                    raise CurationError("invalid or duplicate no-file placeholder annotation")
                placeholder_seen = True
                record["original_key"] = None
                result["annotations"].append(record)
            else:
                record["original_key"] = _resolve(fields[0], indexes)
                result["orientation_records"].append(record)
    except csv.Error as exc:
        raise CurationError("malformed orientation CSV") from exc
    section, seen = None, set()
    for number, raw in enumerate(sources[DESIGN_NAME]["text"].splitlines(keepends=True), 1):
        heading = re.match(r"^##\s+(.+?)\s*#*\s*$", raw.rstrip("\r\n"))
        if heading:
            section = _HEADINGS.get(heading[1])
            if section:
                if section in seen:
                    raise CurationError("duplicate curation section")
                seen.add(section)
            continue
        if re.match(r"^#\s", raw):
            section = None
        if not section or not raw.strip():
            continue
        match = _BULLET.fullmatch(raw)
        if not match:
            raise CurationError("unrecognized record in reviewed curation section")
        reference = match[1] or match[2]
        result[section].append({"line": number, "raw": raw, "reference": reference,
                                "original_key": _resolve(reference, indexes)})
    if seen != set(_HEADINGS.values()):
        raise CurationError("both reviewed curation headings are required")
    return result


def _verified(row):
    if not isinstance(row, dict):
        raise CurationError("canonical media lacks verified evidence")
    try:
        return {"id": _string(row["id"]), "path": str(_path(row["path"])),
                "sha256": _hash(row["sha256"])}
    except KeyError as exc:
        raise CurationError("canonical media lacks verified identity, path or hash") from exc


def build_catalog(curation, verified, drive_root_id, provenance):
    """Return deterministic JSON bytes from curation and already-verified media.

    Verification and publication remain the caller's responsibility. The catalog
    deliberately contains neither its own identity/hash nor transient phase data.
    """
    if not isinstance(curation, dict) or type(curation.get("version")) is not int or curation["version"] != 1:
        raise CurationError("unsupported curation version")
    try:
        sources = curation["sources"]
        regenerated = parse_curation(sources[ORIENTATION_NAME]["text"].encode("utf-8"),
                                     sources[DESIGN_NAME]["text"].encode("utf-8"),
                                     curation["alias_bindings"])
    except (KeyError, AttributeError, TypeError) as exc:
        raise CurationError("incomplete curation evidence") from exc
    if regenerated != curation:
        raise CurationError("curation differs from its raw records and alias evidence")
    if not isinstance(verified, dict) or not isinstance(provenance, list):
        raise CurationError("catalog requires verified media and provenance")
    groups = _groups(curation["alias_bindings"])
    selected = {row["original_key"] for row in curation["design_records"]}
    restricted = {row["original_key"] for row in curation["restriction_records"]}
    originals, used_ids = [], set()
    for key, group in sorted(groups.items()):
        original = _verified(verified.get(key))
        if original["sha256"] != group["sha256"]:
            raise CurationError("verified original hash differs from curation lineage")
        webp = None
        if group["webp_key"] is not None:
            webp = _verified(verified.get(group["webp_key"]))
            if group["webp_sha256"] not in {None, webp["sha256"]}:
                raise CurationError("verified WebP hash differs from accepted aliases")
            webp["entry_key"] = group["webp_key"]
        for row in (original, webp):
            if row is not None:
                if row["id"] in used_ids:
                    raise CurationError("canonical media reuses a Drive ID")
                used_ids.add(row["id"])
        originals.append({"original_key": key, "canonical": original,
                          "aliases": group["aliases"], "webp": webp,
                          "selected": key in selected, "restricted": key in restricted})
    sidecars, seen = [], set()
    verified_identities = {(row.get("id"), row.get("path"), row.get("sha256"))
                           for row in verified.values() if isinstance(row, dict)}
    for evidence in provenance:
        row = _verified(evidence)
        name = PurePosixPath(row["path"]).name
        if (name not in sources or name in seen or row["sha256"] != sources[name]["sha256"]
                or (row["id"], row["path"], row["sha256"]) not in verified_identities
                or row["id"] in used_ids):
            raise CurationError("sidecar provenance is missing, duplicated or differs from source bytes")
        seen.add(name)
        used_ids.add(row["id"])
        sidecars.append(row)
    if seen != set(sources):
        raise CurationError("both unchanged curation sidecars need verified provenance")
    return _canonical({"version": 1, "drive_folder_id": _string(drive_root_id),
                       "provenance": sorted(sidecars, key=lambda row: row["path"]),
                       "curation": {key: value for key, value in curation.items() if key != "alias_bindings"},
                       "originals": originals})
