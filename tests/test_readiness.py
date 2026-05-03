"""Unit tests for the runtime readiness probe (issue #184).

Covers the three transitions that ``/v1/readyz`` needs to stop
lying during Maya's boot window:

* ``process = True``    — always, once the Python interpreter is up.
* ``dispatcher = True`` — after ``register_inprocess_executor`` wires
  the in-process executor.
* ``dcc = True``        — after Maya's main thread pumps the first
  deferred no-op job (or synchronously on
  :class:`MayaStandaloneDispatcher`).

The tests avoid importing a real Maya — they use a fake dispatcher for
the "never pumps" case and :class:`MayaStandaloneDispatcher` for the
"flips synchronously" case, matching the harness style already used by
``tests/test_dispatcher.py``.
"""

from __future__ import annotations

from typing import Any, Callable, List
from unittest.mock import MagicMock

import pytest

from dcc_mcp_maya import (
    ENV_READINESS_TIMEOUT_SECS,
    MayaStandaloneDispatcher,
    ReadinessProbe,
    ReadinessReport,
    StaticReadiness,
    install_readiness,
    resolve_readiness_timeout_secs,
)

# ---------------------------------------------------------------------------
# ReadinessReport / StaticReadiness
# ---------------------------------------------------------------------------


class TestReadinessReport:
    """Immutable snapshot contract."""

    def test_default_starts_with_only_process_green(self) -> None:
        report = ReadinessReport()
        assert report.process is True
        assert report.dispatcher is False
        assert report.dcc is False
        assert report.is_ready() is False

    def test_is_ready_requires_all_three_bits(self) -> None:
        assert ReadinessReport(True, True, True).is_ready() is True
        assert ReadinessReport(True, True, False).is_ready() is False
        assert ReadinessReport(True, False, True).is_ready() is False
        assert ReadinessReport(False, True, True).is_ready() is False

    def test_to_dict_returns_json_friendly_payload(self) -> None:
        report = ReadinessReport(process=True, dispatcher=True, dcc=False)
        assert report.to_dict() == {
            "process": True,
            "dispatcher": True,
            "dcc": False,
        }


class TestStaticReadiness:
    """Mutable container used as the in-process authority."""

    def test_default_report_matches_three_state_contract(self) -> None:
        sr = StaticReadiness()
        report = sr.report()
        assert report.process is True
        assert report.dispatcher is False
        assert report.dcc is False

    def test_set_dispatcher_ready_flips_the_bit(self) -> None:
        sr = StaticReadiness()
        sr.set_dispatcher_ready(True)
        assert sr.report().dispatcher is True
        sr.set_dispatcher_ready(False)
        assert sr.report().dispatcher is False

    def test_set_dcc_ready_flips_the_bit(self) -> None:
        sr = StaticReadiness()
        sr.set_dcc_ready(True)
        assert sr.report().dcc is True

    def test_is_ready_requires_all_three(self) -> None:
        sr = StaticReadiness()
        assert sr.is_ready() is False
        sr.set_dispatcher_ready(True)
        assert sr.is_ready() is False
        sr.set_dcc_ready(True)
        assert sr.is_ready() is True

    def test_fully_ready_classmethod(self) -> None:
        sr = StaticReadiness.fully_ready()
        assert sr.is_ready() is True


# ---------------------------------------------------------------------------
# resolve_readiness_timeout_secs
# ---------------------------------------------------------------------------


class TestResolveReadinessTimeout:
    """Env-var + explicit-argument resolution."""

    def test_returns_none_when_nothing_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_READINESS_TIMEOUT_SECS, raising=False)
        assert resolve_readiness_timeout_secs() is None

    def test_explicit_argument_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_READINESS_TIMEOUT_SECS, "30")
        assert resolve_readiness_timeout_secs(5) == 5

    def test_reads_env_var_when_argument_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_READINESS_TIMEOUT_SECS, "12")
        assert resolve_readiness_timeout_secs() == 12

    def test_non_integer_env_collapses_to_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_READINESS_TIMEOUT_SECS, "twelve")
        assert resolve_readiness_timeout_secs() is None

    def test_non_positive_collapses_to_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_READINESS_TIMEOUT_SECS, "0")
        assert resolve_readiness_timeout_secs() is None
        monkeypatch.setenv(ENV_READINESS_TIMEOUT_SECS, "-5")
        assert resolve_readiness_timeout_secs() is None

    def test_empty_env_treated_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_READINESS_TIMEOUT_SECS, "   ")
        assert resolve_readiness_timeout_secs() is None

    def test_invalid_explicit_argument_returns_none(self) -> None:
        # Strings are not valid positive integers here.
        assert resolve_readiness_timeout_secs("oops") is None  # type: ignore[arg-type]
        assert resolve_readiness_timeout_secs(-1) is None


