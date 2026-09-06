"""A cancellable priority mutex. Network I/O must happen outside this lock."""

import asyncio
import heapq
import itertools
from contextlib import asynccontextmanager


class PriorityLock:
    def __init__(self):
        self._busy = False
        self._waiters = []
        self._sequence = itertools.count()

    async def acquire(self, priority=10):
        future = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (priority, next(self._sequence), future))
        self._grant()
        try:
            await future
        except asyncio.CancelledError:
            if not future.cancelled():
                self.release()  # cancellation after the lock was granted
            raise
        return True

    def _grant(self):
        if self._busy:
            return
        while self._waiters:
            _, _, future = heapq.heappop(self._waiters)
            if not future.cancelled():
                self._busy = True
                future.set_result(True)
                return

    def release(self):
        if not self._busy:
            raise RuntimeError("unlocked mutex")
        self._busy = False
        self._grant()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *exc):
        self.release()

    @asynccontextmanager
    async def hold(self, priority):
        await self.acquire(priority)
        try:
            yield self
        finally:
            self.release()
