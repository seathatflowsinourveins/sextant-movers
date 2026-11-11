"""The account lease and the stream reservation: one writer per account, across processes.

The reference is `WriterLease` and `reserve_stream` at its commit
06562bec0898a7203f4eaf69086ced83d2477e6b (`src/sextant/execution/custody.py:75-85,163-205`,
`src/sextant/tradingview/producer.py:33-47`). Its lock is the standard library's `fcntl.flock`
(https://docs.python.org/3.13/library/fcntl.html) on a file named by the SHA-256 of the account
key; flock(2) (https://man7.org/linux/man-pages/man2/flock.2.html, both read 2026-11-11) ties the
lock to the open file description and releases it when its descriptors close.

Every child is a fresh interpreter (`sys.executable -I`, or for the reference proof one whose
`PYTHONPATH` names the reference) that reports on its stdout and waits on its stdin, so no step
sleeps. Every path is under pytest's `tmp_path`. Scope names are made up.
"""

import errno
import fcntl
import hashlib
import os
import select
import subprocess
import sys
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


def test_another_flock_error_closes_the_descriptor(tmp_path: Path, monkeypatch) -> None:
    closed: list[int] = []
    real_close = os.close

    def no_locks(fd: int, operation: int) -> None:
        raise OSError(errno.ENOLCK, "no locks available")

    def recording_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(lease_module.fcntl, "flock", no_locks)
    monkeypatch.setattr(lease_module.os, "close", recording_close)
    lease = WriterLease(SCOPE, tmp_path, "w")
    with pytest.raises(OSError, match="no locks"):
        lease.acquire()
    assert len(closed) == 1 and not lease.held and lease.token is None


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
    with pytest.raises(RuntimeError, match="inside"):
        with reserve_stream(SCOPE, tmp_path, existing_streams=0, source="s", writer="w") as again:
            raise RuntimeError("inside")
    assert not again.held


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


@pytest.mark.local
def test_the_real_reference_lease_and_this_lease_refuse_each_other(tmp_path: Path) -> None:
    reference = os.environ.get("MOVERS_REFERENCE_SRC")
    if not reference:
        pytest.skip("set MOVERS_REFERENCE_SRC to the reference's exported src/ directory")
    # -s and -P keep the user's site and the working directory off the child's path; PYTHONPATH,
    # set only in the child's own environment, is how it reaches the reference.
    command = [sys.executable, "-s", "-P"]
    _mutual_refusal(tmp_path, REFERENCE_CHILD, command=command, env={"PYTHONPATH": reference})
