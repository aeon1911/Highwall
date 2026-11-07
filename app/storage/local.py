from __future__ import annotations

import secrets
from pathlib import Path

from . import Storage, StoredFile, StorageError


class LocalStorage(Storage):
    """Armazenamento em disco — só para desenvolvimento/testes sem bot do Telegram."""

    name = "local"

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, ref: str) -> Path:
        p = (self.root / ref).resolve()
        if self.root.resolve() not in p.parents:
            raise StorageError("Referência inválida")
        return p

    async def put(self, data: bytes, filename: str, mime: str, caption: str = "") -> StoredFile:
        ext = filename.rsplit(".", 1)[-1] if "." in filename else "bin"
        ref = f"{secrets.token_hex(16)}.{ext}"
        self._path(ref).write_bytes(data)
        return StoredFile(ref=ref)

    async def fetch(self, ref: str) -> bytes:
        p = self._path(ref)
        if not p.exists():
            raise StorageError("Arquivo não encontrado")
        return p.read_bytes()

    async def delete(self, ref: str, message_id: int | None) -> None:
        self._path(ref).unlink(missing_ok=True)