# ---------------------------------------------------------------------------
# Dispatcher probe helpers
# ---------------------------------------------------------------------------


class _NeverPumpingDispatcher:
    """Fake UI dispatcher that *accepts* jobs but never runs them.

    Mimics a Maya that has its HTTP listener up (process green) and the
    in-process executor attached (dispatcher green) but whose main thread
    is still booting — the deferred callback never fires, so ``dcc``
    must stay red.
    """

    def __init__(self) -> None:
        self.received: List[Any] = []

    def submit_async_callable(
        self,
        *,
        request_id: str,
        task: Callable[[], Any],
        affinity: str = "main",
        timeout_ms: int = 5_000,
        on_complete: Callable[[Any], None] | None = None,
        **_: Any,
    ) -> dict:
        # Record the call but never invoke the callback.
        self.received.append(
            {
                "request_id": request_id,
                "affinity": affinity,
                "has_callback": on_complete is not None,
            }
        )
        return {"request_id": request_id, "status": "pending"}


class _FakeConfig:
    """Shim around ``McpHttpConfig`` with just the attributes we inspect."""

    def __init__(self, *, has_readiness_attr: bool = False) -> None:
        if has_readiness_attr:
            self.readiness: Any = None  # placeholder until probe publishes


class _FakeServer:
    """Minimal surface that :class:`ReadinessProbe` touches."""

    def __init__(
        self,
        dispatcher: Any = None,
        *,
        config_has_readiness: bool = False,
    ) -> None:
        self._maya_dispatcher = dispatcher
        self._host_dispatcher = dispatcher
        self._config = _FakeConfig(has_readiness_attr=config_has_readiness)


# ---------------------------------------------------------------------------
# ReadinessProbe.bind — the core contract
# ---------------------------------------------------------------------------


class TestReadinessProbeBind:
    """Transitions driven by :meth:`ReadinessProbe.bind`."""

    def test_bind_with_no_dispatcher_leaves_both_bits_red(self) -> None:
        server = _FakeServer(dispatcher=None)
        probe = ReadinessProbe()
        bound = probe.bind(server)
        assert bound is False
        snap = probe.report()
        assert snap.process is True
        assert snap.dispatcher is False
        assert snap.dcc is False

    def test_bind_with_standalone_flips_both_bits_synchronously(self) -> None:
        """:class:`MayaStandaloneDispatcher` runs callbacks on the calling thread."""
        server = _FakeServer(dispatcher=MayaStandaloneDispatcher())
        probe = ReadinessProbe()
        bound = probe.bind(server)
        assert bound is True
        assert probe.report().is_ready() is True

    def test_bind_with_never_pumping_dispatcher_keeps_dcc_red(self) -> None:
        dispatcher = _NeverPumpingDispatcher()
        server = _FakeServer(dispatcher=dispatcher)
        probe = ReadinessProbe()
        bound = probe.bind(server)
        # We *did* accept the probe (returned True from default scheduler)
        # and flipped dispatcher=True, but the callback never fires so
        # dcc stays red.
        assert bound is True
        snap = probe.report()
        assert snap.dispatcher is True
        assert snap.dcc is False
        # The scheduler used the main-thread affinity as expected.
        assert dispatcher.received, "dispatcher should have recorded one submit"
        call = dispatcher.received[0]
        assert call["affinity"] == "main"
        assert call["has_callback"] is True

    def test_bind_is_idempotent(self) -> None:
        server = _FakeServer(dispatcher=MayaStandaloneDispatcher())
        probe = ReadinessProbe()
        assert probe.bind(server) is True
        # Second bind on the same server is a no-op and returns the
        # previously-recorded scheduling state.
        assert probe.bind(server) is True
        assert probe.report().is_ready() is True

    def test_custom_probe_scheduler_is_used(self) -> None:
        """Injected scheduler overrides the default :func:`submit_async_callable` path."""
        flipped: List[bool] = []

        def scheduler(_dispatcher: Any, on_done: Callable[[], None]) -> bool:
            # Fire the callback after asserting we were passed the dispatcher.
            on_done()
            flipped.append(True)
            return True

        probe = ReadinessProbe(probe_scheduler=scheduler)
        server = _FakeServer(dispatcher=MagicMock(name="unused-dispatcher"))
        assert probe.bind(server) is True
        assert flipped == [True]
        assert probe.report().is_ready() is True

    def test_bind_publishes_to_config_when_supported(self) -> None:
        """Forward-compatible publish hook for core 0.14.27."""
        server = _FakeServer(
            dispatcher=MayaStandaloneDispatcher(),
            config_has_readiness=True,
        )
        probe = ReadinessProbe()
        probe.bind(server)
        # The config attribute is our in-process StaticReadiness — the
        # eventual Rust binding will observe the same mutations.
        assert server._config.readiness is probe.readiness  # type: ignore[attr-defined]

    def test_bind_skips_config_publish_on_older_core(self) -> None:
        """Today's core (0.14.23 / 0.14.26) has no ``readiness`` attr — silent no-op."""
        server = _FakeServer(
            dispatcher=MayaStandaloneDispatcher(),
            config_has_readiness=False,
        )
        probe = ReadinessProbe()
        # Binding should still succeed — this is the whole point of
        # forward-compat wiring.
        assert probe.bind(server) is True
        assert not hasattr(server._config, "readiness")

    def test_mark_dispatcher_ready_is_idempotent(self) -> None:
        probe = ReadinessProbe()
        probe.mark_dispatcher_ready()
        probe.mark_dispatcher_ready()
        assert probe.report().dispatcher is True
        probe.mark_dispatcher_ready(False)
        assert probe.report().dispatcher is False

    def test_mark_dcc_ready_logs_info_on_flip(self) -> None:
        probe = ReadinessProbe()
        probe.mark_dcc_ready(True)
        assert probe.report().dcc is True


