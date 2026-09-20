import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


class SourceTooLargeError(ValueError):
    pass


@dataclass(frozen=True)
class ObjectInfo:
    sha256: str
    path: Path
    size_bytes: int


class SourceObjectStore:
    def __init__(self, root: Path, max_bytes: int) -> None:
        self.root = root.resolve()
        self.max_bytes = max_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, stream: BinaryIO) -> ObjectInfo:
        digest = hashlib.sha256()
        size = 0
        file_descriptor, temp_name = tempfile.mkstemp(prefix=".upload-", dir=self.root)
        try:
            with os.fdopen(file_descriptor, "wb") as target:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise SourceTooLargeError(
                            f"source exceeds maximum size of {self.max_bytes} bytes"
                        )
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            sha256 = digest.hexdigest()
            destination = self.root / sha256[:2] / sha256
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                Path(temp_name).unlink()
            else:
                os.replace(temp_name, destination)
            return ObjectInfo(sha256=sha256, path=destination, size_bytes=size)
        except Exception:
            Path(temp_name).unlink(missing_ok=True)
            raise
