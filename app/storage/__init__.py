from __future__ import annotations

import asyncio
import hashlib
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from ..config import get_settings


@dataclass
class StoredFile:
    ref: str  # file_id (Telegram) ou nome do arquivo (local)
    message_id: int | None = None


class StorageError(RuntimeError):
    pass


class Storage(ABC):
    name: str

    @abstractmethod
    async def put(self, data: bytes, filename: str, mime: str, caption: str = "") -> StoredFile: ...

    @abstractmethod
    async def fetch(self, ref: str) -> bytes: ...

    async def delete(self, ref: str, message_id: int | None) -> None:  # opcional
        return None


class DiskCache:
    """Cache LRU em disco para não baixar do Telegram a cada visualização."""

    def __init__(self, root: Path, max_bytes: int):
        self.root = root
        self.max_bytes = max_bytes
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}
        self._writes = 0

    def path_for(self, key: str) -> Path:
        h = hashlib.sha256(key.encode()).hexdigest()
        return self.root / h[:2] / h

    def lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def get(self, key: str) -> Path | None:
        p = self.path_for(key)
        if p.exists():
            os.utime(p)  # marca como usado recentemente
            return p
        return None

    def put(self, key: str, data: bytes) -> Path:
        p = self.path_for(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)
        self._writes += 1
        if self._writes % 20 == 0:
            self.evict()
        return p

    def evict(self) -> None:
        files = [(f.stat().st_mtime, f.stat().st_size, f) for f in self.root.rglob("*") if f.is_file()]
        total = sum(s for _, s, _ in files)
        for _, size, f in sorted(files):
            if total <= self.max_bytes:
                break
            f.unlink(missing_ok=True)
            total -= size


_storage: Storage | None = None
_cache: DiskCache | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        s = get_settings()
        if s.storage_backend == "telegram":
            from .telegram import TelegramStorage

            _storage = TelegramStorage(s.telegram_bot_token, s.telegram_chat_id, s.telegram_api_base)
        elif s.storage_backend == "local":
            from .local import LocalStorage

            _storage = LocalStorage(s.local_storage_dir)
        else:
            raise StorageError(f"Backend de armazenamento desconhecido: {s.storage_backend}")
    return _storage


def set_storage(storage: Storage | None) -> None:
    global _storage
    _storage = storage


def get_cache() -> DiskCache:
    global _cache
    if _cache is None:
        s = get_settings()
        _cache = DiskCache(Path(s.cache_dir), s.cache_max_mb * 1024 * 1024)
    return _cache


async def fetch_cached(ref: str) -> Path:
    """Devolve o caminho local de um arquivo armazenado, baixando-o se necessário."""
    cache = get_cache()
    hit = cache.get(ref)
    if hit:
        return hit
    async with cache.lock(ref):
        hit = cache.get(ref)
        if hit:
            return hit
        data = await get_storage().fetch(ref)
        return cache.put(ref, data)
