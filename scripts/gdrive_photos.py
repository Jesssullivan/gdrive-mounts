#!/usr/bin/env python3
"""Manifest-bound, serial reconciliation of the Sulliwood GFTB photo corpus.

The Drive adapter never reads or prints credentials. Remote reads use immutable
Drive IDs, not the pathname view which collapses duplicate Google Drive names.
All new photo bytes are staged through the guarded mount; verified IDs are
promoted via the Drive API. Retirement is a recoverable move, requires the
exact plan digest, and follows independent remote verification.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import ctypes
import fcntl
import hashlib
from importlib import metadata
import json
import os
import plistlib
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid


ORG = "sulliwood"
FOLDER = "GFTB Stuff"
SSD_ROOT = Path("/Volumes/TinylandSSD/tinyland/gftb-photos")
PHOTO_EXTENSIONS = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}
RETIRABLE_DIRS = {"originals", "webp", "webp-prev2", "webp-unrotated", "tmp"}
RECIPE = {"version": 1, "quality": 80, "long_edge": 2000, "method": 6,
          "metadata": "none", "orientation": "decoder-and-exif-transpose"}
FOLDER_MIME = "application/vnd.google-apps.folder"
MAX_FILES = 10000
MAX_BYTES = 64 * 1024 ** 3
CURATION_NAMES = ("orientation-judged.csv", "design-picks.md")
CURATION_PATHS = {path for name in CURATION_NAMES for path in (name, f"webp/{name}")}
CATALOG_PATH = "webp/gftb-catalog.json"
MAX_SIDECAR_BYTES = 64 * 1024


class Refusal(RuntimeError):
    """An unsafe or unverified operation was refused before further writes."""


class IncompleteStage(Refusal):
    """Journal-owned partial bytes must be preserved under their existing name."""


class RemoteFailure(Refusal):
    def __init__(self, operation, returncode):
        super().__init__(f"rclone {operation} failed with exit {returncode}")
        self.returncode = returncode


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def relative(value):
    p = PurePosixPath(value)
    if (not value or p.is_absolute() or str(p) != value or
            any(part in {"", ".", ".."} for part in p.parts) or
            "\x00" in value or "\\" in value):
        raise Refusal("invalid relative path")
    return p


def fingerprint(st):
    return {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size,
            "mtime_ns": st.st_mtime_ns, "ctime_ns": st.st_ctime_ns}


def hash_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        before = os.fstat(f.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise Refusal("source is not a regular file")
        h = hashlib.sha256()
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
        after = os.fstat(f.fileno())
    if fingerprint(before) != fingerprint(after):
        raise Refusal("source changed while hashing")
    return {"sha256": h.hexdigest(), "fingerprint": fingerprint(after)}


def assert_source(path, expected):
    actual = hash_file(path)
    if actual != expected:
        raise Refusal("source content or identity changed; create a new plan")


def snapshot(source, expected, destination):
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as src, Path(destination).open("xb") as dst:
        before = fingerprint(os.fstat(src.fileno()))
        if before != expected["fingerprint"]:
            raise Refusal("source identity changed before private snapshot")
        h = hashlib.sha256()
        for block in iter(lambda: src.read(1024 * 1024), b""):
            h.update(block)
            dst.write(block)
        if fingerprint(os.fstat(src.fileno())) != before or h.hexdigest() != expected["sha256"]:
            raise Refusal("source changed while creating private snapshot")
    Path(destination).chmod(0o400)


@contextlib.contextmanager
def anchored_parent(root_fd, path, create=False):
    """Walk only below an already-open root; reject every symlink component."""
    parts = relative(path).parts
    fd = os.dup(root_fd)
    device = os.fstat(root_fd).st_dev
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            if os.fstat(fd).st_dev != device:
                raise Refusal("path traverses a different filesystem")
        yield fd, parts[-1]
    finally:
        os.close(fd)


def hash_at(parent, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    with os.fdopen(fd, "rb") as f:
        before = os.fstat(f.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise Refusal("retirement source is not a regular file")
        sha = hashlib.file_digest(f, "sha256").hexdigest()
        after = os.fstat(f.fileno())
    if fingerprint(before) != fingerprint(after):
        raise Refusal("retirement source changed during hash verification")
    return {"sha256": sha, "fingerprint": fingerprint(after)}


def rename_noreplace(src, dst, src_fd, dst_fd):
    """Atomic no-clobber publication. Unsupported filesystems fail closed."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        name, flag = "renameatx_np", 4  # Darwin RENAME_EXCL
    else:
        name, flag = "renameat2", 1  # Linux RENAME_NOREPLACE
    fn = getattr(libc, name, None)
    if fn is None:
        raise Refusal("atomic no-replace rename is unavailable")
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                   ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    if fn(src_fd, os.fsencode(src), dst_fd, os.fsencode(dst), flag):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def private_dir(path):
    path = Path(path).absolute()
    for parent in reversed([path, *path.parents]):
        if parent.is_symlink():
            raise Refusal("private state path traverses a symlink")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    st = path.stat()
    if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
        raise Refusal("state directory must be owned by this user with mode 0700")
    return path


