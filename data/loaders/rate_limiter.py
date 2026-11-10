"""Token bucket rate limiter ensuring compliance with Kite historical API limits (max 3 req/sec)."""
import time
import threading
from functools import wraps
from typing import Callable, Any

from configs.logger import logger


class RateLimiter:
    """Thread-safe rate limiter with minimum interval enforcement."""

    def __init__(self, min_interval_seconds: float = 0.35):
        self.min_interval = min_interval_seconds
        self.last_called = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        """Blocks execution until the required interval has passed."""
        with self._lock:
            now = time.time()
            elapsed = now - self.last_called
            left_to_wait = self.min_interval - elapsed
            if left_to_wait > 0:
                time.sleep(left_to_wait)
            self.last_called = time.time()

    def __call__(self, func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            self.wait()
            return func(*args, **kwargs)
        return wrapper


# Default instance configured for 330ms (safe margin under 3 requests/sec)
kite_rate_limiter = RateLimiter(min_interval_seconds=0.35)
