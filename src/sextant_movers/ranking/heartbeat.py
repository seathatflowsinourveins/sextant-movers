"""The heartbeat emitter: one beat per newly published eligible generation, never a made-up one.

A beat hands the injected sink two keyword-only ints: `at`, the generation's `known_at`, and
`received_ns`, the injected clock's reading. It is sent only for a READY, complete generation
whose expected and observed counts equal its rows, whose missing, invalid and late counts are
zero, and whose clocks run `ts_event <= input_cutoff <= known_at <= ts_init`; then only when
`0 <= at <= received_ns`, `received_ns - at` is within `heartbeat_max_age_s` (read through
`Registry.seconds`, which returns nanoseconds) and `at` is past the last sent one. A sink that
returns a falsy value or raises leaves the last sent `at` where it was; a raise is counted in
`sink_failures`, never raised. `heartbeat_signal` only names the engine signal a node beats on
(`publish_signal(name, value, ts_event=0)`, nautechsystems/nautilus_trader v2.0.0rc8
`d53849b216e20f2c122ca01b4dd0da377638b186` `python/nautilus_trader/common/__init__.pyi:720`);
nothing here publishes it. The engine's `heartbeat_interval_secs`
(`crates/common/src/msgbus/config.rs:80`) is the external Redis bus's timed liveness message
(`crates/infrastructure/src/redis/msgbus.rs:902-938`), not a freshness beat for a custom data
stream, so it is not used. Nothing here starts a stream, a socket, a thread or a node, and
nothing here places an order.

Source: the reference's `src/sextant/ranking/heartbeat.py` at
`06562bec0898a7203f4eaf69086ced83d2477e6b`, rebuilt value for value.
"""

from collections.abc import Callable
from typing import Protocol

from sextant_movers.ranking.generation import CoverageState, RankingGeneration
from sextant_movers.ranking.registry import Registry


def heartbeat_signal(feed: str) -> str:
    """The engine signal name a node beats on for one feed."""
    return f"ranking.heartbeat.{feed}"


class HeartbeatSink(Protocol):
    def __call__(self, *, at: int, received_ns: int) -> bool: ...


class HeartbeatEmitter:
    """Sends one beat per newly published eligible generation through the injected sink."""

    def __init__(
        self, *, registry: Registry, clock: Callable[[], int], sink: HeartbeatSink
    ) -> None:
        self._registry = registry
        self._clock = clock
        self._sink = sink
        self._last_at: int | None = None
        self.sink_failures = 0

    def emit(self, generation: RankingGeneration | None) -> bool:
        if not isinstance(generation, RankingGeneration) or not _eligible(generation):
            return False
        at, received_ns = generation.known_at, self._clock()
        if not self._receivable(at, received_ns):
            return False
        try:
            sent = self._sink(at=at, received_ns=received_ns)
        except Exception:  # a broken sink never fails the publication that called it
            self.sink_failures += 1
            return False
        if not sent:
            return False
        self._last_at = at
        return True

    def _receivable(self, at: object, received_ns: object) -> bool:
        if type(at) is not int or type(received_ns) is not int:
            return False
        if not 0 <= at <= received_ns:
            return False
        if received_ns - at > self._registry.seconds("heartbeat_max_age_s"):
            return False
        return self._last_at is None or at > self._last_at


def _eligible(generation: RankingGeneration) -> bool:
    coverage, rows = generation.coverage, len(generation.rows)
    return bool(
        coverage.state is CoverageState.READY
        and coverage.complete
        and coverage.expected_symbols == coverage.observed_symbols == rows
        and coverage.missing_symbols == coverage.invalid_symbols == coverage.late_symbols == 0
        and generation.ts_event
        <= generation.input_cutoff
        <= generation.known_at
        <= generation.ts_init
    )
