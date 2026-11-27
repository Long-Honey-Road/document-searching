"""Минутный лимит запусков на учётку в процессе backend."""

from collections import Counter, deque
from time import monotonic


class AccountRateLimit:
    def __init__(self) -> None:
        self._runs: deque[tuple[float, str]] = deque()
        self._counts: Counter[str] = Counter()

    def allow(self, sub: str, limit: int) -> bool:
        now = monotonic()
        while self._runs and self._runs[0][0] <= now - 60:
            _, expired = self._runs.popleft()
            self._counts[expired] -= 1
            if self._counts[expired] == 0:
                del self._counts[expired]
        if self._counts[sub] >= limit:
            return False
        self._runs.append((now, sub))
        self._counts[sub] += 1
        return True
