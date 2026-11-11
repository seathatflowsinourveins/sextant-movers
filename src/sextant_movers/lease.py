"""The account lease and the stream reservation: one writer, and so one stream, per account.

BUILD ON the reference at its commit 06562bec0898a7203f4eaf69086ced83d2477e6b:
`src/sextant/execution/custody.py:75-85` (`AccountScope`), `:163-205` (`WriterLease`) and
`src/sextant/tradingview/producer.py:33-47` (`reserve_stream`). For the same scope and root the
lock file path, its open flags and mode, and the lock call are the reference's, so a reference
process and a process of this package refuse each other (tests/proofs/test_lease.py).

USE AS IS `fcntl.flock` with `LOCK_EX | LOCK_NB` (https://docs.python.org/3.13/library/fcntl.html,
read 2026-11-11). Per flock(2) (https://man7.org/linux/man-pages/man2/flock.2.html, read
2026-11-11) a held lock fails a non-blocking request with EWOULDBLOCK (`BlockingIOError`,
measured), and the lock goes only with LOCK_UN or the close of the open file description's last
descriptor, as at process exit.

Ownership ends at the process boundary: a lease records the pid that acquired it, and a hook
registered with `os.register_at_fork(after_in_child=...)`
(https://docs.python.org/3.13/library/os.html, read 2026-11-11) closes every live lease's
inherited descriptor in a forked child, so the child holds nothing and the parent's `close`
still releases the lock. One lock per lease serialises `acquire`, `close` and `bind_pacer`.
A module lock spans each descriptor's open or close and its record, and the `before` hook takes
it, as CPython's `logging` does (v3.13.18, `Lib/logging/__init__.py:231-279`): a fork from
another thread waits until the descriptor is recorded or closed, so a child sees every copy it
inherits. It is reentrant, so a fork from the thread inside the window (a signal
handler) proceeds, and that child may keep an unrecorded copy until it exits.

Not covered, fail-closed: "a KeyboardInterrupt may appear at any point during execution" (the
signal module's "Note on Signal Handlers and Exceptions",
https://docs.python.org/3.13/library/signal.html, read 2026-11-11). One delivered between
`os.open` returning and its result being bound, or between `close` taking the descriptor and
closing it, leaves the descriptor open and unowned: the lock stays until the process ends and
nobody holds it, never two writers. A lease dropped without `close` also keeps its descriptor;
`close` is explicit, and `reserve_stream` is the supported use.

This module reserves; it never opens a stream, a socket or a service.
"""

import fcntl
import hashlib
import os
import threading
import weakref
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

# The reference locks this broker's accounts under a fixed directory of its own and ignores the
# root it is given, so this package, which honours the root, would lock a different file and two
# writers would both believe they hold the account. This package has no such account: refuse it.
REFUSED_BROKER = "INTERACTIVE_BROKERS"


class LeaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class AccountScope:
    broker: str
    account: str

    def __post_init__(self) -> None:
        if type(self.broker) is not str or type(self.account) is not str:
            raise ValueError("broker and account must be strings")
        if not self.broker or not self.account:
            raise ValueError("broker and account required")
        if ":" in self.broker:  # so that `broker:account` names exactly one pair
            raise ValueError("a broker name may not contain a colon")
        if self.broker == REFUSED_BROKER:
            raise LeaseError(f"{REFUSED_BROKER} is locked outside the given root; not leased here")

    @property
    def key(self) -> str:
        return f"{self.broker}:{self.account}"


_LIVE: "weakref.WeakSet[WriterLease]" = weakref.WeakSet()

# Held from before a descriptor is opened or taken out of a lease until it is recorded or closed,
# and by a forking thread from `before` to `after_in_parent`, so no fork lands in between; the
# pattern of `logging`'s module lock. Reentrant, so a fork from the holding thread cannot wait on
# itself. Order: a lease's `_lock`, then this; the fork hooks take only this.
_GUARD = threading.RLock()


def _prepare_fork() -> None:
    try:  # as `logging._prepareFork`: an asynchronous exception must not abandon the lock
        _GUARD.acquire()
    except BaseException:
        _GUARD.release()
        raise


def _after_fork_in_parent() -> None:
    _GUARD.release()


def _forget_in_forked_child() -> None:
    global _GUARD
    _GUARD = threading.RLock()  # the parent's forking thread held the old one
    for lease in list(_LIVE):
        lease._lock = threading.Lock()  # another thread may have held it at the fork
        fd, lease.fd = lease.fd, None
        lease._token = lease._pid = lease.pacer = None
        if fd is not None:
            with suppress(OSError):
                os.close(fd)  # the parent's copy keeps the lock


os.register_at_fork(
    before=_prepare_fork,
    after_in_parent=_after_fork_in_parent,
    after_in_child=_forget_in_forked_child,
)


class WriterLease:
    """One writer per account scope and lock root, across threads, lanes and processes."""

    def __init__(self, scope: AccountScope, root: Path, writer: str) -> None:
        self.scope, self.root, self.writer = scope, root, writer
        self.fd: int | None = None
        self.pacer: Any = None
        self._token: str | None = None
        self._pid: int | None = None
        self._lock = threading.Lock()
        _LIVE.add(self)

    @property
    def held(self) -> bool:
        return self.fd is not None and self._pid == os.getpid()

    @property
    def token(self) -> str | None:
        return self._token if self.held else None

    def _clear(self) -> int | None:
        fd, self.fd = self.fd, None
        self._token = self._pid = self.pacer = None
        return fd

    def acquire(self) -> None:
        with self._lock:
            if self.held:
                raise LeaseError("writer already holds lease")
            with _GUARD:  # no fork between a descriptor's open or close and its record
                stale = self._clear()  # a copy inherited by a fork the hook did not see
                if stale is not None:
                    os.close(stale)
                token = uuid4().hex
                self.root.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256(self.scope.key.encode()).hexdigest()
                fd = None
                try:
                    fd = os.open(self.root / f"{digest}.lock", os.O_RDWR | os.O_CREAT, 0o600)
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        raise LeaseError("account already has a writer") from None
                    self._token, self._pid = token, os.getpid()
                    self.fd = fd  # published last
                except BaseException:
                    self._clear()
                    if fd is not None:
                        os.close(fd)
                    raise

    def close(self) -> None:
        with self._lock, _GUARD:
            fd = self._clear()  # out of the object first, so a retry never closes a reused number
            if fd is not None:
                os.close(fd)

    def bind_pacer(self, pacer: Any) -> None:
        with self._lock:
            if not self.held or (self.pacer is not None and self.pacer is not pacer):
                raise LeaseError("account writer must use one shared pacer")
            self.pacer = pacer


@contextmanager
def reserve_stream(
    scope: AccountScope, root: Path, *, existing_streams: int, source: str, writer: str
) -> Generator[WriterLease]:
    """Hold the account's lease for the block; the caller opens its one stream inside it."""
    if (
        type(existing_streams) is not int
        or existing_streams != 0
        or type(source) is not str
        or not source.strip()
    ):
        raise ValueError("sourced zero existing streams required; never a second stream")
    lease = WriterLease(scope, root, writer)
    try:
        lease.acquire()
        yield lease
    finally:
        lease.close()
