"""The account lease and the stream reservation: one writer per account, across processes.

The reference is `WriterLease` and `reserve_stream` at its commit
06562bec0898a7203f4eaf69086ced83d2477e6b; `sextant_movers/lease.py` names its lines and sources.

Children are fresh interpreters (`sys.executable -I`) that answer on stdout and wait on stdin, or
a fork that answers on a pipe. Threads meet at events, barriers and a condition; interruptions are
monkeypatched standard-library calls. No step sleeps or measures time; every path is in `tmp_path`.
"""

import errno
import fcntl
import hashlib
import os
import select
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import sextant_movers.lease as lease_module
from sextant_movers.lease import AccountScope, LeaseError, WriterLease, reserve_stream

SCOPE = AccountScope("MADE_UP_BROKER", "made-up-account")
SRC = str(Path(lease_module.__file__).resolve().parents[1])
WAIT = 30  # seconds a child may take to answer before the test fails instead of hanging


def _digest(scope: AccountScope) -> str:
    return hashlib.sha256(scope.key.encode()).hexdigest()


# --- one process -------------------------------------------------------------------------------


def test_one_writer_per_scope_and_root_and_close_releases(tmp_path: Path) -> None:
    first = WriterLease(SCOPE, tmp_path, "first")
    second = WriterLease(SCOPE, tmp_path, "second")
    first.acquire()
    try:
        with pytest.raises(LeaseError, match="already holds"):
            first.acquire()
        with pytest.raises(LeaseError, match="already has a writer"):
            second.acquire()
        assert first.held and not second.held
        other_scope = WriterLease(AccountScope("MADE_UP_BROKER", "another"), tmp_path, "x")
        other_root = WriterLease(SCOPE, tmp_path / "elsewhere", "y")
        other_scope.acquire()
        other_root.acquire()
        assert other_scope.held and other_root.held
        other_scope.close()
        other_root.close()
    finally:
        first.close()
    assert not first.held
    second.acquire()
    assert second.held
    second.close()
    second.close()  # closing twice is harmless
    assert not second.held


def test_the_lock_file_is_the_key_digest_with_mode_0600(tmp_path: Path) -> None:
    root = tmp_path / "locks"
    lease = WriterLease(SCOPE, root, "w")
    lease.acquire()
    try:
        assert SCOPE.key == "MADE_UP_BROKER:made-up-account"
        assert [p.name for p in root.iterdir()] == [f"{_digest(SCOPE)}.lock"]
        assert (root / f"{_digest(SCOPE)}.lock").stat().st_mode & 0o777 == 0o600
    finally:
        lease.close()


