"""Cache SQLite com TTL por chamada e suporte a stale-if-error."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class CacheEntry:
    value: Any
    age: float
    fresh: bool


class Cache:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, stored_at REAL NOT NULL)"
        )
        self._conn.commit()
        self._lock = asyncio.Lock()

    def _get(self, key: str) -> tuple[str, float] | None:
        row = self._conn.execute("SELECT value, stored_at FROM cache WHERE key = ?", (key,)).fetchone()
        return (row[0], row[1]) if row else None

    def _set(self, key: str, value: str, ts: float) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO cache (key, value, stored_at) VALUES (?, ?, ?)", (key, value, ts)
        )
        self._conn.commit()

    async def get(self, key: str, ttl: int) -> CacheEntry | None:
        async with self._lock:
            row = await asyncio.to_thread(self._get, key)
        if row is None:
            return None
        age = time.time() - row[1]
        return CacheEntry(value=json.loads(row[0]), age=age, fresh=age < ttl)

    async def set(self, key: str, value: Any) -> None:
        async with self._lock:
            await asyncio.to_thread(self._set, key, json.dumps(value, default=str), time.time())

    def close(self) -> None:
        self._conn.close()
