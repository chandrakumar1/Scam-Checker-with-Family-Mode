"""In-memory TTL set of already-seen Telegram update_ids.

Per process and lost on restart, which is fine for a single worker with no
database. Telegram redelivers an update if it does not get a quick 2xx, so the
webhook checks this before doing any work.
"""

import threading
import time
from collections import OrderedDict
from collections.abc import Callable

DEFAULT_TTL_SECONDS = 3600.0
DEFAULT_MAX_SIZE = 10_000


class SeenUpdates:
    def __init__(
        self,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_size: int = DEFAULT_MAX_SIZE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._clock = clock
        self._lock = threading.Lock()
        # update_id -> expiry time. Every entry gets the same TTL, so insertion
        # order is also expiry order and expired entries sit at the front.
        self._entries: OrderedDict[int, float] = OrderedDict()

    def seen_before(self, update_id: int) -> bool:
        """Return True if `update_id` was seen within the TTL; otherwise record it."""
        with self._lock:
            now = self._clock()
            while self._entries:
                oldest_id, expires_at = next(iter(self._entries.items()))
                if expires_at > now:
                    break
                del self._entries[oldest_id]

            if update_id in self._entries:
                return True

            self._entries[update_id] = now + self._ttl
            while len(self._entries) > self._max_size:
                self._entries.popitem(last=False)
            return False

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