def test_a_held_lock_fails_flock_with_blocking_io_error(tmp_path: Path) -> None:
    """Measures which exception Python raises for flock(2)'s EWOULDBLOCK on a second open."""
    lease = WriterLease(SCOPE, tmp_path, "w")
    lease.acquire()
    fd = os.open(tmp_path / f"{_digest(SCOPE)}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with pytest.raises(BlockingIOError) as caught:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert caught.value.errno in (errno.EWOULDBLOCK, errno.EAGAIN)
    finally:
        os.close(fd)
        lease.close()


def test_the_token_is_set_while_held_and_cleared_after(tmp_path: Path) -> None:
    lease = WriterLease(SCOPE, tmp_path, "w")
    assert lease.token is None
    lease.acquire()
    token = lease.token
    assert token is not None and len(token) == 32 and int(token, 16) >= 0
    lease.close()
    assert lease.token is None
    lease.acquire()
    assert lease.token not in (None, token)
    lease.close()


def test_a_held_lease_binds_one_pacer(tmp_path: Path) -> None:
    lease = WriterLease(SCOPE, tmp_path, "w")
    one, two = object(), object()
    with pytest.raises(LeaseError, match="one shared pacer"):
        lease.bind_pacer(one)
    lease.acquire()
    lease.bind_pacer(one)
    lease.bind_pacer(one)
    with pytest.raises(LeaseError, match="one shared pacer"):
        lease.bind_pacer(two)
    assert lease.pacer is one
    lease.close()
    assert lease.pacer is None
    lease.acquire()
    lease.bind_pacer(two)
    lease.close()


def test_the_broker_name_with_a_fixed_reference_root_is_refused(tmp_path: Path) -> None:
    with pytest.raises(LeaseError, match="INTERACTIVE_BROKERS"):
        AccountScope("INTERACTIVE_BROKERS", "made-up-account")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(("broker", "account"), [("", "a"), ("b", ""), ("", "")])
def test_broker_and_account_are_required(broker: str, account: str) -> None:
    with pytest.raises(ValueError, match="broker and account required"):
        AccountScope(broker, account)


@pytest.mark.parametrize(
    ("existing", "source"),
    [
        *[(bad, "s") for bad in (1, -1, 2, 0.0, True, False, "0", None)],
        *[(0, bad) for bad in ("", "   ", None)],
    ],
)
def test_reserve_stream_refuses_before_any_file(tmp_path: Path, existing, source) -> None:
    root = tmp_path / "locks"
    with pytest.raises(ValueError, match="zero existing streams"):
        with reserve_stream(SCOPE, root, existing_streams=existing, source=source, writer="w"):
            pass
    assert not root.exists()


def test_reserve_stream_yields_a_held_lease_and_closes_it(tmp_path: Path) -> None:
    with reserve_stream(SCOPE, tmp_path, existing_streams=0, source="s", writer="w") as held:
        assert held.held and held.scope == SCOPE and held.writer == "w"
        with pytest.raises(LeaseError, match="already has a writer"):
            WriterLease(SCOPE, tmp_path, "other").acquire()
    assert not held.held
    seen: list[WriterLease] = []
    with pytest.raises(RuntimeError, match="inside"):
        with reserve_stream(SCOPE, tmp_path, existing_streams=0, source="s", writer="w") as again:
            seen.append(again)
            raise RuntimeError("inside")
    assert len(seen) == 1 and not seen[0].held


# --- interruptions, threads and fork --------------------------------------------------------


def _track_descriptors(monkeypatch) -> tuple[list[int], list[int]]:
    opened: list[int] = []
    closed: list[int] = []
    real_open, real_close = os.open, os.close

    def recording_open(*args, **kwargs) -> int:
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def recording_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(os, "open", recording_open)
    monkeypatch.setattr(os, "close", recording_close)
    return opened, closed


def _raises(error: BaseException):
    def fail(*args, **kwargs):
        raise error

    return fail


def _interrupted_at_publication(*, after: bool) -> type[WriterLease]:
    """A lease whose publication of its descriptor is interrupted, before or after the store."""

    class Interrupted(WriterLease):
        def __setattr__(self, name: str, value) -> None:
            publishing = name == "fd" and value is not None
            if publishing and not after:
                raise KeyboardInterrupt
            super().__setattr__(name, value)
            if publishing:
                raise KeyboardInterrupt

    return Interrupted


@pytest.mark.parametrize("failure", ["uuid4", "flock", "before-publish", "after-publish"])
def test_a_failed_acquire_leaves_no_descriptor_and_no_holder(
    tmp_path: Path, monkeypatch, failure: str
) -> None:
    """From the open to the publication, any failure closes what it opened."""
    opened, closed = _track_descriptors(monkeypatch)
    lease_class = WriterLease
    if failure == "uuid4":
        monkeypatch.setattr(lease_module, "uuid4", _raises(OSError("no entropy")))
    elif failure == "flock":
        no_locks = OSError(errno.ENOLCK, "no locks available")
        monkeypatch.setattr(lease_module.fcntl, "flock", _raises(no_locks))
    else:
        lease_class = _interrupted_at_publication(after=failure == "after-publish")
    lease = lease_class(SCOPE, tmp_path, "w")
    with pytest.raises((OSError, KeyboardInterrupt)):
        lease.acquire()
    assert sorted(opened) == sorted(closed)
    assert not lease.held and lease.token is None
    monkeypatch.undo()
    contender = WriterLease(SCOPE, tmp_path, "contender")
    contender.acquire()
    contender.close()


def test_an_interrupted_close_never_closes_a_reused_descriptor(tmp_path: Path, monkeypatch) -> None:
    """The descriptor leaves the object before it is closed, so a retry closes nothing."""
    first = WriterLease(SCOPE, tmp_path, "first")
    first.acquire()
    number = first.fd
    real_close = os.close

    def close_then_interrupt(fd: int) -> None:
        real_close(fd)
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(os, "close", close_then_interrupt)
        with pytest.raises(KeyboardInterrupt):
            first.close()
    assert not first.held
    second = WriterLease(SCOPE, tmp_path, "second")
    second.acquire()
    assert second.fd == number  # the kernel hands out the lowest free number again
    first.close()
    assert second.held
    with pytest.raises(LeaseError, match="already has a writer"):
        WriterLease(SCOPE, tmp_path, "third").acquire()
    second.close()


class _ProbeLock:
    """Stands in for a lease's lock and counts each attempt to take it, under `progress`."""

    def __init__(self, progress: threading.Condition) -> None:
        self.lock, self.progress, self.attempts = threading.Lock(), progress, 0

    def __enter__(self) -> None:
        with self.progress:
            self.attempts += 1
            self.progress.notify_all()
        self.lock.acquire()

    def __exit__(self, *exc) -> None:
        self.lock.release()


def test_two_threads_closing_at_once_close_the_descriptor_once(tmp_path: Path, monkeypatch) -> None:
    """The first close pauses inside os.close; the second must wait or find nothing."""
    lease = WriterLease(SCOPE, tmp_path, "w")
    lease.acquire()
    number = lease.fd
    progress = threading.Condition()
    probe = _ProbeLock(progress)
    lease._lock = probe  # type: ignore[assignment]
    entered, go = threading.Event(), threading.Event()
    calls: list[int] = []
    errors: list[BaseException] = []
    done = [0]
    real_close = os.close

    def paused_close(fd: int) -> None:
        calls.append(fd)
        if len(calls) == 1:
            entered.set()
            assert go.wait(WAIT)
        real_close(fd)

    def close() -> None:
        try:
            lease.close()
        except BaseException as error:
            errors.append(error)
        finally:
            with progress:
                done[0] += 1
                progress.notify_all()

    monkeypatch.setattr(os, "close", paused_close)
    first = threading.Thread(target=close)
    first.start()
    assert entered.wait(WAIT)
    second = threading.Thread(target=close)
    second.start()
    with progress:  # the second close is at the lease's lock, or (unguarded) already finished
        assert progress.wait_for(lambda: probe.attempts >= 2 or done[0] >= 1, WAIT)
    go.set()
    first.join(WAIT)
    second.join(WAIT)
    monkeypatch.undo()
    assert calls == [number] and errors == [] and not lease.held
    other = WriterLease(SCOPE, tmp_path, "other")
    other.acquire()
    lease.close()
    with pytest.raises(LeaseError, match="already has a writer"):
        WriterLease(SCOPE, tmp_path, "third").acquire()
    other.close()


def test_two_threads_binding_at_once_bind_one_pacer(tmp_path: Path) -> None:
    """Each bind reads the pacer and waits there until the other bind reads it too or waits
    at the lease's lock, so an unguarded check-then-set would accept both."""
    progress = threading.Condition()
    probe = _ProbeLock(progress)
    reads = [0]

    class Watched(WriterLease):
        @property
        def pacer(self):  # type: ignore[override]
            with progress:
                value = self.watched  # what this check sees, read before it waits
                reads[0] += 1
                progress.notify_all()
                assert progress.wait_for(lambda: reads[0] >= 2 or probe.attempts >= 2, WAIT)
            return value

        @pacer.setter
        def pacer(self, value) -> None:
            self.watched = value

    lease = Watched(SCOPE, tmp_path, "w")
    lease.acquire()
    lease._lock = probe  # type: ignore[assignment]
    barrier = threading.Barrier(2)
    accepted: list[object] = []
    refused: list[object] = []

    def bind(pacer: object) -> None:
        barrier.wait(WAIT)
        try:
            lease.bind_pacer(pacer)
            accepted.append(pacer)
        except LeaseError:
            refused.append(pacer)

    threads = [threading.Thread(target=bind, args=(object(),)) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(WAIT)
    assert len(accepted) == 1 and len(refused) == 1 and lease.watched is accepted[0]
    lease.close()


def test_an_interrupt_right_after_the_reservation_acquires_releases_it(
    tmp_path: Path, monkeypatch
) -> None:
    """The reservation's cleanup covers its own acquire."""
    real_acquire = WriterLease.acquire

    def acquire_then_interrupt(self: WriterLease) -> None:
        real_acquire(self)
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(WriterLease, "acquire", acquire_then_interrupt)
        with pytest.raises(KeyboardInterrupt):
            with reserve_stream(SCOPE, tmp_path, existing_streams=0, source="s", writer="w"):
                pytest.fail("the block must not run")
    contender = WriterLease(SCOPE, tmp_path, "contender")
    contender.acquire()
    contender.close()


def test_a_forked_child_holds_nothing_and_the_parent_close_releases(tmp_path: Path) -> None:
    """Ownership ends at the process boundary. The child's inherited copy is closed at fork,
    and closing a copy of the open file description does not release the parent's lock."""
    lease = WriterLease(SCOPE, tmp_path, "parent")
    lease.acquire()
    report_r, report_w = os.pipe()
    go_r, go_w = os.pipe()
    pid = os.fork()
    if pid == 0:  # the child: report, then stay alive until the parent says go
        status = 1
        try:
            facts = [lease.held, lease.token is None, lease.fd is None]
            for use in (lambda: lease.bind_pacer(object()), lease.acquire):
                try:
                    use()
                    facts.append(False)
                except LeaseError:
                    facts.append(True)
            os.write(report_w, repr(facts).encode() + b"\n")
            os.read(go_r, 1)
            status = 0
        finally:
            os._exit(status)
    os.close(report_w)
    os.close(go_r)
    try:
        assert select.select([report_r], [], [], WAIT)[0], "the child did not answer"
        assert os.read(report_r, 200) == b"[False, True, True, True, True]\n"
        contender = WriterLease(SCOPE, tmp_path, "contender")
        with pytest.raises(LeaseError, match="already has a writer"):
            contender.acquire()
        lease.close()
        contender.acquire()  # the child is alive, waiting on its pipe
        assert os.waitpid(pid, os.WNOHANG) == (0, 0)
        contender.close()
    finally:
        os.write(go_w, b"x")
        os.close(go_w)
        os.close(report_r)
        _, status = os.waitpid(pid, 0)
        lease.close()
    assert os.waitstatus_to_exitcode(status) == 0


@pytest.mark.parametrize(
    ("broker", "account"), [(1, "a"), ("b", 2), (b"b", "a"), ("b", None), (None, None)]
)
def test_broker_and_account_must_be_strings(broker, account) -> None:
    """A non-string could format to a different key on each acquire."""
    with pytest.raises(ValueError, match="must be strings"):
        AccountScope(broker, account)


def test_a_colon_in_the_broker_is_refused_and_the_key_stays_the_references() -> None:
    """With no colon in the broker, `broker:account` names one pair, in the reference's form."""
    with pytest.raises(ValueError, match="colon"):
        AccountScope("MADE_UP:BROKER", "account")
    assert AccountScope("MADE_UP", "BROKER:account").key == "MADE_UP:BROKER:account"


def test_the_lock_descriptor_is_not_inheritable_across_exec(tmp_path: Path) -> None:
    """PEP 446: `os.open` makes a non-inheritable descriptor; only a fork without exec copies it."""
    lease = WriterLease(SCOPE, tmp_path, "w")
    lease.acquire()
    try:
        assert lease.fd is not None and os.get_inheritable(lease.fd) is False
    finally:
        lease.close()


def _same_file(fd: int, path: Path) -> bool:
    try:
        held, named = os.fstat(fd), os.stat(path)
    except OSError:
        return False
    return (held.st_dev, held.st_ino) == (named.st_dev, named.st_ino)


def _report_lock_descriptors(path: Path, report_w: int, go_r: int) -> None:
    """In a forked child: count the descriptors open on the lock file, report, wait, exit.

    /proc/self/fd lists every descriptor the process has open (proc(5)), and (st_dev, st_ino)
    from fstat names the file each one refers to, whatever path opened it."""
    status = 1
    try:
        count = sum(_same_file(int(name), path) for name in os.listdir("/proc/self/fd"))
        os.write(report_w, f"{count}\n".encode())
        os.read(go_r, 1)
        status = 0
    finally:
        os._exit(status)


class _GuardProbe:
    """Stands in for the module's fork guard, an RLock, and records the threads that tried it."""

    def __init__(self, progress: threading.Condition) -> None:
        self.lock, self.progress, self.tried = threading.RLock(), progress, set()

    def acquire(self) -> bool:
        with self.progress:
            self.tried.add(threading.get_ident())
            self.progress.notify_all()
        return self.lock.acquire()

    def release(self) -> None:
        self.lock.release()

    def __enter__(self) -> None:
        self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


@pytest.mark.parametrize("window", ["open", "close", "failed-acquire"])
def test_a_fork_from_another_thread_never_copies_an_unrecorded_descriptor(
    tmp_path: Path, monkeypatch, window: str
) -> None:
    """A thread stands between a descriptor's open or close and its record; another thread forks.

    The child must hold no copy of the lock file, so after the parent's close a contender acquires
    while the child is alive; and the fork must wait until the window has passed."""
    lock = tmp_path / f"{_digest(SCOPE)}.lock"
    lease = WriterLease(SCOPE, tmp_path, "parent")
    if window == "close":
        lease.acquire()
    progress = threading.Condition()
    probe = _GuardProbe(progress)
    monkeypatch.setattr(lease_module, "_GUARD", probe, raising=False)
    inside, go = threading.Event(), threading.Event()
    real_open, real_close = os.open, os.close
    errors: list[BaseException] = []
    forked: list[int] = []
    report_r, report_w = os.pipe()
    go_r, go_w = os.pipe()

    def paused_open(path, *args, **kwargs) -> int:
        fd = real_open(path, *args, **kwargs)
        if window == "open" and Path(path) == lock:
            inside.set()
            assert go.wait(WAIT)
        return fd

    def paused_close(fd: int) -> None:
        if window != "open" and not inside.is_set() and _same_file(fd, lock):
            inside.set()
            assert go.wait(WAIT)
        real_close(fd)

    def step() -> None:
        try:
            lease.close() if window == "close" else lease.acquire()
        except BaseException as error:
            errors.append(error)

    def fork() -> None:
        pid = os.fork()
        if pid == 0:
            _report_lock_descriptors(lock, report_w, go_r)
        with progress:
            forked.append(pid)
            progress.notify_all()

    monkeypatch.setattr(os, "open", paused_open)
    monkeypatch.setattr(os, "close", paused_close)
    if window == "failed-acquire":
        no_locks = OSError(errno.ENOLCK, "no locks available")
        monkeypatch.setattr(lease_module.fcntl, "flock", _raises(no_locks))
    stepper = threading.Thread(target=step)
    stepper.start()
    forker = threading.Thread(target=fork)
    try:
        assert inside.wait(WAIT), "the step never reached its window"
        forker.start()
        with progress:  # the fork either completed or waits at the guard
            assert progress.wait_for(lambda: forked or forker.ident in probe.tried, WAIT)
            forked_inside_the_window = bool(forked)
        go.set()
        stepper.join(WAIT)
        forker.join(WAIT)
        monkeypatch.undo()
        assert forked and forked[0] > 0, "the fork did not happen"
        assert select.select([report_r], [], [], WAIT)[0], "the child did not answer"
        copies = int(os.read(report_r, 20))
        lease.close()
        contender = WriterLease(SCOPE, tmp_path, "contender")
        try:
            contender.acquire()
            refused = False
        except LeaseError:
            refused = True
        assert os.waitpid(forked[0], os.WNOHANG) == (0, 0), "the child must still be alive"
        contender.close()
        assert (copies, refused, forked_inside_the_window) == (0, False, False)
        assert [type(e) for e in errors] == ([OSError] if window == "failed-acquire" else [])
    finally:
        go.set()
        monkeypatch.undo()
        os.write(go_w, b"x")
        for fd in (go_w, go_r, report_w, report_r):
            os.close(fd)
        if forked and forked[0] > 0:
            _, status = os.waitpid(forked[0], 0)
            assert os.waitstatus_to_exitcode(status) == 0
        lease.close()


# --- across processes --------------------------------------------------------------------------

LEASE_CHILD = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from sextant_movers.lease import AccountScope, LeaseError, WriterLease
lease = WriterLease(AccountScope(sys.argv[3], sys.argv[4]), Path(sys.argv[2]), "child")
try:
    lease.acquire()
except LeaseError:
    print("refused", flush=True)
    sys.exit(0)
print("held", flush=True)
sys.stdin.readline()
lease.close()
"""

# The reference's lock, written out from `src/sextant/execution/custody.py:185-192` at its commit.
REFERENCE_WAY_CHILD = """
import fcntl, hashlib, os, sys
from pathlib import Path
root = Path(sys.argv[2])
root.mkdir(parents=True, exist_ok=True)
digest = hashlib.sha256(f"{sys.argv[3]}:{sys.argv[4]}".encode()).hexdigest()
fd = os.open(root / f"{digest}.lock", os.O_RDWR | os.O_CREAT, 0o600)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    os.close(fd)
    print("refused", flush=True)
    sys.exit(0)
print("held", flush=True)
sys.stdin.readline()
os.close(fd)
"""

RESERVE_CHILD = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from sextant_movers.lease import AccountScope, reserve_stream
scope, root = AccountScope(sys.argv[3], sys.argv[4]), Path(sys.argv[2])
try:
    with reserve_stream(scope, root, existing_streams=0, source="t", writer="c") as lease:
        print("held", lease.held, flush=True)
        if sys.stdin.readline() == "raise\\n":
            raise RuntimeError("inside the block")
except RuntimeError:
    print("raised", lease.held, flush=True)
else:
    print("closed", lease.held, flush=True)
sys.stdin.readline()
"""

# A fork from the lease's own thread, inside the window between the open and the record.
SAME_THREAD_FORK_CHILD = """
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from sextant_movers.lease import AccountScope, WriterLease
real_open = os.open
def open_then_fork(*args, **kwargs):
    fd = real_open(*args, **kwargs)
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    print("forked", os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]), flush=True)
    return fd
os.open = open_then_fork
lease = WriterLease(AccountScope(sys.argv[3], sys.argv[4]), Path(sys.argv[2]), "child")
lease.acquire()
os.open = real_open
lease.close()
print("done", lease.held, flush=True)
"""

# The REAL reference lease, imported in the child from the directory `MOVERS_REFERENCE_SRC` names.
REFERENCE_CHILD = """
import sys
from pathlib import Path
from sextant.execution.custody import AccountScope, CustodyError, WriterLease
lease = WriterLease(AccountScope(sys.argv[3], sys.argv[4]), Path(sys.argv[2]), "reference")
try:
    lease.acquire()
except CustodyError:
    print("refused", flush=True)
    sys.exit(0)
print("held", flush=True)
sys.stdin.readline()
lease.close()
"""


def _start(program: str, root: Path, *, command=None, env=None) -> subprocess.Popen:
    argv = command or [sys.executable, "-I"]
    args = [*argv, "-c", program, SRC, str(root), SCOPE.broker, SCOPE.account]
    return subprocess.Popen(
        args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
    )


def _line(child: subprocess.Popen) -> str:
    assert child.stdout is not None
    ready, _, _ = select.select([child.stdout], [], [], WAIT)
    assert ready, "the child did not answer"
    return child.stdout.readline().decode().strip()


def _finish(child: subprocess.Popen) -> None:
    _, err = child.communicate(timeout=WAIT)
    assert child.returncode == 0, err.decode()


@pytest.mark.parametrize("ending", ["exit", "kill"])
def test_a_child_holding_the_lease_refuses_the_parent_until_it_ends(tmp_path: Path, ending) -> None:
    child = _start(LEASE_CHILD, tmp_path)
    try:
        assert _line(child) == "held"
        with pytest.raises(LeaseError, match="already has a writer"):
            WriterLease(SCOPE, tmp_path, "parent").acquire()
    finally:
        if ending == "kill":
            child.kill()
            child.communicate(timeout=WAIT)
        else:
            _finish(child)
    assert child.returncode == (-9 if ending == "kill" else 0)
    parent = WriterLease(SCOPE, tmp_path, "parent")
    parent.acquire()
    assert parent.held
    parent.close()


def test_a_parent_holding_the_lease_refuses_a_child(tmp_path: Path) -> None:
    parent = WriterLease(SCOPE, tmp_path, "parent")
    parent.acquire()
    try:
        child = _start(LEASE_CHILD, tmp_path)
        assert _line(child) == "refused"
        _finish(child)
    finally:
        parent.close()


def _mutual_refusal(tmp_path: Path, program: str, **start) -> None:
    """Both orders: the new lease refuses the child, and the child refuses the new lease."""
    parent = WriterLease(SCOPE, tmp_path, "parent")
    parent.acquire()
    try:
        child = _start(program, tmp_path, **start)
        assert _line(child) == "refused"
        _finish(child)
    finally:
        parent.close()
    child = _start(program, tmp_path, **start)
    try:
        assert _line(child) == "held"
        with pytest.raises(LeaseError, match="already has a writer"):
            parent.acquire()
        assert not parent.held
    finally:
        _finish(child)
    parent.acquire()
    parent.close()


def test_the_reference_way_of_locking_and_the_lease_refuse_each_other(tmp_path: Path) -> None:
    _mutual_refusal(tmp_path, REFERENCE_WAY_CHILD)


@pytest.mark.parametrize("ending", ["closed", "raised"])
def test_reserve_stream_holds_the_lease_for_its_whole_block(tmp_path: Path, ending) -> None:
    child = _start(RESERVE_CHILD, tmp_path)
    try:
        assert _line(child) == "held True"
        with pytest.raises(LeaseError, match="already has a writer"):
            WriterLease(SCOPE, tmp_path, "parent").acquire()
        assert child.stdin is not None
        child.stdin.write(b"raise\n" if ending == "raised" else b"go\n")
        child.stdin.flush()
        assert _line(child) == f"{ending} False"
        # The child is still alive: the block's end, not the process's, released the lease.
        assert child.poll() is None
        parent = WriterLease(SCOPE, tmp_path, "parent")
        parent.acquire()
        parent.close()
    finally:
        _finish(child)


def test_a_fork_from_the_lease_own_thread_inside_the_window_does_not_deadlock(
    tmp_path: Path,
) -> None:
    """The fork guard is reentrant, so a fork made inside the window by the thread that holds it
    (a signal handler, say) proceeds instead of waiting on itself."""
    child = _start(SAME_THREAD_FORK_CHILD, tmp_path)
    try:
        lines = [_line(child), _line(child)]
    except AssertionError:
        child.kill()
        raise
    _finish(child)
    assert lines == ["forked 0", "done False"]


@pytest.mark.local
def test_the_real_reference_lease_and_this_lease_refuse_each_other(tmp_path: Path) -> None:
    reference = os.environ.get("MOVERS_REFERENCE_SRC")
    if not reference:
        pytest.skip("set MOVERS_REFERENCE_SRC to the reference's exported src/ directory")
    # -s and -P keep the user's site and the cwd off the path; PYTHONPATH adds the export.
    command = [sys.executable, "-s", "-P"]
    _mutual_refusal(tmp_path, REFERENCE_CHILD, command=command, env={"PYTHONPATH": reference})
