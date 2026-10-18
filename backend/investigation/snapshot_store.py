"""本地内容寻址快照：冻结受限投影，不保存任意原始标签或实验真值。"""

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from .evidence_models import ObservationRef


class SnapshotStore:
    MAX_BYTES = 8_000_000

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def freeze(self, payload: dict) -> str:
        data = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(data) > self.MAX_BYTES:
            raise ValueError("snapshot_too_large")
        key = hashlib.sha256(data).hexdigest()
        # 同目录临时文件 + 硬链接发布：读者看不到半写入文件，已有版本不覆盖。
        fd, name = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(name, self.directory / f"{key}.private.json")
            except FileExistsError:
                self.read(key)
        finally:
            Path(name).unlink(missing_ok=True)
        return key

    def read(self, key: str) -> dict:
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError("invalid_snapshot_id")
        with (self.directory / f"{key}.private.json").open("rb") as stream:
            data = stream.read(self.MAX_BYTES + 1)
        if len(data) > self.MAX_BYTES or hashlib.sha256(data).hexdigest() != key:
            raise ValueError("snapshot_integrity_error")
        return json.loads(data)

    def resolve(self, reference: ObservationRef):
        payload = self.read(reference.snapshot_id)
        trace = payload["traces"].get(reference.trace_id)
        if trace is not None:
            for span in trace["spans"]:
                if span["span_id"] == reference.span_id:
                    if reference.field.startswith("tags."):
                        if reference.field[5:] in span["tags"]:
                            return span["tags"][reference.field[5:]]
                    if reference.field in {"start_us", "duration_ms", "service", "operation"}:
                        return span[reference.field]
        raise ValueError("invalid_observation_reference")