# ---------------------------------------------------------------------------
# install_readiness — module-level convenience
# ---------------------------------------------------------------------------


class TestInstallReadiness:
    def test_returns_probe_even_when_binding_fails(self) -> None:
        server = _FakeServer(dispatcher=None)
        probe = install_readiness(server)
        assert isinstance(probe, ReadinessProbe)
        # dispatcher/dcc stay red — the convenience helper never raises.
        snap = probe.report()
        assert snap.dispatcher is False
        assert snap.dcc is False

    def test_threads_timeout_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_READINESS_TIMEOUT_SECS, raising=False)
        server = _FakeServer(dispatcher=MayaStandaloneDispatcher())
        probe = install_readiness(server, timeout_secs=45)
        assert probe.timeout_secs == 45


# ---------------------------------------------------------------------------
# Integration with MayaMcpServer
# ---------------------------------------------------------------------------


class TestMayaMcpServerReadinessIntegration:
    """End-to-end: constructor + ``attach_dispatcher`` drive the probe."""

    def test_construction_without_dispatcher_is_red(self) -> None:
        from dcc_mcp_maya import MayaMcpServer

        server = MayaMcpServer(port=0)
        try:
            assert server.readiness_report() == {
                "process": True,
                "dispatcher": False,
                "dcc": False,
            }
            assert server.readiness is not None
        finally:
            # Ensure the Rust HTTP server thread is torn down even though
            # we never called start() — construction still spins up some
            # internal handles via the base class.
            try:
                server.stop()
            except Exception:  # noqa: BLE001
                pass

    def test_attach_dispatcher_flips_probe_green(self) -> None:
        from dcc_mcp_maya import MayaMcpServer

        server = MayaMcpServer(port=0)
        try:
            assert server.readiness_report()["dispatcher"] is False
            server.attach_dispatcher(MayaStandaloneDispatcher())
            snap = server.readiness_report()
            assert snap["dispatcher"] is True
            assert snap["dcc"] is True
        finally:
            try:
                server.stop()
            except Exception:  # noqa: BLE001
                pass

    def test_constructor_with_dispatcher_lands_all_green(self) -> None:
        from dcc_mcp_maya import MayaMcpServer

        server = MayaMcpServer(
            port=0,
            host_dispatcher=MayaStandaloneDispatcher(),
        )
        try:
            assert server.readiness_report() == {
                "process": True,
                "dispatcher": True,
                "dcc": True,
            }
        finally:
            try:
                server.stop()
            except Exception:  # noqa: BLE001
                pass

    def test_readiness_timeout_is_resolved_from_kwarg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dcc_mcp_maya import MayaMcpServer

        monkeypatch.delenv(ENV_READINESS_TIMEOUT_SECS, raising=False)
        server = MayaMcpServer(port=0, readiness_timeout_secs=42)
        try:
            assert server.readiness.timeout_secs == 42
        finally:
            try:
                server.stop()
            except Exception:  # noqa: BLE001
                pass

    def test_readiness_timeout_is_resolved_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dcc_mcp_maya import MayaMcpServer

        monkeypatch.setenv(ENV_READINESS_TIMEOUT_SECS, "17")
        server = MayaMcpServer(port=0)
        try:
            assert server.readiness.timeout_secs == 17
        finally:
            try:
                server.stop()
            except Exception:  # noqa: BLE001
                pass
