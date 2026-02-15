from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from typing import Generic, TypeVar

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


@dataclass(frozen=True, slots=True)
class CacheStats:
    hits: int
    misses: int
    sets: int
    evictions: int
    expirations: int


class LruTtlCache(Generic[K, V]):
    __slots__ = (
        "_data",
        "_lock",
        "_maxsize",
        "_ttl_seconds",
        "_clock",
        "_hits",
        "_misses",
        "_sets",
        "_evictions",
        "_expirations",
    )

    def __init__(
        self,
        *,
        maxsize: int,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize must be positive")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")

        self._data: OrderedDict[K, tuple[float, V]] = OrderedDict()
        self._lock = threading.Lock()
        self._maxsize = maxsize
        self._ttl_seconds = ttl_seconds
        self._clock = clock

        self._hits = 0
        self._misses = 0
        self._sets = 0
        self._evictions = 0
        self._expirations = 0

    def get(self, key: K) -> V | None:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                self._misses += 1
                return None

            expires_at, value = item
            now = self._clock()
            if expires_at <= now:
                self._expirations += 1
                self._misses += 1
                del self._data[key]
                return None

            self._hits += 1
            self._data.move_to_end(key, last=True)
            return value

    def set(self, key: K, value: V) -> None:
        with self._lock:
            now = self._clock()
            expires_at = now + self._ttl_seconds

            if key in self._data:
                self._data.move_to_end(key, last=True)

            self._data[key] = (expires_at, value)
            self._sets += 1

            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)
                self._evictions += 1

    def stats(self) -> CacheStats:
        with self._lock:
            return CacheStats(
                hits=self._hits,
                misses=self._misses,
                sets=self._sets,
                evictions=self._evictions,
                expirations=self._expirations,
            )

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
