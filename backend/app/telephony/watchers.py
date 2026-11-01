"""Phone captions on screen (M7.2): a verified phone call's screen events
(captions, what the caller said, the agent's sentences, interruptions, the
end), relayed live to the account holder's own signed-in pages. In memory
only: nothing is stored, and with no one watching an event is dropped.
"""

import asyncio

MAX_WAITING = 500  # a watcher this far behind misses events rather than growing memory


class CallWatchers:
    def __init__(self) -> None:
        self._watching: dict[str, set[asyncio.Queue]] = {}

    def subscribe(self, customer_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_WAITING)
        self._watching.setdefault(customer_id, set()).add(queue)
        return queue

    def unsubscribe(self, customer_id: str, queue: asyncio.Queue) -> None:
        queues = self._watching.get(customer_id, set())
        queues.discard(queue)
        if not queues:
            self._watching.pop(customer_id, None)

    def publish(self, customer_id: str, message: str) -> None:
        for queue in self._watching.get(customer_id, ()):
            if not queue.full():
                queue.put_nowait(message)

    def watching(self, customer_id: str) -> int:
        return len(self._watching.get(customer_id, ()))


watchers = CallWatchers()
