"""A bounded, thread-safe in-memory batch buffer for serialized span rows.

Deliberately holds already-serialized wire-format dicts, not `Span` objects
-- serialization happens once, at enqueue time, off whatever thread
completed the span, so draining the buffer for delivery never has to touch
`Span` state again (and never races a caller still mutating it).
"""

from __future__ import annotations

import threading
from typing import Any


class BoundedBatchBuffer:
    """Buffers span rows up to `max_queue_size`; signals when a caller
    should flush once `max_batch_size` rows have accumulated.

    All methods are safe to call from multiple threads. No method ever
    performs I/O or blocks on anything but the internal lock, so this class
    can safely be used from both a background worker thread and a caller
    thread (e.g. an explicit `flush()`) without risking a deadlock.
    """

    def __init__(self, *, max_batch_size: int, max_queue_size: int) -> None:
        self._max_batch_size = max_batch_size
        self._max_queue_size = max_queue_size
        self._lock = threading.Lock()
        self._items: list[dict[str, Any]] = []

    def add(self, item: dict[str, Any]) -> tuple[bool, bool]:
        """Append `item`. Returns `(added, should_flush)`.

        `added` is `False` if the buffer was already at `max_queue_size` --
        the item is dropped rather than growing the buffer unboundedly.
        `should_flush` is `True` once the buffer has reached
        `max_batch_size`, signaling the caller to trigger a flush.
        """
        with self._lock:
            if len(self._items) >= self._max_queue_size:
                return False, False
            self._items.append(item)
            return True, len(self._items) >= self._max_batch_size

    def drain(self) -> list[dict[str, Any]]:
        """Atomically remove and return every currently-buffered item."""
        with self._lock:
            items, self._items = self._items, []
            return items

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
