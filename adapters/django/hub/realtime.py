"""Push-first realtime coordination for the Django Hub adapter.

The event ledger remains canonical.  This module is only the wake-up plane: a mutation publishes
an identity-only signal *after* its durable write, and every open SSE response waiting in this
process wakes immediately.  A reconnect still reconciles from the ledger cursor, so a lost signal
can never become lost state.

The built-in bus is intentionally process-local and has no polling loop.  A multi-process
deployment must set ``HUB_REALTIME_BROKER`` to a shared broker factory.  The factory is called with
no arguments and returns an object with this small interface::

    publish(channel: str, signal: dict) -> None
    listen(channel: str) -> Iterator[dict]

``listen`` is a blocking iterator (Redis pub/sub, Postgres LISTEN/NOTIFY, NATS, etc.).  One daemon
listener per Django process relays shared messages into the same local condition bus.  Broker
failure never rolls back an already durable board mutation; it is logged and the local path keeps
working.  Operators must treat a degraded shared broker as a deployment incident, not as an
invisible fallback guarantee.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import queue
import threading
import time
import uuid
from collections import deque
from pathlib import Path

LOG = logging.getLogger(__name__)
_HISTORY = 2048
_BUSES = {}
_BUSES_LOCK = threading.Lock()
_BROKERS = {}
_BROKERS_LOCK = threading.Lock()
_TIMERS = {}
_TIMERS_LOCK = threading.Lock()


def _setting(name, default=None):
    try:
        from django.conf import settings
        return getattr(settings, name, default)
    except Exception:
        return default


def _root_key(root) -> str:
    try:
        return str(Path(root).resolve())
    except OSError:
        return str(root)


def _load(spec):
    if not isinstance(spec, str):
        return spec
    module, _, name = spec.rpartition(".")
    if not module:
        raise ValueError("HUB_REALTIME_BROKER must be a dotted factory path")
    return getattr(importlib.import_module(module), name)


class _Bus:
    def __init__(self):
        self.condition = threading.Condition()
        self.generation = 0
        self.history = deque(maxlen=_HISTORY)
        self.seen_order = deque(maxlen=_HISTORY)
        self.seen = set()
        self.async_subscribers = set()

    def position(self):
        with self.condition:
            return self.generation

    def publish(self, signal):
        signal = dict(signal or {})
        signal.setdefault("id", uuid.uuid4().hex)
        signal.setdefault("at", time.time())
        sid = signal["id"]
        with self.condition:
            # Shared brokers normally echo a publisher's own signal.  The identity makes that
            # echo a no-op without suppressing a distinct mutation at the same ledger cursor.
            if sid in self.seen:
                return self.generation
            if len(self.seen_order) == self.seen_order.maxlen:
                self.seen.discard(self.seen_order[0])
            self.seen_order.append(sid)
            self.seen.add(sid)
            self.generation += 1
            self.history.append((self.generation, signal))
            subscribers = tuple(self.async_subscribers)
            self.condition.notify_all()
        for subscriber in subscribers:
            subscriber.wake()
        return self.generation

    def drain(self, after):
        with self.condition:
            marker = self.generation
            rows = [signal for generation, signal in self.history if generation > after]
        # A very slow consumer can outrun the bounded identity queue.  One overflow signal is
        # sufficient: the browser's canonical delta reconciliation starts from its own cursor.
        if marker > after and not rows:
            rows = [{"id": uuid.uuid4().hex, "kind": "overflow", "at": time.time()}]
        return marker, rows

    def wait(self, after, timeout):
        with self.condition:
            self.condition.wait_for(lambda: self.generation > after, timeout=timeout)
        return self.drain(after)

    def add_async(self, subscriber):
        with self.condition:
            self.async_subscribers.add(subscriber)

    def remove_async(self, subscriber):
        with self.condition:
            self.async_subscribers.discard(subscriber)


def _bus(root):
    key = _root_key(root)
    with _BUSES_LOCK:
        return _BUSES.setdefault(key, _Bus())


class Subscription:
    """A lossless-within-the-bounded-queue synchronous wait cursor."""

    def __init__(self, bus):
        self.bus = bus
        self.marker = bus.position()

    def wait(self, timeout=15):
        self.marker, rows = self.bus.wait(self.marker, timeout)
        return rows


class AsyncSubscription:
    """Native ASGI waiter: no sync request thread is held while the stream is idle."""

    def __init__(self, bus):
        self.bus = bus
        self.marker = bus.position()
        self.loop = asyncio.get_running_loop()
        self.event = asyncio.Event()
        self.closed = False
        bus.add_async(self)

    def wake(self):
        if not self.closed:
            try:
                self.loop.call_soon_threadsafe(self.event.set)
            except RuntimeError:
                pass

    async def wait(self, timeout=15):
        while self.bus.position() <= self.marker:
            self.event.clear()
            if self.bus.position() > self.marker:
                break
            try:
                await asyncio.wait_for(self.event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                return []
        self.marker, rows = self.bus.drain(self.marker)
        return rows

    def close(self):
        if not self.closed:
            self.closed = True
            self.bus.remove_async(self)


class _BrokerState:
    def __init__(self, spec, channel, bus):
        self.spec = spec
        self.channel = channel
        self.bus = bus
        self.backend = None
        self.thread = None
        self.publisher_thread = None
        self.outbox = queue.SimpleQueue()
        self.error = None

    @property
    def name(self):
        return self.spec if isinstance(self.spec, str) else type(self.spec).__name__

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        try:
            factory = _load(self.spec)
            self.backend = factory() if inspect.isclass(factory) or callable(factory) else factory
            if not callable(getattr(self.backend, "publish", None)) or not callable(
                    getattr(self.backend, "listen", None)):
                raise TypeError("realtime broker must expose publish(channel, signal) and listen(channel)")
        except Exception as exc:
            self.error = str(exc)
            LOG.exception("Hub shared realtime broker could not start")
            return
        self.thread = threading.Thread(target=self._listen_forever,
                                       name="hub-realtime-broker", daemon=True)
        self.thread.start()
        self.publisher_thread = threading.Thread(target=self._publish_forever,
                                                 name="hub-realtime-publisher", daemon=True)
        self.publisher_thread.start()

    def _listen_forever(self):
        delay = 0.25
        while True:
            try:
                self.error = None
                for signal in self.backend.listen(self.channel):
                    if isinstance(signal, dict):
                        self.bus.publish(signal)
                raise RuntimeError("shared realtime broker listener ended")
            except Exception as exc:
                self.error = str(exc)
                LOG.exception("Hub shared realtime broker listener failed; reconnecting")
                threading.Event().wait(delay)
                delay = min(delay * 2, 15)

    def _publish_forever(self):
        while True:
            signal = self.outbox.get()
            try:
                self.backend.publish(self.channel, dict(signal))
                self.error = None
            except Exception as exc:
                self.error = str(exc)
                LOG.exception("Hub shared realtime publication failed")

    def publish(self, signal):
        self.start()
        if not self.backend:
            return
        # Never put network I/O on the durable mutation path.  The local process is already awake;
        # this daemon relays to sibling processes immediately after the request releases its lock.
        self.outbox.put(dict(signal))


def _broker(root, channel):
    spec = _setting("HUB_REALTIME_BROKER")
    if not spec:
        return None
    key = (_root_key(root), str(channel), str(spec))
    with _BROKERS_LOCK:
        state = _BROKERS.get(key)
        if state is None:
            state = _BrokerState(spec, str(channel), _bus(root))
            _BROKERS[key] = state
    state.start()
    return state


def subscribe(root, *, channel="hub"):
    _broker(root, channel)
    return Subscription(_bus(root))


def subscribe_async(root, *, channel="hub"):
    _broker(root, channel)
    return AsyncSubscription(_bus(root))


def publish(root, signal, *, channel="hub"):
    """Wake local subscribers immediately, then relay the same identity through the shared bus."""
    payload = dict(signal or {})
    payload.setdefault("id", uuid.uuid4().hex)
    payload.setdefault("at", time.time())
    _bus(root).publish(payload)
    broker = _broker(root, channel)
    if broker:
        broker.publish(payload)


def info(root, *, channel="hub"):
    """Truthful transport scope for the cockpit and deployment diagnostics."""
    state = _broker(root, channel)
    if not state:
        return {"mode": "push", "scope": "process", "broker": None,
                "degraded": False}
    return {"mode": "push", "scope": "shared" if not state.error else "shared-degraded",
            "broker": state.name, "degraded": bool(state.error)}


def schedule(root, key, at, signal, *, channel="hub"):
    """Publish at a semantic time boundary (lease stall/expiry), with no polling clock."""
    timer_key = (_root_key(root), str(key))

    def fire(timer):
        with _TIMERS_LOCK:
            if _TIMERS.get(timer_key) is not timer:
                return
            _TIMERS.pop(timer_key, None)
        publish(root, signal, channel=channel)

    delay = max(0.0, float(at) - time.time())
    holder = {}
    timer = threading.Timer(delay, lambda: fire(holder["timer"]))
    timer.daemon = True
    holder["timer"] = timer
    with _TIMERS_LOCK:
        old = _TIMERS.get(timer_key)
        if old:
            old.cancel()
        _TIMERS[timer_key] = timer
    timer.start()


def cancel_scheduled(root, key):
    timer_key = (_root_key(root), str(key))
    with _TIMERS_LOCK:
        timer = _TIMERS.pop(timer_key, None)
    if timer:
        timer.cancel()
