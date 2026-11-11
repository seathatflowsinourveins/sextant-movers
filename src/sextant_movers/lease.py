"""The account lease and the stream reservation: one writer, and so one stream, per account.

BUILD ON the reference at its commit 06562bec0898a7203f4eaf69086ced83d2477e6b:
`src/sextant/execution/custody.py:75-85` (`AccountScope`), `:163-205` (`WriterLease`) and
`src/sextant/tradingview/producer.py:33-47` (`reserve_stream`). For the same scope and root the
lock file path, its open flags and mode, and the lock call are the reference's, so a reference
process and a process of this package refuse each other (tests/proofs/test_lease.py).

USE AS IS the standard library's lock: `fcntl.flock` with `LOCK_EX | LOCK_NB`
(https://docs.python.org/3.13/library/fcntl.html, read 2026-11-11). flock(2)
(https://man7.org/linux/man-pages/man2/flock.2.html, read 2026-11-11): a held lock fails a
non-blocking request with EWOULDBLOCK, which Python raises as `BlockingIOError` (measured in the
proof); the lock belongs to the open file description and is released when its descriptors
close, which _exit(2) does for every descriptor when a process ends.

This module reserves; it never opens a stream, a socket or a service.
"""

import fcntl
import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
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
        if not self.broker or not self.account:
            raise ValueError("broker and account required")
        if self.broker == REFUSED_BROKER:
            raise LeaseError(f"{REFUSED_BROKER} is locked outside the given root; not leased here")

    @property
    def key(self) -> str:
        return f"{self.broker}:{self.account}"


class WriterLease:
    """One writer per account scope and lock root, across threads, lanes and processes."""

    def __init__(self, scope: AccountScope, root: Path, writer: str) -> None:
        self.scope, self.root, self.writer = scope, root, writer
        self.fd: int | None = None
        self.token: str | None = None
        self.pacer: Any = None

    @property
    def held(self) -> bool:
        return self.fd is not None

    def acquire(self) -> None:
        if self.held:
            raise LeaseError("writer already holds lease")
        self.root.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(self.scope.key.encode()).hexdigest()
        fd = os.open(self.root / f"{digest}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise LeaseError("account already has a writer") from None
        except BaseException:
            os.close(fd)  # any other failure leaves no descriptor behind
            raise
        self.fd, self.token = fd, uuid4().hex

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd, self.token = None, None
            self.pacer = None

    def bind_pacer(self, pacer: Any) -> None:
        if not self.held or (self.pacer is not None and self.pacer is not pacer):
            raise LeaseError("account writer must use one shared pacer")
        self.pacer = pacer


@contextmanager
def reserve_stream(
    scope: AccountScope, root: Path, *, existing_streams: int, source: str, writer: str
) -> Iterator[WriterLease]:
    """Hold the account's lease for the block; the caller opens its one stream inside it."""
    if (
        type(existing_streams) is not int
        or existing_streams != 0
        or type(source) is not str
        or not source.strip()
    ):
        raise ValueError("sourced zero existing streams required; never a second stream")
    lease = WriterLease(scope, root, writer)
    lease.acquire()
    try:
        yield lease
    finally:
        lease.close()