class State:
    def __init__(self, root):
        self.root = private_dir(root)

    @contextlib.contextmanager
    def locked(self):
        fd = os.open(self.root / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Refusal("another photos command holds the state lock") from None
            yield
        finally:
            os.close(fd)

    def save(self, plan):
        data = canonical(plan)
        key = hashlib.sha256(data).hexdigest()
        path = self.root / f"plan-{key}.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        self.event(key, "planned", entries=len(plan["entries"]))
        return key

    def load(self, key):
        if not re.fullmatch(r"[a-f0-9]{64}", key):
            raise Refusal("plan must be its full SHA256 digest")
        path = self.root / f"plan-{key}.json"
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o277:
                raise Refusal("plan must be private and read-only")
            raw = f.read()
        if hashlib.sha256(raw).hexdigest() != key:
            raise Refusal("immutable plan digest mismatch")
        plan = json.loads(raw)
        if plan.get("version") != 1 or plan.get("org") != ORG:
            raise Refusal("unsupported plan version or org")
        return plan

    def event(self, key, action, **values):
        row = {"plan": key, "at": time.time(), "event": action, **values}
        fd = os.open(self.root / f"journal-{key}.jsonl",
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "ab") as f:
            f.write(canonical(row))
            f.flush()
            os.fsync(f.fileno())

    def events(self, key):
        path = self.root / f"journal-{key}.jsonl"
        if not path.exists():
            return []
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r") as f:
            try:
                rows = [json.loads(line) for line in f]
            except json.JSONDecodeError:
                raise Refusal("journal has a partial record; reconcile before continuing") from None
        if any(row.get("plan") != key for row in rows):
            raise Refusal("journal plan mismatch")
        return rows


def safe_name(value):
    if len(relative(value).parts) != 1:
        raise Refusal("Drive name cannot be represented safely on this filesystem")
    return value


class RcloneRemote:
    """ID-aware adapter. See https://rclone.org/drive/#query and #copyid."""
    def __init__(self, config, state, sock, binary="rclone"):
        self.config, self.state, self.sock, self.binary = str(config), state, str(sock), binary

    def run(self, *args):
        cmd = [self.binary, *map(str, args), "--config", self.config,
               "--log-level", "ERROR", "--retries", "1", "--low-level-retries", "1",
               "--timeout", "30s", "--contimeout", "15s", "--transfers", "1",
               "--checkers", "1", "--multi-thread-streams", "0"]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
        except subprocess.TimeoutExpired:
            raise Refusal("rclone exceeded its operation deadline") from None
        if result.returncode:
            # Never echo argv, stderr or rendered config: all can carry auth context.
            operation = "operation"
            if args and args[0] in ("backend", "rc", "mkdir"):
                operation = args[0]
            if len(args) > 1 and args[0] == "backend" and args[1] in ("query", "copyid", "moveid"):
                operation = f"backend {args[1]}"
            raise RemoteFailure(operation, result.returncode)
        return result.stdout

    @staticmethod
    def quote(value):
        return value.replace("\\", "\\\\").replace("'", "\\'")

    def query(self, query):
        rows = json.loads(self.run("backend", "query", f"gdrive-{ORG}:", query))
        # rclone serializes an empty Drive query result as JSON null.
        if rows is None:
            return []
        if not isinstance(rows, list):
            raise Refusal("unexpected Drive query response")
        return rows

    def children(self, folder):
        return self.query(f"'{self.quote(folder)}' in parents and trashed = false")

    def root_folder(self):
        found = self.query(f"'root' in parents and name = '{self.quote(FOLDER)}' "
                           f"and mimeType = '{FOLDER_MIME}' and trashed = false")
        if len(found) != 1:
            raise Refusal("GFTB Stuff root folder is missing or ambiguous by Drive ID")
        return found[0]["id"]

    def inventory(self, folder):
        result, pending, seen = [], [(folder, "", 0)], set()
        while pending:
            parent, prefix, depth = pending.pop(0)
            if depth > 16 or parent in seen:
                raise Refusal("Drive folder tree is cyclic or exceeds depth limit")
            seen.add(parent)
            children = self.children(parent)
            quarantine_names = [r for r in children if r.get("name", "").casefold() == ".gdm-retired"]
            if quarantine_names and (len(quarantine_names) != 1 or quarantine_names[0].get("name") != ".gdm-retired" or quarantine_names[0].get("mimeType") != FOLDER_MIME):
                raise Refusal("reserved quarantine name is a shortcut, file, or ambiguous folder")
            for obj in children:
                name = safe_name(obj["name"])
                if name == ".gdm-retired":
                    continue
                path = f"{prefix}/{name}" if prefix else name
                row = {k: obj.get(k) for k in
                       ("id", "name", "mimeType", "size", "md5Checksum",
                        "sha256Checksum", "modifiedTime")}
                row.update(path=path, parent=parent)
                result.append(row)
                if obj.get("mimeType") == FOLDER_MIME:
                    pending.append((obj["id"], path, depth + 1))
                if len(result) > MAX_FILES:
                    raise Refusal("Drive inventory exceeds bounded file budget")
        return sorted(result, key=lambda r: (r["path"], r["id"]))

    def current(self, row):
        found = [r for r in self.children(row["parent"]) if r.get("id") == row["id"]]
        if len(found) != 1:
            raise Refusal("planned Drive object changed parent or disappeared")
        obj = found[0]
        for name in ("name", "size", "md5Checksum", "modifiedTime"):
            if obj.get(name) != row.get(name):
                raise Refusal("Drive object fingerprint changed; create a new plan")
        return obj

    def download(self, row, destination):
        self.current(row)
        self.run("backend", "copyid", f"gdrive-{ORG}:", row["id"], str(destination), "--immutable")
        self.current(row)

    def sha256(self, row):
        # Read back by ID even when Drive advertises hashes: this also proves
        # independent transport durability and avoids pathname deduplication.
        # download brackets the copy with fresh fingerprint checks.
        with tempfile.TemporaryDirectory(prefix="readback-", dir=self.state.root) as temp:
            target = Path(temp) / "object"
            self.download(row, target)
            return hash_file(target)["sha256"]

    def uploads_empty(self):
        data = json.loads(self.run("rc", "--unix-socket", self.sock, "vfs/stats"))
        if not isinstance(data, dict):
            raise Refusal("VFS upload evidence is not an object")
        cache = data.get("diskCache", {})
        required = ("uploadsQueued", "uploadsInProgress", "erroredFiles")
        if not isinstance(cache, dict) or any(type(cache.get(name)) is not int or cache[name] < 0 for name in required):
            raise Refusal("VFS upload queue evidence must contain nonnegative integer counts")
        if type(cache.get("outOfSpace")) is not bool:
            raise Refusal("VFS out-of-space evidence is unavailable or malformed")
        if any(cache[name] for name in required) or cache["outOfSpace"]:
            raise Refusal("VFS uploads have not drained; run verify again after drainage")

    def planning_sha256(self, row):
        """Plan-only download bound to advertised bytes and a final full snapshot.

        Mutation-phase verification deliberately retains per-object checks.
        Without an advertised content checksum, retain those checks here too.
        """
        expected_md5 = row.get("md5Checksum")
        if expected_md5 is None or expected_md5 == "":
            return self.sha256(row)
        if not isinstance(expected_md5, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", expected_md5):
            raise Refusal("planning content checksum is malformed")
        size = row.get("size")
        if not isinstance(size, (str, int)) or isinstance(size, bool) or not re.fullmatch(r"[0-9]+", str(size)):
            raise Refusal("planning byte length is unavailable")
        size = int(size)
        if size > MAX_BYTES:
            raise Refusal("planning object exceeds byte budget")
        advertised_sha = row.get("sha256Checksum")
        if advertised_sha is not None and (not isinstance(advertised_sha, str)
                or not re.fullmatch(r"[0-9a-fA-F]{64}", advertised_sha)):
            raise Refusal("planning SHA256 checksum is malformed")
        # At most two 120-second operations; never extend mutation retries.
        for attempt in range(2):
            try:
                return self._planning_download(row, size, expected_md5, advertised_sha)
            except RemoteFailure as error:
                if error.returncode != 5 or attempt:
                    raise

    def _planning_download(self, row, size, expected_md5, advertised_sha):
        with tempfile.TemporaryDirectory(prefix="plan-readback-", dir=self.state.root) as temp:
            target = Path(temp) / "object"
            self.run("backend", "copyid", f"gdrive-{ORG}:", row["id"], str(target), "--immutable")
            fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as payload:
                before = os.fstat(payload.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size != size:
                    raise Refusal("planning downloaded byte length differs")
                md5, sha, length = hashlib.md5(), hashlib.sha256(), 0
                for block in iter(lambda: payload.read(min(1024 * 1024, size - length + 1)), b""):
                    length += len(block)
                    if length > size:
                        raise Refusal("planning download exceeds advertised length")
                    md5.update(block)
                    sha.update(block)
                if length != size or fingerprint(before) != fingerprint(os.fstat(payload.fileno())):
                    raise Refusal("planning downloaded file changed")
            if md5.hexdigest() != expected_md5.lower() or (advertised_sha is not None
                    and sha.hexdigest() != advertised_sha.lower()):
                raise Refusal("planning downloaded content differs from inventory")
            return sha.hexdigest()

    def retire(self, row, key):
        self.current(row)
        name = f"{row['id']}-{safe_name(row['name'])}"
        original_parent_path = PurePosixPath(row["path"]).parent
        root = self.root_folder()
        if str(original_parent_path) == ".":
            if row["parent"] != root:
                raise Refusal("original retirement parent is not the planned Drive root")
        else:
            parents = [r for r in self.inventory(root) if r["path"] == str(original_parent_path) and r["mimeType"] == FOLDER_MIME]
            if len(parents) != 1 or parents[0]["id"] != row["parent"]:
                raise Refusal("original retirement parent ID or path changed")
        prefix = "" if str(original_parent_path) == "." else str(original_parent_path)
        parent = self.quarantine_parent(key, row["parent"], prefix)
        if any(r.get("name", "").casefold() == name.casefold() for r in self.children(parent)):
            raise Refusal("Drive quarantine destination already exists")
        path = original_parent_path / ".gdm-retired" / key / name
        destination = f"gdrive-{ORG}:{FOLDER}/{path}"
        self.run("backend", "moveid", f"gdrive-{ORG}:", row["id"], destination, "--immutable")

    def quarantine_parent(self, key, parent=None, prefix=""):
        parent = parent or self.root_folder()
        parts = []
        for name in (".gdm-retired", key):
            parts.append(name)
            found = [r for r in self.children(parent) if r.get("name", "").casefold() == name.casefold()]
            if not found:
                destination = PurePosixPath(FOLDER) / prefix / PurePosixPath(*parts)
                self.run("mkdir", f"gdrive-{ORG}:{destination}")
                found = [r for r in self.children(parent) if r.get("name", "").casefold() == name.casefold()]
            if len(found) != 1 or found[0].get("mimeType") != FOLDER_MIME or found[0]["name"] != name:
                raise Refusal("Drive quarantine folder is missing, a shortcut, or ambiguous")
            parent = found[0]["id"]
        return parent

    def rename(self, row, destination):
        """Rename in the existing parent, preserving the unique original ID."""
        self.current(row)
        if PurePosixPath(destination).parent != PurePosixPath(row["path"]).parent:
            raise Refusal("original reconciliation must be a sibling rename")
        name = PurePosixPath(destination).name
        if any(r.get("name", "").casefold() == name.casefold() for r in self.children(row["parent"])):
            raise Refusal("original rename destination already exists")
        self.run("backend", "moveid", f"gdrive-{ORG}:", row["id"],
                 f"gdrive-{ORG}:{FOLDER}/{destination}", "--immutable")

    def refresh(self, path):
        self.run("rc", "--unix-socket", self.sock, "vfs/refresh",
                 f"dir={FOLDER}/{path}" if path != "." else f"dir={FOLDER}", "recursive=false")

    def retired(self, row, key):
        parent = row["parent"]
        for name in (".gdm-retired", key):
            found = [r for r in self.children(parent) if r.get("name", "").casefold() == name.casefold()]
            if not found:
                return None
            if len(found) != 1 or found[0].get("name") != name or found[0].get("mimeType") != FOLDER_MIME:
                raise Refusal("recoverable Drive quarantine folder is ambiguous")
            parent = found[0]["id"]
        found = [r for r in self.children(parent) if r.get("id") == row["id"]]
        if not found:
            return None
        if len(found) != 1 or found[0]["name"] != f"{row['id']}-{row['name']}":
            raise Refusal("quarantined Drive object identity is unexpected")
        result = dict(found[0])
        path = PurePosixPath(row["path"]).parent / ".gdm-retired" / key / result["name"]
        result.update(parent=parent, path=str(path))
        return result


class MountedIO:
    """Directory-FD anchored publication; mount detachment cannot write shadows."""
    def __init__(self, point):
        self.point = Path(point)
        self.fd = None
        self.device = None
        self.table_line = None
        self.inode = None
        self.pid = None

    def daemon_pid(self):
        result = subprocess.run(["/bin/launchctl", "list", f"dev.tinyland.gdrive-mounts.{ORG}-root"],
                                capture_output=True, text=True, check=True, timeout=10)
        found = re.search(r'"PID"\s*=\s*(\d+)', result.stdout)
        if not found:
            raise Refusal("mount daemon PID is unavailable")
        return int(found[1])

    def table(self):
        result = subprocess.run(["/sbin/mount"], capture_output=True, text=True,
                                check=True, timeout=10)
        needle = f" on {self.point} ("
        lines = [line for line in result.stdout.splitlines() if needle in line]
        if len(lines) != 1 or "read-only" in lines[0] or "(nfs," not in lines[0]:
            raise Refusal("the exact writable NFS mount is absent")
        return lines[0]

    def check(self):
        if self.table() != self.table_line:
            raise Refusal("mount identity changed during the operation")
        if self.fd is not None and os.fstat(self.fd).st_dev != self.device:
            raise Refusal("mount filesystem identity changed")
        if self.fd is not None:
            current = os.stat(self.point, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (self.device, self.inode) or self.daemon_pid() != self.pid:
                raise Refusal("current mount generation changed")

    def __enter__(self):
        self.table_line = self.table()
        nfs = subprocess.run(["/usr/bin/nfsstat", "-m"], capture_output=True,
                             text=True, check=True, timeout=10).stdout
        section = nfs.split(str(self.point), 1)
        if len(section) != 2:
            raise Refusal("negotiated NFS options are unavailable")
        options = section[1].split("\n\n", 1)[0]
        if not re.search(r"\bsoft\b", options) or re.search(r"\b(?:nointr|not responding)\b", options):
            raise Refusal("NFS must have responsive, interruptible soft semantics before photo I/O")
        self.fd = os.open(self.point, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.device = os.fstat(self.fd).st_dev
        self.inode = os.fstat(self.fd).st_ino
        self.pid = self.daemon_pid()
        if self.device == self.point.parent.stat().st_dev:
            self.__exit__(None, None, None)
            raise Refusal("mountpoint is a local shadow")
        self.check()
        return self

    def __exit__(self, *_):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    @contextlib.contextmanager
    def parent(self, path, create=False):
        parts = relative(f"{FOLDER}/{path}").parts
        self.check()
        fd = os.dup(self.fd)
        try:
            for part in parts[:-1]:
                self.check()
                if create and part != FOLDER:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
                if os.fstat(fd).st_dev != self.device:
                    raise Refusal("publication path escaped the mounted filesystem")
            yield fd, parts[-1]
        finally:
            os.close(fd)

    def read_to(self, path, destination, expected_sha, *, expected_size=None):
        if expected_size is not None and (type(expected_size) is not int
                                         or not 0 <= expected_size <= MAX_BYTES):
            raise Refusal("mounted read requires a bounded verified byte length")
        limit = MAX_BYTES if expected_size is None else expected_size
        with self.parent(path) as (parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            with os.fdopen(fd, "rb") as source, Path(destination).open("xb") as target:
                before = fingerprint(os.fstat(source.fileno()))
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise Refusal("mounted original is not a regular file")
                if before["size"] > limit or (expected_size is not None and before["size"] != expected_size):
                    raise Refusal("mounted payload size differs from the verified byte length")
                h = hashlib.sha256()
                count = 0
                while block := source.read(min(1024 * 1024, limit - count + 1)):
                    self.check()
                    count += len(block)
                    if count > limit:
                        raise Refusal("mounted payload exceeds the verified byte length")
                    h.update(block)
                    target.write(block)
                self.check()
                if (fingerprint(os.fstat(source.fileno())) != before or h.hexdigest() != expected_sha
                        or (expected_size is not None and count != expected_size)):
                    raise Refusal("mounted original does not match its planned ID-resolved bytes")
        Path(destination).chmod(0o400)

    def stage(self, source, path, expected):
        """Write only a journal-owned unpredictable staging name on NFS.

        NFS cannot guarantee exclusive CREATE or exclusive rename on this
        backend. This never touches a canonical filename, never truncates an
        existing file, and claims no atomic no-clobber property. The subsequent
        drained-queue, unique Drive ID + SHA256 checks authorize promotion.
        """
        if not re.fullmatch(r"\.gdm-stage-[a-f0-9]{32}\.tmp", PurePosixPath(path).name):
            raise Refusal("staging requires a journal-owned unpredictable UUID name")
        assert_source(source, expected)
        with self.parent(path, create=True) as (parent, name):
            try:
                current = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            except FileNotFoundError:
                current = None
            if current is not None:
                with os.fdopen(current, "rb") as f:
                    value = hashlib.file_digest(f, "sha256").hexdigest()
                if value != expected["sha256"]:
                    raise IncompleteStage("interrupted staging bytes differ; preserve the recorded staging ID")
                self.check()
                return "stage-reused"
            # Deliberately no O_TRUNC, no final-path write, and no claim that
            # O_EXCL works on Apple's NFS client / go-nfs server combination.
            fd = os.open(name, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            with os.fdopen(fd, "wb") as target:
                if os.fstat(target.fileno()).st_size:
                    raise Refusal("staging namespace collision; existing bytes preserved")
                source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(source_fd, "rb") as original:
                    before = fingerprint(os.fstat(original.fileno()))
                    h = hashlib.sha256()
                    for block in iter(lambda: original.read(1024 * 1024), b""):
                        self.check()
                        h.update(block)
                        target.write(block)
                    after = fingerprint(os.fstat(original.fileno()))
                if before != expected["fingerprint"] or before != after or h.hexdigest() != expected["sha256"]:
                    raise Refusal("source changed while writing mounted staging bytes")
                target.flush()
                os.fsync(target.fileno())
                self.check()
            return "staged"

    def publish(self, source, path, expected, temporary=None):
        assert_source(source, expected)
        with self.parent(path, create=True) as (parent, name):
            try:
                current = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            except FileNotFoundError:
                current = None
            if current is not None:
                with os.fdopen(current, "rb") as f:
                    h = hashlib.file_digest(f, "sha256").hexdigest()
                if h == expected["sha256"]:
                    return "reused"
                raise Refusal("publication destination exists with different bytes")
            temporary = temporary or f".gdm-photo-{uuid.uuid4().hex}.tmp"
            safe_name(temporary)
            if not temporary.startswith(".gdm-photo-") or not temporary.endswith(".tmp"):
                raise Refusal("temporary name is not owned by the photo journal")
            try:
                prior = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            except FileNotFoundError:
                prior = None
            if prior is not None:
                with os.fdopen(prior, "rb") as existing:
                    if hashlib.file_digest(existing, "sha256").hexdigest() != expected["sha256"]:
                        raise Refusal("interrupted temporary has incomplete bytes; preserve for explicit reconciliation")
                self.check()
                rename_noreplace(temporary, name, parent, parent)
                self.check()
                return "published"
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                         0o600, dir_fd=parent)
            # Any failure preserves this journal-owned temporary. EIO/rename
            # failure is ambiguous; cleanup must follow reconciliation.
            with os.fdopen(fd, "wb") as target:
                source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(source_fd, "rb") as original:
                    before = fingerprint(os.fstat(original.fileno()))
                    h = hashlib.sha256()
                    for block in iter(lambda: original.read(1024 * 1024), b""):
                        self.check()
                        target.write(block)
                        h.update(block)
                    after = fingerprint(os.fstat(original.fileno()))
                if before != expected["fingerprint"] or before != after or h.hexdigest() != expected["sha256"]:
                    raise Refusal("source changed during mounted publication")
                target.flush()
                os.fsync(target.fileno())
            self.check()
            rename_noreplace(temporary, name, parent, parent)
            self.check()
            return "published"


def validate_webp(path):
    from PIL import Image
    with Image.open(path) as im:
        if im.format != "WEBP" or getattr(im, "n_frames", 1) != 1:
            raise Refusal("adopted output is not a single-frame WebP")
        im.load()
        if max(im.size) > RECIPE["long_edge"]:
            raise Refusal("adopted output exceeds the agreed 2000px limit")
        if im.info.get("exif") or im.info.get("xmp") or im.info.get("icc_profile"):
            raise Refusal("adopted WebP retains metadata; review before adoption")
        return {"width": im.width, "height": im.height}


def convert(source, destination, *, clockwise_degrees=0):
    if type(clockwise_degrees) is not int or clockwise_degrees not in (0, 90, 180, 270):
        raise Refusal("unsupported explicit clockwise correction")
    from PIL import Image, ImageOps
    import pillow_heif
    pillow_heif.register_heif_opener()
    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image)
        if clockwise_degrees:
            image = image.transpose({90: Image.Transpose.ROTATE_270,
                                     180: Image.Transpose.ROTATE_180,
                                     270: Image.Transpose.ROTATE_90}[clockwise_degrees])
        image.thumbnail((RECIPE["long_edge"], RECIPE["long_edge"]), Image.Resampling.LANCZOS)
        image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
        image.save(destination, "WEBP", quality=RECIPE["quality"], method=RECIPE["method"])
    validate_webp(destination)


def codec_versions():
    versions = {}
    for package in ("Pillow", "pillow-heif"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = "unavailable"
    try:
        from PIL import features
        versions["libwebp"] = features.version("webp")
    except ImportError:
        versions["libwebp"] = "unavailable"
    return versions


def local_inventory(root):
    root = Path(root)
    if root.is_symlink() or root.resolve() != root.absolute():
        raise Refusal("SSD source root must be a real directory without symlink ancestors")
    result, total = {}, 0
    for base, dirs, files in os.walk(root, followlinks=False):
        relbase = Path(base).relative_to(root)
        dirs[:] = sorted(d for d in dirs if d != ".gdm-retired")
        if len(relbase.parts) > 4:
            raise Refusal("SSD source tree exceeds bounded depth")
        for name in dirs:
            if (Path(base) / name).is_symlink():
                raise Refusal("SSD source tree contains a directory symlink")
        for name in sorted(files):
            path = Path(base) / name
            rel = path.relative_to(root).as_posix()
            relative(rel)
            st = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode):
                raise Refusal("local source is not a regular file")
            total += st.st_size
            if len(result) >= MAX_FILES or total > MAX_BYTES:
                raise Refusal("SSD corpus exceeds the bounded inventory budget")
            value = hash_file(path)
            result[rel] = value
    return result


def unique_path(desired, sha, occupied):
    relative(desired)
    key = desired.casefold()
    if key not in occupied:
        occupied.add(key)
        return desired
    p = PurePosixPath(desired)
    for length in (12, 20, 64):
        candidate = str(p.with_name(f"{p.stem}-{sha[:length]}{p.suffix}"))
        if candidate.casefold() not in occupied:
            occupied.add(candidate.casefold())
            return candidate
    raise Refusal("content-addressed destination is still ambiguous")


def _derive_alias_bindings(plan):
    """Derive private traceability only from already-recorded byte evidence.

    Remote WebP aliases are limited to evidenced keeper/retirement rows. This
    map does not discover other remote duplicates or establish their lineage.
    """
    entries = plan["entries"]
    keyed = {entry["key"]: entry for entry in entries}
    if len(keyed) != len(entries):
        raise Refusal("alias bindings require unique canonical entry keys")
    inventory = {row["id"]: row for row in plan["remote_inventory"]}
    if len(inventory) != len(plan["remote_inventory"]):
        raise Refusal("alias bindings require unique inventory Drive IDs")
    originals = [entry for entry in entries if entry["kind"] == "original"]
    if len({entry["sha256"] for entry in originals}) != len(originals):
        raise Refusal("alias bindings require one original entry per SHA256")
    local_originals, retirements = {}, {}
    for path, value in plan["local"].items():
        if path.startswith("originals/"):
            local_originals.setdefault(value["sha256"], []).append(path)
    for item in plan["retirement"]:
        if item["reason"] == "verified-webp-copy":
            canonical_entry = keyed.get(item["canonical_key"])
            if (canonical_entry is None or canonical_entry["kind"] != "webp" or
                    canonical_entry["recipe"].get("operation") != "adopt-corrected-webp"):
                raise Refusal("accepted WebP duplicate has no accepted canonical entry")
        retirements.setdefault(item["canonical_key"], []).append(item)
    derivatives = {}
    for entry in entries:
        if entry["kind"] != "webp":
            continue
        original = keyed.get(entry["original_key"])
        if (original is None or original["kind"] != "original" or
                entry["recipe"].get("source_original_sha256") != original["sha256"]):
            raise Refusal("alias WebP linkage does not match its canonical original")
        if original["key"] in derivatives:
            raise Refusal("alias bindings require one accepted WebP entry per original")
        derivatives[original["key"]] = entry

    bindings = {}

    def add(origin, role, path, identifier, sha, original, webp):
        relative(path)
        if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise Refusal("alias byte evidence must be a full SHA256")
        row = {"origin": origin, "role": role, "path": path, "drive_id": identifier,
               "sha256": sha, "original_sha256": original["sha256"],
               "original_key": original["key"], "webp_key": webp["key"] if webp else None}
        identity = (origin, path, identifier)
        if identity in bindings and bindings[identity] != row:
            raise Refusal("one alias has conflicting canonical original evidence")
        bindings[identity] = row

    def add_remote(row, role, sha, original, webp):
        if not isinstance(row.get("id"), str) or not row["id"] or inventory.get(row["id"]) != row:
            raise Refusal("alias Drive identity differs from the immutable inventory")
        add("drive", role, row["path"], row["id"], sha, original, webp)

    for original in originals:
        sha = original["sha256"]
        webp = derivatives.get(original["key"])
        local_paths = sorted(local_originals.get(sha, []))
        original_alias_paths = {str(PurePosixPath(path).relative_to("originals")) for path in local_paths}
        for path in local_paths:
            add("ssd", "original", path, None, sha, original, webp)
        keeper = original["source"]["remote"]
        if keeper is not None:
            if keeper["id"] != original["keep_id"]:
                raise Refusal("original alias keeper differs from its canonical Drive ID")
            add_remote(keeper, "original", sha, original, webp)
            original_alias_paths.add(keeper["path"])
        elif original["keep_id"] is not None:
            raise Refusal("original alias keeper lacks inventory provenance")
        if not local_paths and keeper is None:
            raise Refusal("canonical original has no proven source alias")
        for item in retirements.get(original["key"], []):
            if item["reason"] != "verified-original-copy" or item["sha256"] != sha:
                raise Refusal("retired original alias lacks byte-equivalent evidence")
            add_remote(item["row"], "original", sha, original, webp)
            original_alias_paths.add(item["row"]["path"])

        if webp is None or webp["recipe"].get("operation") != "adopt-corrected-webp":
            continue  # Missing/blocked/generated derivatives are not accepted source aliases.
        adopted = webp["source"]["local"]
        candidates = {str(PurePosixPath("webp") / PurePosixPath(path).relative_to("originals").with_suffix(".webp"))
                      for path in local_paths if PurePosixPath(path).suffix.lower() in PHOTO_EXTENSIONS}
        # Positive reuse follows proven original aliases, not a future
        # content-suffixed name synthesized by collision reconciliation.
        remote_candidates = {str(PurePosixPath("webp") / PurePosixPath(path).with_suffix(".webp")).casefold()
                             for path in original_alias_paths}
        if adopted not in candidates or adopted not in plan["local"] or plan["local"][adopted]["sha256"] != webp["sha256"]:
            raise Refusal("accepted WebP alias lacks its original and local byte evidence")
        for path in sorted(candidates):
            if path in plan["local"]:
                if plan["local"][path]["sha256"] != webp["sha256"]:
                    raise Refusal("accepted WebP aliases contain conflicting byte evidence")
                add("ssd", "accepted-webp", path, None, webp["sha256"], original, webp)
        if webp["keep_id"] is not None:
            keeper = inventory.get(webp["keep_id"])
            if (keeper is None or keeper["path"].casefold() not in remote_candidates or
                    keeper["path"] != webp["path"]):
                raise Refusal("accepted WebP keeper lacks matching inventory provenance")
            add_remote(keeper, "accepted-webp", webp["sha256"], original, webp)
        for item in retirements.get(webp["key"], []):
            if item["reason"] == "verified-webp-copy" and item["sha256"] != webp["sha256"]:
                raise Refusal("accepted WebP duplicate lacks byte-equivalent evidence")
            if item["sha256"] != webp["sha256"]:
                continue  # Superseded different-byte outputs are not aliases.
            if (item["reason"] not in {"superseded-webp", "verified-webp-copy"} or
                    item["row"]["path"].casefold() not in remote_candidates):
                raise Refusal("accepted WebP duplicate lacks matching inventory provenance")
            add_remote(item["row"], "accepted-webp", webp["sha256"], original, webp)
    return sorted(bindings.values(), key=lambda row: (row["origin"], row["path"], row["drive_id"] or ""))


def validate_alias_bindings(plan):
    """Check a complete deterministic map; legacy plans carry no alias evidence."""
    if "alias_bindings_version" not in plan and "alias_bindings" not in plan:
        return
    if type(plan.get("alias_bindings_version")) is not int or plan["alias_bindings_version"] != 1:
        raise Refusal("unsupported or incomplete alias bindings version")
    if not isinstance(plan.get("alias_bindings"), list):
        raise Refusal("alias bindings must be a private ordered list")
    try:
        expected = _derive_alias_bindings(plan)
    except (KeyError, TypeError, AttributeError, ValueError):
        raise Refusal("alias bindings have incomplete provenance evidence") from None
    if plan["alias_bindings"] != expected:
        raise Refusal("alias bindings differ from canonical inventory and byte evidence")


def alias_candidates(plan, reference):
    """Return all original keys for a literal path or a bare basename/stem.

    This resolves no curation grammar or publication policy. A bare token must
    include nested aliases even if a root-level alias also matches exactly.
    """
    validate_alias_bindings(plan)
    relative(reference)
    matches = set()
    for row in plan.get("alias_bindings", []):
        path = PurePosixPath(row["path"])
        if (row["path"] == reference if "/" in reference else reference in {path.name, path.stem}):
            matches.add(row["original_key"])
    return sorted(matches)


def prior_payload_evidence(plan, plan_digest):
    """Extract immutable prior readback evidence, never manufacture it from metadata."""
    if (not isinstance(plan_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", plan_digest)
            or digest(plan) != plan_digest):
        raise Refusal("prior readback plan digest differs")
    validate_alias_bindings(plan)
    inventory = {}
    for row in plan["remote_inventory"]:
        identifier = row.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in inventory:
            raise Refusal("prior readback inventory has ambiguous IDs")
        inventory[identifier] = row
    hashes = {}
    def bind(identifier, sha):
        if identifier is None or sha is None:
            return
        if (identifier not in inventory or not isinstance(sha, str)
                or not re.fullmatch(r"[a-f0-9]{64}", sha)):
            raise Refusal("prior payload evidence is malformed")
        if identifier in hashes and hashes[identifier] != sha:
            raise Refusal("prior payload evidence conflicts for one Drive ID")
        hashes[identifier] = sha
    for entry in plan["entries"]:
        bind(entry.get("keep_id"), entry.get("sha256"))
    for item in plan["retirement"]:
        row = item["row"]
        if inventory.get(row.get("id")) != row:
            raise Refusal("prior retirement row differs from readback inventory")
        bind(row["id"], item["sha256"])
    return inventory, hashes


def reusable_payload_sha(row, prior_rows, prior_hashes):
    identifier = row.get("id")
    if not isinstance(identifier, str) or prior_rows.get(identifier) != row:
        return None
    # Require complete unchanged fingerprints, not just a matching advertised hash.
    if any(not isinstance(row.get(field), str) or not row[field]
           for field in ("id", "parent", "path", "name", "mimeType", "modifiedTime")):
        return None
    if (not isinstance(row.get("size"), str) or not re.fullmatch(r"[0-9]+", row["size"])
            or not isinstance(row.get("md5Checksum"), str)
            or not re.fullmatch(r"[a-fA-F0-9]{32}", row["md5Checksum"])
            or not isinstance(row.get("sha256Checksum"), str)
            or not re.fullmatch(r"[a-fA-F0-9]{64}", row["sha256Checksum"])):
        return None
    known = prior_hashes.get(identifier)
    return known if known is not None and row["sha256Checksum"].lower() == known else None


def make_plan(source_root, mount_point, remote, validator=validate_webp, *, require_curation=False,
              reuse_plan=None, reuse_plan_digest=None):
    """Inventory a whole collection; the operator CLI always requires curation.

    The default retains the pre-catalog API for legacy safety fixtures. There
    is deliberately no command-line switch to publish a new uncurated plan.
    """
    prior_rows, prior_hashes, reused = {}, {}, 0
    if reuse_plan is not None:
        prior_rows, prior_hashes = prior_payload_evidence(reuse_plan, reuse_plan_digest)
        if (reuse_plan.get("org") != ORG or type(reuse_plan.get("version")) is not int
                or reuse_plan.get("version") != 1
                or reuse_plan.get("source_root") != str(Path(source_root).absolute())
                or reuse_plan.get("mount_point") != str(Path(mount_point).absolute())):
            raise Refusal("prior readback plan roots differ")
    elif reuse_plan_digest is not None:
        raise Refusal("prior readback digest requires its immutable plan")
    source_root = Path(source_root)
    local = local_inventory(source_root)
    if not any(p.startswith("originals/") for p in local):
        raise Refusal("SSD corpus has no staged originals")
    root_id = remote.root_folder()
    if reuse_plan is not None and reuse_plan.get("drive_root_id") != root_id:
        raise Refusal("prior readback Drive root differs")
    inventory = remote.inventory(root_id)
    identifiers = [row.get("id") for row in inventory]
    if any(not isinstance(identifier, str) or not identifier for identifier in identifiers) or len(set(identifiers)) != len(identifiers):
        raise Refusal("planning inventory has missing or duplicate IDs")
    dirs = {}
    for row in inventory:
        if row["mimeType"] == FOLDER_MIME:
            dirs.setdefault(row["path"].casefold(), []).append(row)
    if any(len(rows) != 1 for rows in dirs.values()):
        raise Refusal("duplicate Drive directory names require explicit reconciliation")
    files = [r for r in inventory if r["mimeType"] != FOLDER_MIME]
    if any(PurePosixPath(r["path"]).name.startswith(".gdm-stage-") for r in files):
        raise Refusal("unreconciled staging objects exist; resume their existing private plan")
    by_path, hashes, remote_bytes = {}, {}, 0
    for row in files:
        by_path.setdefault(row["path"].casefold(), []).append(row)
        if row["mimeType"] == "application/vnd.google-apps.shortcut":
            raise Refusal("Drive shortcuts require an explicit provenance decision")
        if row["mimeType"].startswith("application/vnd.google-apps."):
            continue  # Preserve native documents and shortcuts; never follow them.
        remote_bytes += int(row.get("size") or 0)
        if remote_bytes > MAX_BYTES:
            raise Refusal("Drive corpus exceeds the bounded readback byte budget")
        reused_sha = reusable_payload_sha(row, prior_rows, prior_hashes)
        if reused_sha is None:
            hashes[row["id"]] = getattr(remote, "planning_sha256", remote.sha256)(row)
        else:
            hashes[row["id"]] = reused_sha
            reused += 1
    originals = [r for r in files if not r["path"].startswith("webp/") and r["id"] in hashes
                 and not (require_curation and r["path"].casefold() in CURATION_PATHS)]
    blockers = []
    for rows in by_path.values():
        sources = [r for r in rows if r in originals]
        if not sources:
            continue
        counts = {}
        for row in sources:
            counts[hashes[row["id"]]] = counts.get(hashes[row["id"]], 0) + 1
        first_sha = hashes[min(sources, key=lambda r: r["id"])["id"]]
        if any(sha != first_sha and count > 1 for sha, count in counts.items()):
            blockers.append("multiple duplicated content groups share one original pathname; reconcile those IDs before mount reads")
    groups = {}
    for row in originals:
        groups.setdefault(hashes[row["id"]], {"remote": [], "local": []})["remote"].append(row)
    for path, data in local.items():
        if path.startswith("originals/"):
            groups.setdefault(data["sha256"], {"remote": [], "local": []})["local"].append(path)
    occupied = {row["path"].casefold() for row in inventory}
    entries, retirement, warnings = [], {}, []
    original_entries = []
    for sha, group in sorted(groups.items()):
        group["remote"].sort(key=lambda r: (r["path"], r["id"]))
        group["local"].sort()
        keeper = group["remote"][0] if group["remote"] else None
        local_path = group["local"][0] if group["local"] else None
        wanted = keeper["path"] if keeper else str(PurePosixPath(local_path).relative_to("originals"))
        # Keep one stable Drive ID at a colliding name; rename other distinct
        # originals by ID into sibling content-suffixed paths. Unique IDs are
        # never retired: sharing and references continue to identify those bytes.
        at_name = sorted(by_path.get(wanted.casefold(), []), key=lambda r: r["id"])
        keep_name = keeper and at_name and keeper["id"] == at_name[0]["id"]
        target = wanted if keep_name else unique_path(wanted, sha, occupied)
        entry = {"kind": "original", "path": target, "sha256": sha,
                 "recipe": {"operation": "preserve-original-bytes"},
                 "source": {"local": local_path, "remote": keeper},
                 "keep_id": keeper["id"] if keeper else None,
                 "rename_from": keeper if keeper and not keep_name else None}
        entry["key"] = digest(entry)
        entries.append(entry)
        original_entries.append((entry, group))
        for row in group["remote"]:
            if row["id"] != entry["keep_id"]:
                retirement[row["id"]] = {"row": row, "sha256": sha, "reason": "verified-original-copy",
                                         "canonical_key": entry["key"]}
    for original, group in original_entries:
        if PurePosixPath(original["path"]).suffix.lower() not in PHOTO_EXTENSIONS:
            continue  # Preserve videos, Live Photo companions and sidecars unchanged.
        candidates = []
        for source in group["local"]:
            candidate = str(PurePosixPath("webp") / PurePosixPath(source).relative_to("originals").with_suffix(".webp"))
            if candidate in local:
                candidates.append(candidate)
        candidate_hashes = {local[c]["sha256"] for c in candidates}
        if len(candidate_hashes) > 1:
            raise Refusal("same original has conflicting corrected WebP candidates")
        adopted = sorted(candidates)[0] if candidates else None
        # Positive reuse is limited to derivative paths of byte-proven source
        # aliases, not synthesized names or unrelated equal-byte WebPs.
        source_paths = {*(r["path"] for r in group["remote"]),
                        *(str(PurePosixPath(p).relative_to("originals")) for p in group["local"])}
        derivative_paths = {str(PurePosixPath("webp") / PurePosixPath(p).with_suffix(".webp")).casefold()
                            for p in source_paths}
        alias_outputs = [r for path in sorted(derivative_paths) for r in by_path.get(path, [])
                         if r["id"] in hashes]
        if adopted:
            # Same-stem, different-original formats cannot claim one derivative.
            stems = [p for p in local if p.startswith("originals/") and PurePosixPath(p).suffix.lower() in PHOTO_EXTENSIONS and
                     str(PurePosixPath("webp") / PurePosixPath(p).relative_to("originals").with_suffix(".webp")) == adopted]
            if len({local[p]["sha256"] for p in stems}) != 1:
                raise Refusal("same-stem source collision makes adopted WebP provenance ambiguous")
            dimensions = validator(source_root / adopted)
            sha = local[adopted]["sha256"]
            wanted = adopted
            recipe = {**RECIPE, "operation": "adopt-corrected-webp", **dimensions,
                      "source_original_sha256": original["sha256"]}
        else:
            # A missing SSD derivative is not evidence that Drive lacks the
            # accepted correction. Check every known original alias, including
            # its pre-collision name, before proposing an EXIF-only re-encode.
            missing_output_paths = derivative_paths | {
                str(PurePosixPath("webp") / PurePosixPath(original["path"]).with_suffix(".webp")).casefold()}
            existing = [r for path in sorted(missing_output_paths) for r in by_path.get(path, [])
                        if r["id"] in hashes]
            if existing:
                evidence = ", ".join(f"{r['path']} (ID {r['id']}, SHA256 {hashes[r['id']]})"
                                     for r in sorted(existing, key=lambda row: (row["path"], row["id"])))
                blockers.append(f"remote WebP requires accepted-output decision for {original['path']}: {evidence}; "
                                "resolve ID/hash-bound adoption or orientation before regeneration or retirement")
                continue
            sha = None
            wanted = str(PurePosixPath("webp") / PurePosixPath(original["path"]).with_suffix(".webp"))
            recipe = {**RECIPE, "operation": "encode-original",
                      "source_original_sha256": original["sha256"], "codec_versions": codec_versions()}
        matches = [r for r in alias_outputs if hashes[r["id"]] == sha] if adopted else []
        keeper = min(matches, key=lambda r: (r["path"] != wanted,
                                            r["path"].casefold() != wanted.casefold(),
                                            r["path"], r["id"])) if matches else None
        target = keeper["path"] if keeper else unique_path(wanted, sha or digest(recipe), occupied)
        entry = {"kind": "webp", "path": target, "sha256": sha, "recipe": recipe,
                 "source": {"local": adopted, "remote": None},
                 "original_key": original["key"], "keep_id": keeper["id"] if keeper else None}
        entry["key"] = digest(entry)
        entries.append(entry)
        for row in by_path.get(wanted.casefold(), []):
            if row["id"] in hashes and row["id"] != entry["keep_id"]:
                retirement[row["id"]] = {"row": row, "sha256": hashes[row["id"]],
                                         "reason": "superseded-webp", "canonical_key": entry["key"]}
        for row in matches:
            if row["id"] != entry["keep_id"]:
                retirement[row["id"]] = {"row": row, "sha256": sha,
                                         "reason": "verified-webp-copy", "canonical_key": entry["key"]}
    for name in CURATION_NAMES:
        if name in local and f"webp/{name}" in local and local[name]["sha256"] != local[f"webp/{name}"]["sha256"]:
            blockers.append(f"conflicting provenance copies: {name}")
        source = name if name in local else f"webp/{name}"
        if source not in local:
            warnings.append(f"missing provenance sidecar: {name}")
            continue
        wanted, sha = f"webp/{name}", local[source]["sha256"]
        copies = [r for path in (name, wanted) for r in by_path.get(path, [])]
        if require_curation and any(r["path"] not in (name, wanted) for r in copies):
            blockers.append(f"case-variant provenance path requires explicit reconciliation: {name}")
        if require_curation and any(hashes.get(r["id"]) != sha for r in copies):
            blockers.append(f"Drive/SSD provenance differs: {name}; preserve every copy for authority review")
        matches = [r for r in by_path.get(wanted.casefold(), []) if hashes.get(r["id"]) == sha]
        keeper = min(matches, key=lambda r: r["id"]) if matches else None
        entry = {"kind": "provenance", "path": wanted if keeper else unique_path(wanted, sha, occupied),
                 "sha256": sha, "recipe": {"operation": "preserve-provenance-bytes"},
                 "source": {"local": source, "remote": None},
                 "keep_id": keeper["id"] if keeper else None}
        entry["key"] = digest(entry)
        entries.append(entry)
        if require_curation:
            for row in copies:
                if row["id"] != entry["keep_id"] and hashes.get(row["id"]) == sha:
                    retirement[row["id"]] = {"row": row, "sha256": sha,
                                             "reason": "verified-provenance-copy",
                                             "canonical_key": entry["key"]}
    judged = source_root / "orientation-judged.csv"
    if "orientation-judged.csv" in local and not require_curation:
        with judged.open(newline="") as f:
            rows = list(csv.DictReader(f))
        names = {PurePosixPath(p).stem for p in local if p.startswith("originals/")}
        extra = sorted(r["name"] for r in rows if r.get("name") not in names)
        if extra:
            warnings.append("orientation provenance contains non-source rows: " + ", ".join(extra))
    # A proven alias can reuse another original's wanted output pathname.
    # That keeper must not also be superseded by the second original's
    # different accepted bytes. Keep both canonical outputs; the runtime
    # retirement guard remains an independent last line of defense.
    canonical_ids = {entry["keep_id"] for entry in entries if entry["keep_id"] is not None}
    retirement = {identifier: item for identifier, item in retirement.items() if identifier not in canonical_ids}
    covered = {e["sha256"] for e in entries if e["sha256"]}
    local_retirement = [p for p in sorted(local) if (PurePosixPath(p).parts[0] in RETIRABLE_DIRS
                        or (require_curation and p in CURATION_NAMES))
                        and local[p]["sha256"] in covered]
    unresolved = [{"path": p, "reason": "no verified canonical byte-equivalent; preserve locally"}
                  for p in sorted(local) if PurePosixPath(p).parts[0] in RETIRABLE_DIRS and p not in local_retirement]
    source_stat = source_root.stat()
    plan = {"version": 1, "org": ORG, "created_at": time.time(), "source_root": str(source_root.absolute()),
            "source_identity": {"dev": source_stat.st_dev, "ino": source_stat.st_ino},
            "mount_point": str(Path(mount_point).absolute()), "drive_root_id": root_id,
            "local": local, "remote_inventory": inventory, "entries": entries,
            "retirement": sorted(retirement.values(), key=lambda r: r["row"]["id"]),
            "local_retirement": local_retirement, "unresolved": unresolved,
            "warnings": warnings, "blockers": blockers}
    plan["alias_bindings_version"] = 1
    if reuse_plan is not None:
        plan["readback_reuse"] = {"source_plan_digest": reuse_plan_digest, "reused_objects": reused}
    plan["alias_bindings"] = _derive_alias_bindings(plan)
    if require_curation:
        attach_curation(plan, source_root)
    if remote.root_folder() != root_id or remote.inventory(root_id) != inventory:
        raise Refusal("Drive inventory changed during planning; no plan saved")
    return plan


def attach_curation(plan, source_root):
    """Bind the reviewed sidecar bytes and a deferred catalog recipe."""
    from gftb_curation import CurationError, parse_curation
    sources = {}
    for name in CURATION_NAMES:
        matches = [e for e in plan["entries"] if e["kind"] == "provenance"
                   and PurePosixPath(e["path"]).name == name]
        if len(matches) != 1:
            plan["blockers"].append(f"missing canonical curation document: {name}")
            continue
        entry = matches[0]
        path = Path(source_root) / relative(entry["source"]["local"])
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_SIDECAR_BYTES:
                raise Refusal("curation document is not a bounded regular file")
            raw = source.read(MAX_SIDECAR_BYTES + 1)
            after = os.fstat(source.fileno())
        if (len(raw) > MAX_SIDECAR_BYTES or fingerprint(before) != fingerprint(after)
                or hashlib.sha256(raw).hexdigest() != entry["sha256"]):
            raise Refusal("curation document changed after inventory")
        sources[name] = {"text": raw.decode("utf-8"), "sha256": entry["sha256"],
                         "entry_key": entry["key"]}
    plan["curation_sources"] = sources
    if len(sources) != len(CURATION_NAMES):
        return
    try:
        plan["curation"] = parse_curation(
            sources[CURATION_NAMES[0]]["text"].encode("utf-8"),
            sources[CURATION_NAMES[1]]["text"].encode("utf-8"), plan["alias_bindings"])
    except CurationError as error:
        plan["blockers"].append(f"curation needs review: {error}")
        return
    if any(e["kind"] == "webp" and e["recipe"]["operation"] == "encode-original"
           for e in plan["entries"]):
        plan["blockers"].append("missing accepted derivative requires an orientation review before publication")
    if any(r["path"].casefold() == CATALOG_PATH for r in plan["remote_inventory"]):
        plan["blockers"].append("existing catalog requires identity/content reconciliation; preserve it")
    if CATALOG_PATH in plan["local"] or "gftb-catalog.json" in plan["local"]:
        plan["blockers"].append("existing SSD catalog requires reconciliation; preserve it")
    entry = {"kind": "catalog", "path": CATALOG_PATH, "sha256": None,
             "recipe": {"operation": "curation-catalog", "version": 1,
                        "curation_sha256": digest(plan["curation"]),
                        "entry_keys": sorted(e["key"] for e in plan["entries"])},
             "source": {"local": None, "remote": None}, "keep_id": None}
    entry["key"] = digest(entry)
    plan["entries"].append(entry)


def validate_curation(plan):
    if "curation_sources" not in plan and "curation" not in plan:
        return  # Immutable legacy manifests remain resumable without new proof.
    from gftb_curation import CurationError, parse_curation
    try:
        sources = plan["curation_sources"]
        raw = []
        for name in CURATION_NAMES:
            value = sources[name]
            data = value["text"].encode("utf-8")
            entries = [e for e in plan["entries"] if e["key"] == value["entry_key"]]
            if (len(data) > MAX_SIDECAR_BYTES or hashlib.sha256(data).hexdigest() != value["sha256"]
                    or len(entries) != 1 or entries[0]["kind"] != "provenance"
                    or entries[0]["path"] != f"webp/{name}"
                    or entries[0]["sha256"] != value["sha256"]):
                raise Refusal("curation byte provenance differs from canonical entries")
            raw.append(data)
        expected = parse_curation(*raw, plan["alias_bindings"])
        catalogs = [e for e in plan["entries"] if e["kind"] == "catalog"]
        recipe = {"operation": "curation-catalog", "version": 1,
                  "curation_sha256": digest(expected),
                  "entry_keys": sorted(e["key"] for e in plan["entries"] if e["kind"] != "catalog")}
        if (expected != plan["curation"] or len(catalogs) != 1 or catalogs[0]["recipe"] != recipe
                or catalogs[0]["path"] != CATALOG_PATH or catalogs[0]["keep_id"] is not None
                or catalogs[0]["sha256"] is not None):
            raise Refusal("catalog recipe differs from immutable curation evidence")
    except (KeyError, TypeError, CurationError):
        raise Refusal("curation evidence is incomplete or invalid") from None


def catalog_content(plan, verified):
    from gftb_curation import build_catalog
    validate_curation(plan)
    provenance = [{field: verified[e["key"]][field] for field in ("path", "id", "sha256")}
                  for e in plan["entries"] if e["kind"] == "provenance"]
    return build_catalog(plan["curation"], verified, plan["drive_root_id"], provenance)


@contextlib.contextmanager
def materialized(entry, plan, remote, state, mount=None):
    source = entry["source"]
    if source["local"]:
        path = Path(plan["source_root"]) / relative(source["local"])
        with tempfile.TemporaryDirectory(prefix="snapshot-", dir=state.root) as temp:
            target = Path(temp) / ("source" + path.suffix)
            snapshot(path, plan["local"][source["local"]], target)
            yield target
    elif source["remote"]:
        with tempfile.TemporaryDirectory(prefix="source-", dir=state.root) as temp:
            path = Path(temp) / ("original" + PurePosixPath(entry["path"]).suffix)
            if mount is None:
                raise Refusal("original workload reads require the guarded mount")
            mount.read_to(entry["path"], path, entry["sha256"])
            if hash_file(path)["sha256"] != entry["sha256"]:
                raise Refusal("Drive source no longer matches planned SHA256")
            yield path
    else:
        raise Refusal("entry has no materializable source")


def namespace(plan, remote):
    if remote.root_folder() != plan["drive_root_id"]:
        raise Refusal("GFTB Stuff Drive root identity changed")
    current = remote.inventory(plan["drive_root_id"])
    planned_dirs = {r["path"]: r["id"] for r in plan["remote_inventory"] if r["mimeType"] == FOLDER_MIME}
    current_dirs = {}
    for row in current:
        if row["mimeType"] == "application/vnd.google-apps.shortcut":
            raise Refusal("Drive namespace contains an unreviewed shortcut")
        if row["mimeType"] == FOLDER_MIME:
            current_dirs.setdefault(row["path"].casefold(), []).append(row)
    guarded_paths = [e["path"] for e in plan["entries"]] + [r["row"]["path"] for r in plan["retirement"]]
    for path in guarded_paths:
        for parent in PurePosixPath(path).parents:
            if str(parent) == ".":
                continue
            rows = current_dirs.get(str(parent).casefold(), [])
            if len(rows) > 1:
                raise Refusal("destination ancestor is ambiguous by Drive ID")
            if str(parent) in planned_dirs and (len(rows) != 1 or rows[0]["id"] != planned_dirs[str(parent)]):
                raise Refusal("destination ancestor Drive ID changed")
    return current


def apply_plan(plan, key, state, remote, mount, encoder=convert, validator=validate_webp):
    validate_alias_bindings(plan)
    if plan.get("blockers"):
        raise Refusal("plan contains unresolved provenance decisions")
    validate_curation(plan)
    if any(event.get("event") == "retired" for event in state.events(key)):
        with mount:
            verified = verify_plan(plan, key, state, remote)
        state.event(key, "apply-noop", entries=len(verified), reason="retired-plan")
        return len(verified)
    media = [entry for entry in plan["entries"] if entry["kind"] != "catalog"]
    _apply_entries(plan, key, state, remote, mount, media, encoder, validator)
    catalogs = [entry for entry in plan["entries"] if entry["kind"] == "catalog"]
    if catalogs:
        # IDs do not exist until media promotion. Verify those exact objects
        # first, then prepare a deterministic second publication stage.
        verified = verify_plan(plan, key, state, remote, include_catalog=False)
        payload = catalog_content(plan, verified)
        _apply_entries(plan, key, state, remote, mount, catalogs, encoder, validator,
                       catalog_payload=payload)
        verify_plan(plan, key, state, remote)
    return len(plan["entries"])


def _apply_entries(plan, key, state, remote, mount, selected, encoder, validator,
                   catalog_payload=None):
    namespace(plan, remote)
    remote.uploads_empty()
    entries = {entry["key"]: entry for entry in plan["entries"]}
    publications = []
    with mount:
        # Reconcile all same-name/different-byte originals before accessing their
        # pathname view. These metadata-only API moves preserve each original ID.
        for entry in selected:
            row = entry.get("rename_from")
            if row is None:
                continue
            current = namespace(plan, remote)
            found = [r for r in current if r["id"] == row["id"]]
            if len(found) != 1 or remote.sha256(found[0]) != entry["sha256"]:
                raise Refusal("original rename source changed")
            if found[0]["path"] == entry["path"]:
                state.event(key, "original-renamed", id=row["id"], path=entry["path"], reconciled=True)
                continue
            if found[0]["path"] != row["path"] or found[0]["parent"] != row["parent"]:
                raise Refusal("original rename source moved outside the approved operation")
            state.event(key, "original-rename-started", id=row["id"], path=entry["path"])
            remote.rename(row, entry["path"])
            after = [r for r in namespace(plan, remote) if r["id"] == row["id"]]
            if len(after) != 1 or after[0]["path"] != entry["path"] or after[0]["parent"] != row["parent"] or remote.sha256(after[0]) != entry["sha256"]:
                raise Refusal("original ID-preserving rename requires reconciliation")
            remote.refresh(str(PurePosixPath(entry["path"]).parent))
            state.event(key, "original-renamed", id=row["id"], path=entry["path"])
        for entry in selected:
            namespace(plan, remote)
            if entry["keep_id"]:
                if entry["kind"] == "original":
                    with tempfile.TemporaryDirectory(prefix="mount-proof-", dir=state.root) as temp:
                        mount.read_to(entry["path"], Path(temp) / "original", entry["sha256"])
                state.event(key, "kept", entry=entry["key"], remote_id=entry["keep_id"])
                continue
            with contextlib.ExitStack() as stack:
                if entry["kind"] == "catalog":
                    if not isinstance(catalog_payload, bytes):
                        raise Refusal("catalog requires independently verified media identities")
                    temp = stack.enter_context(tempfile.TemporaryDirectory(prefix="catalog-", dir=state.root))
                    path = Path(temp) / "catalog.json"
                    with path.open("xb") as output:
                        output.write(catalog_payload)
                    path.chmod(0o400)
                    expected = hash_file(path)
                elif entry["kind"] == "webp" and not entry["source"]["local"]:
                    if entry["recipe"]["codec_versions"] != codec_versions():
                        raise Refusal("encoder library versions changed since planning")
                    original = entries[entry["original_key"]]
                    source = stack.enter_context(materialized(original, plan, remote, state, mount))
                    source_before = hash_file(source)
                    temp = stack.enter_context(tempfile.TemporaryDirectory(prefix="encode-", dir=state.root))
                    path = Path(temp) / "output.webp"
                    encoder(source, path)
                    assert_source(source, source_before)
                    validator(path)
                    expected = hash_file(path)
                else:
                    path = stack.enter_context(materialized(entry, plan, remote, state, mount))
                    if entry["kind"] == "webp":
                        validator(path)
                    expected = hash_file(path)
                    if expected["sha256"] != entry["sha256"]:
                        raise Refusal("materialized bytes differ from planned SHA256")
                # Persist output identity BEFORE publication: a crash after rename
                # can then be reconciled independently without trusting filenames.
                previous = [e for e in state.events(key) if e.get("event") == "prepared" and
                            e.get("entry") == entry["key"]]
                if previous and previous[-1]["sha256"] != expected["sha256"]:
                    raise Refusal("encoder output changed across resume; create a new plan")
                state.event(key, "prepared", entry=entry["key"], sha256=expected["sha256"],
                            recipe=digest(entry["recipe"]))
                intents = [e for e in state.events(key) if e.get("event") == "stage-intent" and e.get("entry") == entry["key"]]
                stage_path = intents[-1]["path"] if intents else str(PurePosixPath(entry["path"]).with_name(f".gdm-stage-{uuid.uuid4().hex}.tmp"))
                current = namespace(plan, remote)
                known = [e for e in state.events(key) if e.get("event") in {"stage-verified", "promoted"}
                         and e.get("entry") == entry["key"]]
                finals = [r for r in current if r["path"].casefold() == entry["path"].casefold()]
                if finals:
                    if len(finals) != 1 or not known or finals[0]["id"] != known[-1]["id"] or remote.sha256(finals[0]) != expected["sha256"]:
                        raise Refusal("canonical destination is occupied by an unapproved or ambiguous ID")
                    state.event(key, "promoted", entry=entry["key"], id=finals[0]["id"],
                                sha256=expected["sha256"], path=entry["path"], reconciled=True)
                    continue
                stages = [r for r in current if r["path"].casefold() == stage_path.casefold()]
                if len(stages) > 1 or (known and stages and stages[0]["id"] != known[-1]["id"]):
                    raise Refusal("staging name resolves to ambiguous or substituted Drive IDs")
                state.event(key, "stage-intent", entry=entry["key"], path=stage_path,
                            target=entry["path"], sha256=expected["sha256"])
                try:
                    outcome = mount.stage(path, stage_path, expected)
                except IncompleteStage:
                    # Never truncate, unlink or overwrite a partial NFS write.
                    # A fresh unpredictable sibling resumes useful work while
                    # the exact old name/ID remains on the private evidence list.
                    current = namespace(plan, remote)
                    old = [r for r in current if r["path"] == stage_path]
                    if len(old) > 1:
                        raise Refusal("incomplete staging name has ambiguous Drive IDs")
                    state.event(key, "incomplete-stage-preserved", entry=entry["key"], path=stage_path,
                                ids=[r["id"] for r in old])
                    stage_path = str(PurePosixPath(entry["path"]).with_name(f".gdm-stage-{uuid.uuid4().hex}.tmp"))
                    state.event(key, "stage-intent", entry=entry["key"], path=stage_path,
                                target=entry["path"], sha256=expected["sha256"])
                    outcome = mount.stage(path, stage_path, expected)
                state.event(key, outcome, entry=entry["key"], path=stage_path, sha256=expected["sha256"])
                publications.append((entry, expected["sha256"], stage_path))
    # One bounded check, no waiting or polling. A pending upload leaves all
    # staged bytes and intent durable; the same apply command safely resumes.
    remote.uploads_empty()
    for entry, expected_sha, stage_path in publications:
        current = namespace(plan, remote)
        found = [r for r in current if r["path"].casefold() == stage_path.casefold()]
        if len(found) != 1 or found[0]["path"] != stage_path:
            raise Refusal("uploaded staging object is missing or ambiguous by Drive ID")
        row = found[0]
        if remote.sha256(row) != expected_sha:
            raise Refusal("uploaded staging bytes failed independent Drive SHA256 verification")
        state.event(key, "stage-verified", entry=entry["key"], id=row["id"],
                    sha256=expected_sha, path=stage_path, parent=row["parent"], target=entry["path"])
        current = namespace(plan, remote)
        if any(r["path"].casefold() == entry["path"].casefold() for r in current):
            raise Refusal("final destination was created concurrently; preserve staging and all existing IDs")
        state.event(key, "promotion-started", entry=entry["key"], id=row["id"], path=entry["path"],
                    sha256=expected_sha, parent=row["parent"])
        remote.rename(row, entry["path"])
        after = [r for r in namespace(plan, remote) if r["path"].casefold() == entry["path"].casefold()]
        if len(after) != 1 or after[0]["id"] != row["id"] or after[0]["parent"] != row["parent"] or after[0]["path"] != entry["path"] or remote.sha256(after[0]) != expected_sha:
            raise Refusal("promotion identity, parent, name or bytes are ambiguous; preserve all IDs for reconciliation")
        remote.refresh(str(PurePosixPath(entry["path"]).parent))
        state.event(key, "promoted", entry=entry["key"], id=row["id"],
                    sha256=expected_sha, path=entry["path"])
    return len(plan["entries"])


def journaled_canonical(entry, expected_sha, events):
    """Bind new canonical bytes to the staged Drive ID and approved target.

    A completed promotion receipt is sufficient only with its staged-ID proof.
    A promotion-started receipt plus the same proof also permits read-only
    reconciliation after a successful move whose final receipt was interrupted.
    """
    intents, staged, candidates = {}, {}, {}
    for event in events:
        if event.get("entry") != entry["key"]:
            continue
        action = event.get("event")
        if action == "stage-intent" and event.get("sha256") == expected_sha and event.get("target") == entry["path"]:
            intents[event.get("path")] = event
        elif action == "stage-verified" and event.get("sha256") == expected_sha:
            if event.get("path") not in intents or not isinstance(event.get("id"), str) or not event["id"]:
                continue
            if not isinstance(event.get("parent"), str) or not event["parent"]:
                continue
            if event.get("target", entry["path"]) != entry["path"]:
                continue
            staged[event["id"]] = event
        elif action in {"promotion-started", "promoted"} and event.get("path") == entry["path"]:
            identifier = event.get("id")
            if identifier not in staged:
                continue
            if action == "promoted" and event.get("sha256") != expected_sha:
                continue
            if action == "promotion-started" and event.get("sha256", expected_sha) != expected_sha:
                continue
            if event.get("parent", staged[identifier]["parent"]) != staged[identifier]["parent"]:
                continue
            candidates[identifier] = staged[identifier]["parent"]
    if len(candidates) != 1:
        raise Refusal("new canonical target lacks unambiguous journaled publication ID evidence")
    identifier, parent = next(iter(candidates.items()))
    return identifier, parent


def verify_plan(plan, key, state, remote, *, include_catalog=True):
    validate_alias_bindings(plan)
    if plan.get("blockers"):
        raise Refusal("plan contains unresolved provenance decisions")
    validate_curation(plan)
    remote.uploads_empty()
    rows = namespace(plan, remote)
    events = state.events(key)
    verified = {}
    # Catalog verification is last even if the immutable entries are reordered.
    selected = [e for e in plan["entries"] if e["kind"] != "catalog"]
    if include_catalog:
        selected += [e for e in plan["entries"] if e["kind"] == "catalog"]
    for entry in selected:
        expected = entry["sha256"]
        if expected is None:
            prepared = [e for e in events if e.get("event") == "prepared" and e.get("entry") == entry["key"]
                        and e.get("recipe") == digest(entry["recipe"])]
            if not prepared:
                raise Refusal("generated output has not been prepared under this recipe")
            expected = prepared[-1]["sha256"]
            if len({e["sha256"] for e in prepared}) != 1:
                raise Refusal("prepared output hashes conflict across resume")
        if entry["kind"] == "catalog":
            if hashlib.sha256(catalog_content(plan, verified)).hexdigest() != expected:
                raise Refusal("catalog bytes differ from verified canonical IDs and curation")
        if entry["keep_id"]:
            identifier, parent = entry["keep_id"], None
            approved_aliases = {item["row"]["id"]: item["row"] for item in plan["retirement"]
                                if item["canonical_key"] == entry["key"] and
                                item["row"]["path"].casefold() == entry["path"].casefold()}
            for alias in rows:
                if alias["path"].casefold() != entry["path"].casefold() or alias["id"] == identifier:
                    continue
                approved = approved_aliases.get(alias["id"])
                if approved is None or alias["path"] != approved["path"] or alias["parent"] != approved["parent"]:
                    raise Refusal("canonical Drive target has an unplanned same-name alias")
                # Manifest-listed duplicates may remain until quarantine, but
                # their identity and fingerprint must still match that plan.
                remote.current(approved)
            found = [r for r in rows if r["path"] == entry["path"] and r["id"] == identifier]
        else:
            identifier, parent = journaled_canonical(entry, expected, events)
            found = [r for r in rows if r["path"].casefold() == entry["path"].casefold()]
        if len(found) != 1:
            raise Refusal("canonical Drive target is missing or ambiguous by ID")
        if found[0]["id"] != identifier or found[0]["path"] != entry["path"] or (parent is not None and found[0]["parent"] != parent):
            raise Refusal("canonical Drive target differs from its journaled ID, parent or target")
        if remote.sha256(found[0]) != expected:
            raise Refusal("independent Drive SHA256 does not match canonical bytes")
        verified[entry["key"]] = {"id": found[0]["id"], "sha256": expected,
                                  "path": entry["path"], "row": found[0]}
    remote.uploads_empty()
    alias_evidence = ({"alias_bindings_sha256": digest(plan["alias_bindings"])}
                      if "alias_bindings_version" in plan else {})
    state.event(key, "verified" if include_catalog else "media-verified", entries=verified, **alias_evidence)
    return verified


def retire_plan(plan, key, approved_digest, state, remote):
    if approved_digest != key:
        raise Refusal("retirement requires explicit approval of the exact full plan digest")
    verified = verify_plan(plan, key, state, remote)
    root = Path(plan["source_root"])
    if root.resolve() != root.absolute():
        raise Refusal("retirement root traverses a symlink")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if {"dev": st.st_dev, "ino": st.st_ino} != plan["source_identity"]:
            raise Refusal("retirement root filesystem identity changed")
        return _retire_verified(plan, key, state, remote, verified, fd)
    finally:
        os.close(fd)


def _retire_verified(plan, key, state, remote, verified, root_fd):
    keep_ids = {e["id"] for e in verified.values()}
    root = Path(plan["source_root"])
    quarantine = root / ".gdm-retired" / key
    paths = plan["local_retirement"]
    # Revalidate every local candidate before moving even one file. Resumes
    # recognize only the exact already-retired path and matching planned bytes.
    for rel in paths:
        moved = False
        try:
            with anchored_parent(root_fd, rel) as (parent, name):
                actual = hash_at(parent, name)
        except FileNotFoundError:
            moved = True
            with anchored_parent(root_fd, f".gdm-retired/{key}/{rel}") as (parent, name):
                actual = hash_at(parent, name)
        expected = plan["local"][rel]
        if actual["sha256"] != expected["sha256"] or actual["fingerprint"]["size"] != expected["fingerprint"]["size"]:
            raise Refusal("retirement source changed since planning")
        if not moved and actual["fingerprint"] != expected["fingerprint"]:
            raise Refusal("retirement source identity changed since planning")
    for item in plan["retirement"]:
        namespace(plan, remote)
        remote.current(verified[item["canonical_key"]]["row"])
        row = item["row"]
        if row["id"] in keep_ids:
            raise Refusal("retirement manifest includes a verified canonical Drive ID")
        moved = remote.retired(row, key)
        if moved is not None:
            if remote.sha256(moved) != item["sha256"]:
                raise Refusal("quarantined Drive object content changed")
            state.event(key, "remote-retired", id=row["id"], reason=item["reason"], reconciled=True,
                        original_parent=row["parent"], quarantine_parent=moved["parent"])
            continue
        if remote.sha256(row) != item["sha256"]:
            raise Refusal("Drive retirement source changed")
        state.event(key, "remote-retire-started", id=row["id"])
        remote.retire(row, key)
        moved = remote.retired(row, key)
        if moved is None or remote.sha256(moved) != item["sha256"]:
            raise Refusal("Drive quarantine move needs reconciliation before further retirement")
        state.event(key, "remote-retired", id=row["id"], reason=item["reason"],
                    original_parent=row["parent"], quarantine_parent=moved["parent"])
    for rel in paths:
        expected_sha = plan["local"][rel]["sha256"]
        if "curation" in plan and rel in CURATION_PATHS:
            provenance_key = plan["curation_sources"][PurePosixPath(rel).name]["entry_key"]
            proof = verified.get(provenance_key)
            canonical_rows = [proof["row"]] if proof and proof["sha256"] == expected_sha else []
        else:
            canonical_rows = [entry["row"] for entry in verified.values() if entry["sha256"] == expected_sha]
        if not canonical_rows:
            raise Refusal("local retirement lacks a verified byte-equivalent canonical object")
        # Remote retirement may have taken arbitrarily long. Re-prove the
        # exact canonical object before hashing and moving each local copy.
        namespace(plan, remote)
        canonical_row = canonical_rows[0]
        remote.current(canonical_row)
        if remote.sha256(canonical_row) != expected_sha:
            raise Refusal("canonical bytes changed before local retirement")
        with anchored_parent(root_fd, rel) as (source_fd, source_name):
            try:
                actual = hash_at(source_fd, source_name)
            except FileNotFoundError:
                continue
            if actual != plan["local"][rel]:
                raise Refusal("local retirement source changed immediately before move")
            with anchored_parent(root_fd, f".gdm-retired/{key}/{rel}", create=True) as (dest_fd, dest_name):
                if fingerprint(os.stat(source_name, dir_fd=source_fd, follow_symlinks=False)) != actual["fingerprint"]:
                    raise Refusal("local retirement source identity changed before rename")
                rename_noreplace(source_name, dest_name, source_fd, dest_fd)
                after = hash_at(dest_fd, dest_name)
                if after["sha256"] != actual["sha256"]:
                    raise Refusal("local quarantine move needs reconciliation")
        state.event(key, "local-retired", path=rel)
    state.event(key, "retired", local_files=len(paths), remote_objects=len(plan["retirement"]))
    return {"local_files": len(paths), "remote_objects": len(plan["retirement"]),
            "quarantine": str(quarantine), "status": "recoverable-quarantine",
            "space_reclaimed": False, "permanent_deletion": False,
            "unresolved_files": len(plan.get("unresolved", [])), "single_copy_complete": False}


def configuration(settings_path, state_override):
    with Path(settings_path).open() as f:
        settings = json.load(f)
    units = [u for u in settings.get("units", []) if u.get("org") == ORG]
    if len(units) != 1:
        raise Refusal("effective settings must resolve exactly one Sulliwood unit")
    unit = units[0]
    point = Path(unit["point"]).expanduser().absolute()
    state_root = Path(settings.get("stateDir", Path(settings_path).parent)).expanduser().absolute()
    photo_state = Path(state_override).expanduser().absolute() if state_override else state_root / "photos" / ORG
    excluded = [SSD_ROOT, point, Path(settings.get("cacheRoot", "/Volumes/TinylandSSD/tinyland/gdrive-cache")).expanduser()]
    if any(photo_state == p or p in photo_state.parents for p in excluded):
        raise Refusal("private photo state cannot live inside sources, mount or VFS cache")
    return point, state_root, photo_state, unit["sock"]


def verified_location(plan, key, state, remote, mount, *, mounted_readback=False):
    """Verify Drive content while holding the actual mounted corpus directory.

    The leaf below is never opened or created: parent() opens GFTB Stuff
    without following symlinks. Reopening it catches directory replacement
    during independent Drive verification. This is not a deduplication verdict.
    """
    if mounted_readback and not any(e.get("event") == "retired" for e in state.events(key)):
        raise Refusal("mounted payload readback requires completed approved retirement first")
    with mount:
        with mount.parent(".gdm-location-check") as (folder_fd, _):
            before = os.fstat(folder_fd)
            verified = verify_plan(plan, key, state, remote)
            if mounted_readback:
                for entry_key, proof in sorted(verified.items()):
                    with tempfile.TemporaryDirectory(prefix="mounted-readback-", dir=state.root) as temp:
                        target = Path(temp) / "payload"
                        try:
                            expected_size = int(proof["row"]["size"])
                        except (KeyError, TypeError, ValueError):
                            raise Refusal("canonical payload lacks a verified byte length") from None
                        mount.read_to(proof["path"], target, proof["sha256"], expected_size=expected_size)
                        if hash_file(target)["sha256"] != proof["sha256"]:
                            raise Refusal("mounted canonical payload differs from independently verified Drive bytes")
                    state.event(key, "mounted-payload-verified", entry=entry_key,
                                id=proof["id"], path=proof["path"], sha256=proof["sha256"])
                # A pathname read must not hide a concurrent Drive substitution.
                if verify_plan(plan, key, state, remote) != verified:
                    raise Refusal("canonical Drive objects changed during mounted readback")
            mount.check()
            with mount.parent(".gdm-location-check") as (current_fd, _):
                after = os.fstat(current_fd)
                if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                    raise Refusal("mounted GFTB directory changed during verification")
            mount.check()
    if mounted_readback:
        state.event(key, "mounted-readback-verified", entries=len(verified))
    return len(verified)


def prepare_review(plan, key, state, remote, encoder=convert, validator=validate_webp):
    """Prepare private, unaccepted previews; never publish or modify the plan."""
    validate_alias_bindings(plan)
    entries = {entry["key"]: entry for entry in plan["entries"]}
    selected = [entry for entry in plan["entries"] if entry["kind"] == "webp"
                and entry["recipe"]["operation"] == "encode-original"]
    if (len(entries) != len(plan["entries"]) or len(selected) > MAX_FILES
            or not re.fullmatch(r"[a-f0-9]{64}", key)
            or any(not re.fullmatch(r"[a-f0-9]{64}", entry_key) for entry_key in entries)):
        raise Refusal("review entry budget or identity is invalid")
    versions, work, total = codec_versions(), [], 0
    for entry in selected:
        original = entries.get(entry.get("original_key"))
        recipe = entry["recipe"]
        if (original is None or original["kind"] != "original" or entry["sha256"] is not None
                or entry["keep_id"] is not None or recipe != {**RECIPE, "operation": "encode-original",
                    "source_original_sha256": original["sha256"], "codec_versions": versions}):
            raise Refusal("review original linkage or pinned recipe differs")
        row = original["source"].get("remote")
        if (not isinstance(row, dict) or row not in plan["remote_inventory"]
                or row["id"] != original["keep_id"]):
            raise Refusal("review requires an exact inventoried Drive source ID")
        size = row.get("size")
        if not isinstance(size, str) or not re.fullmatch(r"[0-9]+", size):
            raise Refusal("review source byte length is unavailable")
        total += int(size)
        if total > MAX_BYTES:
            raise Refusal("review exceeds source byte budget")
        work.append((entry, original, row, int(size)))
    if remote.root_folder() != plan["drive_root_id"]:
        raise Refusal("review Drive root identity changed")
    review = private_dir(state.root / f"review-{key}")
    events = [event for event in state.events(key) if event.get("event") == "review-prepared"]
    for entry, original, row, size in work:
        directory = review / entry["key"]
        source = directory / "original"
        preview = directory / "preview.webp"
        records = [event for event in events if event.get("entry") == entry["key"]]
        if records:
            remote.current(row)
            if len(records) != 1:
                raise Refusal("review journal ownership is ambiguous")
            private_dir(directory)
            record = records[0]
            if (record.get("accepted") is not False or record.get("publication_authorized") is not False
                    or record.get("source_id") != row["id"] or record.get("original_key") != original["key"]
                    or record.get("recipe") != entry["recipe"] or record.get("source_sha256") != original["sha256"]
                    or hash_file(source)["sha256"] != record["source_sha256"]
                    or hash_file(preview)["sha256"] != record["preview_sha256"]
                    or source.stat().st_size != size):
                raise Refusal("private review bytes or provenance changed")
            validator(preview)
            continue
        # An interrupted or unowned directory is preserved and refused, not adopted.
        if directory.exists() or directory.is_symlink():
            raise Refusal("unowned private review target exists")
        for attempt in range(2):
            with tempfile.TemporaryDirectory(prefix="review-attempt-", dir=review) as temporary:
                source, preview = Path(temporary) / "original", Path(temporary) / "preview.webp"
                try:
                    remote.download(row, source)
                except RemoteFailure as error:
                    if error.returncode == 5 and attempt == 0:
                        continue
                    raise
                if hash_file(source)["sha256"] != original["sha256"] or source.stat().st_size != size:
                    raise Refusal("review original differs from planned bytes")
                encoder(source, preview)
                preview_sha = hash_file(preview)["sha256"]
                dimensions = validator(preview)
                if hash_file(source)["sha256"] != original["sha256"]:
                    raise Refusal("review encoder changed original bytes")
                for path in (source, preview):
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                    try:
                        os.fchmod(fd, 0o400)
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                review_fd = os.open(review, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    rename_noreplace(Path(temporary).name, directory.name, review_fd, review_fd)
                    os.fsync(review_fd)
                finally:
                    os.close(review_fd)
                break
        state.event(key, "review-prepared", entry=entry["key"], original_key=original["key"],
                    source_id=row["id"], source_sha256=original["sha256"],
                    preview_sha256=preview_sha, recipe=entry["recipe"],
                    dimensions=dimensions, accepted=False, publication_authorized=False)
    return {"prepared": len(work), "review_directory": str(review),
            "private_index": str(state.root / f"journal-{key}.jsonl"),
            "accepted": False, "publication_authorized": False}


def prepare_correction(plan, key, state, remote, entry_key, clockwise_degrees):
    """Prepare another unaccepted preview from original pixels, never old WebP."""
    if (not isinstance(entry_key, str) or not re.fullmatch(r"[a-f0-9]{64}", entry_key)
            or not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key)
            or type(clockwise_degrees) is not int or clockwise_degrees not in (90, 180, 270)):
        raise Refusal("correction requires exact entry identity and explicit clockwise angle")
    validate_alias_bindings(plan)
    entries = {entry["key"]: entry for entry in plan["entries"]}
    entry = entries.get(entry_key)
    if (len(entries) != len(plan["entries"]) or entry is None or entry["kind"] != "webp"
            or entry["recipe"].get("operation") != "encode-original"
            or entry["sha256"] is not None or entry["keep_id"] is not None):
        raise Refusal("correction requires a missing, unaccepted derivative")
    original = entries[entry["original_key"]]
    row = original["source"]["remote"]
    records = [r for r in state.events(key) if r.get("event") == "review-prepared" and r.get("entry") == entry_key]
    if (len(records) != 1 or not isinstance(row, dict) or row not in plan["remote_inventory"]
            or row["id"] != original["keep_id"] or original["kind"] != "original"
            or entry["recipe"] != {**RECIPE, "operation": "encode-original",
                                  "source_original_sha256": original["sha256"], "codec_versions": codec_versions()}):
        raise Refusal("correction source identity or pinned recipe differs")
    base = records[0]
    if (base.get("accepted") is not False or base.get("publication_authorized") is not False
            or base.get("source_id") != row["id"] or base.get("source_sha256") != original["sha256"]
            or base.get("original_key") != original["key"] or base.get("recipe") != entry["recipe"]):
        raise Refusal("correction base provenance differs")
    directory = state.root / f"review-{key}" / entry_key
    if not directory.is_dir():
        raise Refusal("prepared private review is missing")
    private_dir(directory)
    source, preview = directory / "original", directory / "preview.webp"
    if (hash_file(source)["sha256"] != original["sha256"] or source.stat().st_size != int(row["size"])
            or hash_file(preview)["sha256"] != base["preview_sha256"]):
        raise Refusal("correction original or base preview changed")
    remote.current(row)
    if remote.root_folder() != plan["drive_root_id"]:
        raise Refusal("correction Drive root differs")
    recipe = {**entry["recipe"], "additional_clockwise_degrees": clockwise_degrees}
    records = [r for r in state.events(key) if r.get("event") == "review-correction-prepared"
               and r.get("entry") == entry_key and r.get("orientation_degrees") == clockwise_degrees]
    target_dir = directory / f"correction-{clockwise_degrees}"
    corrected = target_dir / "preview.webp"
    if records:
        if len(records) != 1:
            raise Refusal("correction journal is ambiguous")
        private_dir(target_dir)
        record = records[0]
        if (record.get("accepted") is not False or record.get("publication_authorized") is not False
                or record.get("recipe") != recipe or record.get("source_id") != row["id"]
                or record.get("source_sha256") != original["sha256"]
                or record.get("base_preview_sha256") != base["preview_sha256"]
                or hash_file(corrected)["sha256"] != record.get("corrected_preview_sha256")):
            raise Refusal("correction bytes or provenance changed")
        validate_webp(corrected)
        corrected_sha = record["corrected_preview_sha256"]
    else:
        if target_dir.exists() or target_dir.is_symlink():
            raise Refusal("unowned correction directory exists")
        with tempfile.TemporaryDirectory(prefix="correction-attempt-", dir=directory) as temporary:
            output = Path(temporary) / "preview.webp"
            convert(source, output, clockwise_degrees=clockwise_degrees)
            corrected_sha = hash_file(output)["sha256"]
            dimensions = validate_webp(output)
            if (hash_file(source)["sha256"] != original["sha256"]
                    or hash_file(preview)["sha256"] != base["preview_sha256"]):
                raise Refusal("correction changed preserved inputs")
            fd = os.open(output, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fchmod(fd, 0o400)
                os.fsync(fd)
            finally:
                os.close(fd)
            parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                rename_noreplace(Path(temporary).name, target_dir.name, parent, parent)
                os.fsync(parent)
            finally:
                os.close(parent)
        state.event(key, "review-correction-prepared", entry=entry_key, source_id=row["id"],
                    source_sha256=original["sha256"], base_preview_sha256=base["preview_sha256"],
                    corrected_preview_sha256=corrected_sha, orientation_degrees=clockwise_degrees,
                    recipe=recipe, dimensions=dimensions, accepted=False, publication_authorized=False)
    return {"entry": entry_key, "corrected_preview": str(corrected), "corrected_preview_sha256": corrected_sha,
            "orientation_degrees": clockwise_degrees, "accepted": False, "publication_authorized": False}


def review_decisions(state, review_digest):
    if not isinstance(review_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", review_digest):
        raise Refusal("review requires its full immutable digest")
    path = state.root / f"review-{review_digest}.json"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        before = os.fstat(source.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) & 0o277 or before.st_size > 8 * 1024 ** 2):
            raise Refusal("review decisions must be bounded, private and read-only")
        raw = source.read(8 * 1024 ** 2 + 1)
        if fingerprint(before) != fingerprint(os.fstat(source.fileno())):
            raise Refusal("review decisions changed during read")
    if hashlib.sha256(raw).hexdigest() != review_digest:
        raise Refusal("review decision digest mismatch")
    return json.loads(raw)


def assert_review_ssd():
    result = subprocess.run(["/usr/sbin/diskutil", "info", "-plist", "/Volumes/TinylandSSD"],
                            capture_output=True, timeout=15, check=False)
    if result.returncode:
        raise Refusal("SSD identity probe failed")
    info = plistlib.loads(result.stdout)
    if (info.get("VolumeUUID") != "E3131481-8D53-4688-B20A-E1F71DC0C61E"
            or info.get("MountPoint") != "/Volumes/TinylandSSD"):
        raise Refusal("SSD volume identity differs")


def adopt_reviewed(plan, key, state, remote, review_digest):
    """Stage explicitly reviewed bytes locally; neither publish nor retire."""
    decisions = review_decisions(state, review_digest)
    if (not isinstance(decisions, dict) or type(decisions.get("version")) is not int
            or decisions.get("version") != 1 or decisions.get("plan") != key
            or decisions.get("accepted") is not True
            or decisions.get("publication_authorized") is not False
            or not isinstance(decisions.get("entries"), list)
            or any(not isinstance(row, dict) for row in decisions["entries"])
            or not isinstance(decisions.get("evidence"), list)
            or any(not isinstance(row, dict) for row in decisions["evidence"])):
        raise Refusal("review decisions do not authorize private collection adoption")
    validate_alias_bindings(plan)
    evidence = {}
    for item in decisions.get("evidence", []):
        evidence_sha, reviewers = item.get("sha256"), item.get("reviewers")
        if (not isinstance(evidence_sha, str) or not re.fullmatch(r"[a-f0-9]{64}", evidence_sha)
                or evidence_sha in evidence or not isinstance(reviewers, list)
                or any(not isinstance(r, str) or not r.strip() for r in reviewers)):
            raise Refusal("review evidence identity is invalid")
        evidence_path = state.root / relative(item["path"])
        private_dir(evidence_path.parent)
        if hash_file(evidence_path)["sha256"] != evidence_sha:
            raise Refusal("review evidence artifact changed")
        evidence[evidence_sha] = set(reviewers)
    entries = {entry["key"]: entry for entry in plan["entries"]}
    expected = {entry["key"] for entry in entries.values() if entry["kind"] == "webp"
                and entry["recipe"]["operation"] == "encode-original"}
    selected = decisions["entries"]
    if (len(selected) > MAX_FILES or len(selected) != len(expected)
            or any(not isinstance(r.get("entry"), str) for r in selected)
            or {r.get("entry") for r in selected} != expected):
        raise Refusal("review decisions must cover each missing derivative exactly once")
    prepared = [r for r in state.events(key) if r.get("event") == "review-prepared"]
    work, paths = [], set()
    for decision in selected:
        entry = entries[decision["entry"]]
        original = entries[entry["original_key"]]
        records = [r for r in prepared if r.get("entry") == entry["key"]]
        reviewers = decision.get("reviewers")
        if (len(records) != 1 or decision.get("orientation_degrees") not in (0, 90, 180, 270)
                or type(decision.get("orientation_degrees")) is not int
                or not isinstance(reviewers, list) or len(reviewers) < 2
                or any(not isinstance(r, str) or not r.strip() for r in reviewers)
                or len(set(reviewers)) != len(reviewers)):
            raise Refusal("review lacks independent completed orientation decisions")
        record = records[0]
        evidence_hashes = decision.get("evidence_sha256")
        if (not isinstance(evidence_hashes, list) or not evidence_hashes
                or any(not isinstance(value, str) or value not in evidence for value in evidence_hashes)
                or not set(reviewers).issubset(set().union(*(evidence[value] for value in evidence_hashes)))):
            raise Refusal("review decisions lack hash-bound reviewer evidence")
        row = original["source"]["remote"]
        if (record.get("accepted") is not False or record.get("publication_authorized") is not False
                or record.get("recipe") != entry["recipe"] or record.get("original_key") != original["key"]
                or entry["recipe"].get("codec_versions") != codec_versions()
                or record.get("source_id") != original["keep_id"] or row not in plan["remote_inventory"]
                or row["id"] != original["keep_id"] or record.get("source_sha256") != original["sha256"]
                or any(decision.get(field) != record.get(field) for field in
                       ("source_id", "source_sha256", "preview_sha256"))):
            raise Refusal("review source or preview provenance differs")
        directory = private_dir(state.root / f"review-{key}" / entry["key"])
        source, preview = directory / "original", directory / "preview.webp"
        if (hash_file(source)["sha256"] != original["sha256"]
                or source.stat().st_size != int(row["size"])
                or hash_file(preview)["sha256"] != record["preview_sha256"]):
            raise Refusal("reviewed bytes changed")
        validate_webp(preview)
        accepted_sha = record["preview_sha256"]
        angle = decision["orientation_degrees"]
        if angle:
            corrections = [r for r in state.events(key) if r.get("event") == "review-correction-prepared"
                           and r.get("entry") == entry["key"] and r.get("orientation_degrees") == angle]
            if len(corrections) != 1:
                raise Refusal("review correction evidence is missing or ambiguous")
            correction = corrections[0]
            accepted_sha = decision.get("accepted_preview_sha256")
            if (correction.get("accepted") is not False or correction.get("publication_authorized") is not False
                    or correction.get("source_id") != row["id"] or correction.get("source_sha256") != original["sha256"]
                    or correction.get("base_preview_sha256") != record["preview_sha256"]
                    or correction.get("corrected_preview_sha256") != accepted_sha
                    or correction.get("recipe") != {**entry["recipe"], "additional_clockwise_degrees": angle}):
                raise Refusal("review corrected output differs from accepted evidence")
            correction_dir = private_dir(directory / f"correction-{angle}")
            preview = correction_dir / "preview.webp"
            if hash_file(preview)["sha256"] != accepted_sha:
                raise Refusal("review corrected bytes changed")
            validate_webp(preview)
        elif decision.get("accepted_preview_sha256", accepted_sha) != accepted_sha:
            raise Refusal("zero-rotation decision cannot substitute another preview")
        remote.current(row)
        original_path = relative(original["path"])
        for destination, payload, sha in (
                (str(PurePosixPath("originals") / original_path), source, original["sha256"]),
                (str(PurePosixPath("webp") / original_path.with_suffix(".webp")), preview, accepted_sha)):
            if destination.casefold() in paths or destination.casefold() in {p.casefold() for p in plan["local"]}:
                raise Refusal("review adoption destination collides with existing collection")
            paths.add(destination.casefold())
            work.append((destination, payload, sha))
    if remote.root_folder() != plan["drive_root_id"]:
        raise Refusal("review Drive root changed")
    assert_review_ssd()
    root = Path(plan["source_root"])
    if root != SSD_ROOT or root.resolve() != root.absolute():
        raise Refusal("review adoption requires the exact non-symlink SSD root")
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        st = os.fstat(root_fd)
        if {"dev": st.st_dev, "ino": st.st_ino} != plan["source_identity"]:
            raise Refusal("review SSD source root identity changed")
        # Verify curation evidence before and after staging; never rewrite it.
        for path, value in plan["local"].items():
            if path in CURATION_PATHS:
                with anchored_parent(root_fd, path) as (parent, name):
                    if hash_at(parent, name) != value:
                        raise Refusal("review curation changed")
        for destination, payload, sha in work:
            _stage_reviewed_file(key, state, review_digest, root_fd, destination, payload, sha)
        for path, value in plan["local"].items():
            if path in CURATION_PATHS:
                with anchored_parent(root_fd, path) as (parent, name):
                    if hash_at(parent, name) != value:
                        raise Refusal("review curation changed during staging")
    finally:
        os.close(root_fd)
    return {"staged_files": len(work), "review_digest": review_digest,
            "private_collection_accepted": True, "publication_authorized": False}


@contextlib.contextmanager
def _review_parent(root_fd, destination):
    parts = relative(destination).parts
    fd, device = os.dup(root_fd), os.fstat(root_fd).st_dev
    try:
        for part in parts[:-1]:
            names = [name for name in os.listdir(fd) if name.casefold() == part.casefold()]
            if names and names != [part]:
                raise Refusal("review adoption parent has a case collision")
            if not names:
                os.mkdir(part, 0o700, dir_fd=fd)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            if os.fstat(fd).st_dev != device:
                raise Refusal("review adoption parent crosses filesystems")
        yield fd, parts[-1]
    finally:
        os.close(fd)


def _stage_reviewed_file(key, state, review_digest, root_fd, destination, payload, sha):
    events = [r for r in state.events(key) if r.get("review_digest") == review_digest
              and r.get("path") == destination]
    intents = [r for r in events if r.get("event") == "review-stage-intent"]
    ready = [r for r in events if r.get("event") == "review-stage-ready"]
    if len(intents) > 1 or len(ready) > 1 or (ready and not intents):
        raise Refusal("review staging journal is ambiguous")
    with _review_parent(root_fd, destination) as (parent, name):
        aliases = [p for p in os.listdir(parent) if p.casefold() == name.casefold()]
        if aliases and (aliases != [name] or not ready):
            raise Refusal("unowned review adoption destination exists")
        temporary = intents[0]["temporary"] if intents else f".gdm-review-{uuid.uuid4().hex}.tmp"
        if not re.fullmatch(r"\.gdm-review-[a-f0-9]{32}\.tmp", temporary):
            raise Refusal("review staging name is invalid")
        if ready:
            record = ready[0]
            chosen = name if aliases else temporary
            actual = hash_at(parent, chosen)
            if (actual["sha256"] != sha or record.get("sha256") != sha
                    or any(actual["fingerprint"][field] != record.get(field) for field in ("dev", "ino", "size"))):
                raise Refusal("journal-owned review staging bytes differ")
        else:
            if not intents:
                state.event(key, "review-stage-intent", review_digest=review_digest,
                            path=destination, temporary=temporary)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400, dir_fd=parent)
            with os.fdopen(fd, "wb") as target:
                source_fd = os.open(payload, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(source_fd, "rb") as source:
                    source_stat = os.fstat(source.fileno())
                    if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_size > MAX_BYTES:
                        raise Refusal("review payload is not a bounded regular file")
                    before, calculated, length = fingerprint(source_stat), hashlib.sha256(), 0
                    for block in iter(lambda: source.read(min(1024 * 1024, before["size"] - length + 1)), b""):
                        length += len(block)
                        if length > before["size"]:
                            raise Refusal("review payload grew during staging")
                        calculated.update(block)
                        target.write(block)
                    if calculated.hexdigest() != sha or fingerprint(os.fstat(source.fileno())) != before:
                        raise Refusal("review payload changed during staging")
                target.flush()
                os.fsync(target.fileno())
                identity = os.fstat(target.fileno())
            state.event(key, "review-stage-ready", review_digest=review_digest, path=destination,
                        sha256=sha, dev=identity.st_dev, ino=identity.st_ino, size=identity.st_size)
        if not aliases:
            rename_noreplace(temporary, name, parent, parent)
            os.fsync(parent)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "apply", "verify", "retire", "prepare-review", "prepare-correction", "adopt-reviewed"))
    parser.add_argument("plan_digest", nargs="?")
    parser.add_argument("--approve-digest", help="explicit exact-manifest approval for recoverable retirement")
    parser.add_argument("--review-digest", help="immutable private collection review decision digest")
    parser.add_argument("--entry-key", help="exact missing-derivative key for private correction")
    parser.add_argument("--clockwise-degrees", type=int, help="explicit additional clockwise rotation")
    parser.add_argument("--reuse-plan-digest", help="reuse unchanged advertised SHA256 with prior payload-verified evidence")
    parser.add_argument("--settings", default=str(Path.home() / ".local/state/gdrive-mounts/effective-settings.json"))
    parser.add_argument("--state-dir")
    parser.add_argument("--mounted-readback", action="store_true",
                        help="verify every canonical payload through the mount after approved retirement")
    args = parser.parse_args(argv)
    try:
        if args.reuse_plan_digest and args.command != "plan":
            raise Refusal("--reuse-plan-digest is only valid with plan")
        if args.mounted_readback and args.command != "verify":
            raise Refusal("--mounted-readback is only valid with verify")
        point, runtime, private, sock = configuration(args.settings, args.state_dir)
        state = State(private)
        remote = RcloneRemote(runtime / f"rclone-{ORG}.conf", state, sock)
        with state.locked():
            if args.command == "plan":
                if args.plan_digest:
                    raise Refusal("plan does not accept an existing digest")
                prior = state.load(args.reuse_plan_digest) if args.reuse_plan_digest else None
                plan = make_plan(SSD_ROOT, point, remote, require_curation=True,
                                 reuse_plan=prior, reuse_plan_digest=args.reuse_plan_digest)
                key = state.save(plan)
                result = {"plan": key, "entries": len(plan["entries"]),
                          "retirement_candidates": len(plan["retirement"]), "warning_count": len(plan["warnings"]),
                          "unresolved_files": len(plan["unresolved"]), "blocker_count": len(plan["blockers"])}
            else:
                if not args.plan_digest:
                    raise Refusal("command requires an immutable plan digest")
                key, plan = args.plan_digest, state.load(args.plan_digest)
                if plan["source_root"] != str(SSD_ROOT) or plan["mount_point"] != str(point):
                    raise Refusal("plan roots do not match the explicit corpus and effective mount")
                if args.command == "prepare-review":
                    result = prepare_review(plan, key, state, remote)
                elif args.command == "prepare-correction":
                    result = prepare_correction(plan, key, state, remote, args.entry_key, args.clockwise_degrees)
                elif args.command == "adopt-reviewed":
                    result = adopt_reviewed(plan, key, state, remote, args.review_digest)
                elif args.command == "apply":
                    result = {"entries": apply_plan(plan, key, state, remote, MountedIO(point))}
                elif args.command == "verify":
                    result = {"verified": verified_location(plan, key, state, remote, MountedIO(point),
                                                           mounted_readback=args.mounted_readback)}
                else:
                    result = retire_plan(plan, key, args.approve_digest, state, remote)
                    result.pop("quarantine", None)  # Exact locations remain in the private plan/journal.
                result["plan"] = key
            result["phase"] = args.command
            result["canonical_location"] = {
                "absolute_path": str(point / FOLDER),
                "drive_folder_id": plan["drive_root_id"],
                "mounted_directory_verified": args.command == "verify",
                "mounted_payloads_verified": args.command == "verify" and args.mounted_readback,
                "drive_contents_verified": args.command in {"verify", "retire"},
            }
            result["unresolved_files"] = len(plan.get("unresolved", []))
            result["blocker_count"] = len(plan.get("blockers", []))
            print(json.dumps(result, sort_keys=True))
        return 0
    except (Refusal, OSError, ValueError, KeyError, ImportError) as error:
        print(f"photos: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
