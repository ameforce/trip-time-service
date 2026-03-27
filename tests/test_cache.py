from __future__ import annotations

import threading

from trip_time_service.core.cache import LruTtlCache


class _ManualClock:
    __slots__ = ("_now",)

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def test_get_returns_none_on_miss() -> None:
    cache: LruTtlCache[str, int] = LruTtlCache(maxsize=8, ttl_seconds=60.0)
    assert cache.get("nonexistent") is None
    assert cache.stats().misses == 1


def test_set_and_get_returns_value() -> None:
    cache: LruTtlCache[str, int] = LruTtlCache(maxsize=8, ttl_seconds=60.0)
    cache.set("a", 1)
    assert cache.get("a") == 1
    assert cache.stats().hits == 1


def test_ttl_expiration() -> None:
    clock = _ManualClock(0.0)
    cache: LruTtlCache[str, int] = LruTtlCache(maxsize=8, ttl_seconds=10.0, clock=clock)
    cache.set("a", 100)

    clock.advance(5.0)
    assert cache.get("a") == 100  # within TTL

    clock.advance(6.0)  # total 11s > 10s TTL
    assert cache.get("a") is None  # expired

    stats = cache.stats()
    assert stats.expirations == 1


def test_lru_eviction() -> None:
    cache: LruTtlCache[str, int] = LruTtlCache(maxsize=2, ttl_seconds=60.0)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)  # evicts "a"

    assert cache.get("a") is None
    assert cache.get("b") == 2
    assert cache.get("c") == 3

    stats = cache.stats()
    assert stats.evictions == 1


def test_lru_access_refreshes_order() -> None:
    cache: LruTtlCache[str, int] = LruTtlCache(maxsize=2, ttl_seconds=60.0)
    cache.set("a", 1)
    cache.set("b", 2)

    cache.get("a")  # refresh "a" → LRU is now "b"
    cache.set("c", 3)  # evicts "b"

    assert cache.get("a") == 1
    assert cache.get("b") is None
    assert cache.get("c") == 3


def test_overwrite_existing_key() -> None:
    cache: LruTtlCache[str, int] = LruTtlCache(maxsize=8, ttl_seconds=60.0)
    cache.set("a", 1)
    cache.set("a", 2)
    assert cache.get("a") == 2
    assert len(cache) == 1


def test_len_reflects_live_entries() -> None:
    clock = _ManualClock(0.0)
    cache: LruTtlCache[str, int] = LruTtlCache(maxsize=8, ttl_seconds=5.0, clock=clock)
    cache.set("a", 1)
    cache.set("b", 2)
    assert len(cache) == 2

    clock.advance(6.0)
    # expired entries still in _data until accessed
    cache.get("a")  # triggers expiration removal
    cache.get("b")
    assert len(cache) == 0


def test_thread_safety_concurrent_writes() -> None:
    cache: LruTtlCache[int, int] = LruTtlCache(maxsize=256, ttl_seconds=60.0)
    errors: list[Exception] = []

    def writer(start: int, count: int) -> None:
        try:
            for i in range(start, start + count):
                cache.set(i, i * 10)
                cache.get(i)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i * 50, 50)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    stats = cache.stats()
    assert stats.sets == 200
