"""Content-addressed artifact store.

Blobs are stored by SHA-256 under <root>/ab/cdef..., written atomically and never modified.
Large evidence lives here; the ledger only stores digests.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from ..redact import redact_bytes


class IntegrityError(Exception):
    pass


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, digest: str) -> Path:
        algo, _, hexd = digest.partition(":")
        if algo != "sha256" or len(hexd) != 64:
            raise ValueError(f"bad digest {digest!r}")
        return self.root / hexd[:2] / hexd[2:]

    def put_bytes(self, data: bytes, redact: bool = True) -> str:
        if redact:
            data = redact_bytes(data)
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        path = self._path(digest)
        if path.exists():
            return digest
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o444)
        os.replace(tmp, path)
        return digest

    def put_json(self, obj: Any, redact: bool = True) -> str:
        return self.put_bytes(json.dumps(obj, sort_keys=True, indent=1, default=str).encode(), redact=redact)

    def put_dir(self, src: Path) -> str:
        """Snapshot a directory as a deterministic tarball (sorted, zeroed metadata)."""
        src = Path(src)
        buf = tempfile.SpooledTemporaryFile()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for p in sorted(src.rglob("*")):
                if "__pycache__" in p.parts:
                    continue
                info = tar.gettarinfo(str(p), arcname=str(p.relative_to(src)))
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                if p.is_file():
                    with open(p, "rb") as fh:
                        tar.addfile(info, fh)
                else:
                    tar.addfile(info)
        buf.seek(0)
        return self.put_bytes(buf.read(), redact=False)

    def extract_dir(self, digest: str, dest: Path) -> Path:
        dest = Path(dest)
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        import io

        with tarfile.open(fileobj=io.BytesIO(self.get_bytes(digest)), mode="r") as tar:
            tar.extractall(dest, filter="data")
        return dest

    def get_bytes(self, digest: str) -> bytes:
        data = self._path(digest).read_bytes()
        if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
            raise IntegrityError(f"artifact {digest} does not match its digest")
        return data

    def get_json(self, digest: str) -> Any:
        return json.loads(self.get_bytes(digest))

    def exists(self, digest: str) -> bool:
        try:
            return self._path(digest).exists()
        except ValueError:
            return False

    def verify_all(self) -> tuple[int, list[str]]:
        """Re-hash every blob. Returns (count, list of corrupted digests)."""
        bad, n = [], 0
        for p in self.root.glob("??/*"):
            n += 1
            digest = "sha256:" + p.parent.name + p.name
            if "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest() != digest:
                bad.append(digest)
        return n, bad
