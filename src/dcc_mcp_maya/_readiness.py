"""Runtime readiness probe wiring for :class:`MayaMcpServer` (issue #184).

Maya's embedded MCP HTTP server publishes itself to the ``FileRegistry``
long before Maya's main thread has finished booting.  During that window
``list_dcc_instances`` happily reports ``status: "available"``, any
``tools/call`` with ``affinity: main`` is accepted and then **blocks**
until Maya's main thread is pumping, and the gateway's first
auto-aggregation probe fails with "not a DCC MCP HTTP endpoint" because
the fresh HTTP listener has not answered its first request yet.

The upstream design (see the issue) is a three-state readiness probe —
``process`` / ``dispatcher`` / ``dcc`` — that the Rust skill-REST layer
serves via ``/v1/readyz``.  The Maya adapter owns the ``dispatcher`` and
``dcc`` transitions:

* ``dispatcher = true``  — after :meth:`register_inprocess_executor` has
  wired the in-process executor.
* ``dcc = true``         — after Maya's main thread has executed **one**
  cheap no-op job that we schedule via the UI dispatcher.  In
  ``mayapy`` / batch mode (``MayaStandaloneDispatcher``) this flips
  synchronously because every submit runs on the calling thread.

Core-side exposure
------------------
Until ``dcc-mcp-core`` exposes the ``StaticReadiness`` Python binding
(pending in 0.14.27), the Maya adapter still drives an **in-process**
readiness report.  Callers (tests, ``ReadinessProbe.report``) can consult
it directly.  When core ships the binding, :func:`ReadinessProbe.bind`
will detect the config attribute and publish the report to the Rust
layer in one line — no surrounding code changes required.

SOLID notes
-----------
* **Single Responsibility** — this module *only* tracks and publishes
  the three readiness bits; it never owns lifecycle or dispatcher state
  (those stay on :class:`MayaMcpServer`).
* **Open/Closed** — the dispatcher-probe strategy is injectable
  (:attr:`ReadinessProbe.probe_scheduler`), so tests can verify the
  flip without running a live Maya event loop.
* **Dependency Inversion** — ``register_inprocess_executor`` /
  ``submit_async_callable`` are looked up *on the passed object*, not
  imported at module scope, so an older core wheel or a fake dispatcher
  in a unit test is fully supported.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

#: Environment variable that bounds how long orchestrators are willing
#: to wait for a cold Maya before readiness is considered red.  Parsed
#: as a positive integer number of seconds.  Unset → no Maya-side
#: timeout; the gateway / orchestrator decides.
ENV_READINESS_TIMEOUT_SECS = "DCC_MCP_MAYA_READINESS_TIMEOUT_SECS"

#: Request ID used for the no-op probe job scheduled on the UI
#: dispatcher.  Kept stable so operators grepping the Maya log can
#: correlate the "readiness flip" line with the exact job.
READINESS_PROBE_REQUEST_ID = "dcc_mcp_maya__readiness__dcc_ready_probe"


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadinessReport:
    """Immutable three-state readiness snapshot.

    Mirrors the core-side ``ReadinessReport`` struct so the Maya adapter
    can serve honest values via the same REST contract
    (``GET /v1/readyz`` → ``{"process": ..., "dispatcher": ..., "dcc": ...}``).
    ``process`` is always ``True`` on the adapter side: when this object
    is alive, the Python interpreter is alive, so the HTTP listener's
    in-process plumbing has been spun up.  The gateway still gets to
    veto (e.g. port bind failed) via its own probe.
    """

    process: bool = True
    dispatcher: bool = False
    dcc: bool = False

    def is_ready(self) -> bool:
        """Return ``True`` when all three bits are green."""
        return self.process and self.dispatcher and self.dcc

    def to_dict(self) -> dict:
        """Return a plain ``dict`` suitable for JSON serialisation."""
        return {
            "process": self.process,
            "dispatcher": self.dispatcher,
            "dcc": self.dcc,
        }


# ---------------------------------------------------------------------------
# Mutable static readiness (Python analogue of the Rust ``StaticReadiness``)
# ---------------------------------------------------------------------------


class StaticReadiness:
    """Thread-safe mutable readiness container.

    The two setters are idempotent — flipping to ``True`` twice is a
    no-op; flipping back to ``False`` is accepted (callers that want to
    re-arm the probe after a Maya scene reload can do so).  A single
    ``threading.Lock`` guards the three booleans; the readers (``report``)
    take a short critical section and return an immutable dataclass.

    This class exists because core 0.14.23 / 0.14.26 do not yet expose
    a Python binding for their Rust ``StaticReadiness``.  Once the
    binding lands (tracked in core 0.14.27, see
    ``dcc-mcp-maya#184``) the Maya side can adopt the binding without
    changing the surface any caller of this module relies on — the two
    classes are intentionally shape-compatible.
    """

    def __init__(
        self,
        *,
        process: bool = True,
        dispatcher: bool = False,
        dcc: bool = False,
    ) -> None:
        self._lock = threading.Lock()
        self._process = bool(process)
        self._dispatcher = bool(dispatcher)
        self._dcc = bool(dcc)

    # ── Mutations ─────────────────────────────────────────────────────

    def set_dispatcher_ready(self, value: bool) -> None:
        """Flip the ``dispatcher`` bit."""
        with self._lock:
            self._dispatcher = bool(value)

    def set_dcc_ready(self, value: bool) -> None:
        """Flip the ``dcc`` bit."""
        with self._lock:
            self._dcc = bool(value)

    def set_process_ready(self, value: bool) -> None:
        """Flip the ``process`` bit (rarely useful from Python side)."""
        with self._lock:
            self._process = bool(value)

    # ── Reads ──────────────────────────────────────────────────────────

    def report(self) -> ReadinessReport:
        """Return an immutable snapshot of the three bits."""
        with self._lock:
            return ReadinessReport(
                process=self._process,
                dispatcher=self._dispatcher,
                dcc=self._dcc,
            )

    def is_ready(self) -> bool:
        """Convenience for ``self.report().is_ready()`` — single lock take."""
        with self._lock:
            return self._process and self._dispatcher and self._dcc

    @classmethod
    def fully_ready(cls) -> "StaticReadiness":
        """Return an all-green probe — matches the core-side constructor.

        Mostly useful in tests that want to bypass the probe entirely.
        Startup code should instead build ``StaticReadiness()`` (which
        defaults to ``dispatcher=False, dcc=False``) and let the probe
        flip the bits as each subsystem comes online.
        """
        return cls(process=True, dispatcher=True, dcc=True)


# ---------------------------------------------------------------------------
# Env-var resolution
# ---------------------------------------------------------------------------


def resolve_readiness_timeout_secs(
    readiness_timeout_secs: Optional[int] = None,
) -> Optional[int]:
    """Resolve :data:`ENV_READINESS_TIMEOUT_SECS` into a positive integer.

    Priority: explicit argument > env var > ``None``.  ``None`` means
    "no Maya-side timeout — let the orchestrator decide".  Invalid
    values (non-integer, ``<= 0``) collapse to ``None`` and log a
    warning so a typo never kills startup.
    """
    if readiness_timeout_secs is not None:
        try:
            val = int(readiness_timeout_secs)
        except (TypeError, ValueError):
            return None
        return val if val > 0 else None

    raw = os.environ.get(ENV_READINESS_TIMEOUT_SECS)
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        val = int(raw)
    except ValueError:
        logger.warning(
            "Ignoring invalid %s=%r (expected positive integer seconds)",
            ENV_READINESS_TIMEOUT_SECS,
            raw,
        )
        return None
    if val <= 0:
        logger.warning(
            "Ignoring non-positive %s=%r (expected positive integer seconds)",
            ENV_READINESS_TIMEOUT_SECS,
            raw,
        )
        return None
    return val


# ---------------------------------------------------------------------------
# Integration object — binds the probe to a :class:`MayaMcpServer`
# ---------------------------------------------------------------------------


ProbeScheduler = Callable[[Any, Callable[[], None]], bool]
"""Dispatcher-agnostic scheduler: ``(dispatcher, on_done) -> scheduled?``.

Returns ``True`` when ``on_done`` is guaranteed to eventually be called
(either synchronously on standalone dispatchers, or after the next pump
cycle on UI dispatchers).  Returns ``False`` when the dispatcher is a
shape we do not recognise and the caller should leave ``dcc`` red.
"""


def _default_probe_scheduler(dispatcher: Any, on_done: Callable[[], None]) -> bool:
    """Submit a no-op job on *dispatcher* and flip ``dcc`` in its callback.

    We prefer :meth:`submit_async_callable` because it's non-blocking —
    the caller returns immediately and the callback fires on the UI
    thread once Maya finishes booting.  On :class:`MayaStandaloneDispatcher`
    the callback fires synchronously on the calling thread, which
    mirrors the "dcc is ready the moment we're in mayapy" contract.

    The ``on_complete`` callback signature accepts a result dict per the
    upstream contract; we ignore the payload because we only care about
    the fact that the main thread executed *something*.
    """
    submit_async = getattr(dispatcher, "submit_async_callable", None)
    if submit_async is None:
        # Older dispatchers that only expose ``submit_callable`` block
        # the caller — that would stall server startup, so we refuse to
        # downgrade and let the probe stay red.
        logger.debug(
            "readiness: dispatcher %r has no submit_async_callable; leaving dcc=false",
            type(dispatcher).__name__,
        )
        return False

    def _on_complete(_result: Any) -> None:  # noqa: ARG001 — signature contract
        try:
            on_done()
        except Exception as exc:  # noqa: BLE001
            logger.debug("readiness: dcc-ready callback raised: %s", exc)

    try:
        submit_async(
            request_id=READINESS_PROBE_REQUEST_ID,
            task=lambda: None,
            affinity="main",
            timeout_ms=5_000,
            on_complete=_on_complete,
        )
    except TypeError:
        # Core 0.14.23 dispatchers that don't accept ``on_complete`` as
        # a kwarg — retry without it and poll the dispatcher instead.
        try:
            submit_async(
                request_id=READINESS_PROBE_REQUEST_ID,
                task=lambda: None,
                affinity="main",
                timeout_ms=5_000,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("readiness: submit_async_callable fallback failed: %s", exc)
            return False
        # Best-effort: we can't confirm the main thread actually ran
        # the probe, so leave ``dcc`` red.  Callers that want a hard
        # signal should use a dispatcher that supports ``on_complete``.
        return False
    except Exception as exc:  # noqa: BLE001
        logger.debug("readiness: submit_async_callable raised: %s", exc)
        return False
    return True


class ReadinessProbe:
    """Drive the three readiness bits across a :class:`MayaMcpServer` lifecycle.

    Usage (called once from ``server.register_builtin_actions``)::

        probe = ReadinessProbe()
        probe.bind(server)
        # ``probe.report()`` now transitions through:
        #   dispatcher=False, dcc=False   (construction)
        # → dispatcher=True,  dcc=False   (after executor is attached)
        # → dispatcher=True,  dcc=True    (after first main-thread pump)

    All transitions are idempotent — calling :meth:`bind` twice is safe
    (the second call is a no-op on the same server).

    Parameters
    ----------
    timeout_secs:
        Advisory Maya-side timeout (seconds) for how long a cold Maya
        can stall before callers should consider ``dcc`` permanently
        red.  Consumed by orchestration layers; this class does *not*
        auto-fail the probe when the timeout elapses — a hung Maya is
        better reported as "still booting" than as a synthetic error.
    probe_scheduler:
        Strategy for scheduling the dcc-ready probe on the attached
        dispatcher.  Override in tests to avoid depending on a real
        :class:`MayaUiDispatcher` pump.
    """

    def __init__(
        self,
        *,
        timeout_secs: Optional[int] = None,
        probe_scheduler: Optional[ProbeScheduler] = None,
    ) -> None:
        self.timeout_secs: Optional[int] = resolve_readiness_timeout_secs(
            timeout_secs,
        )
        self.readiness: StaticReadiness = StaticReadiness()
        self.probe_scheduler: ProbeScheduler = probe_scheduler or _default_probe_scheduler
        # Populated by :meth:`bind` so tests can assert what we wired.
        self.bound_server: Any = None
        self.bound_dispatcher: Any = None
        self.dcc_scheduled: bool = False

    # ── Public API ──────────────────────────────────────────────────────

    def report(self) -> ReadinessReport:
        """Return the current immutable readiness snapshot."""
        return self.readiness.report()

    def bind(self, server: Any) -> bool:
        """Wire the probe into *server*.

        Steps:

        1. If the server already has a live dispatcher
           (``server._maya_dispatcher`` is not ``None``), flip
           ``dispatcher=True`` immediately.  Otherwise :meth:`bind`
           returns having only wired what's possible; callers may
           re-invoke :meth:`mark_dispatcher_ready` once the dispatcher
           shows up.
        2. Publish :attr:`readiness` to the inner Rust config when the
           currently-installed ``dcc-mcp-core`` wheel exposes the
           ``readiness`` attribute on :class:`McpHttpConfig`.  Today
           (core 0.14.23 / 0.14.26) that attribute does not exist; the
           call is a silent no-op and will light up automatically when
           core 0.14.27 ships the Python binding.  The Maya-side
           in-process report remains authoritative for tests regardless.
        3. Schedule a no-op probe on the attached dispatcher so the
           first main-thread pump flips ``dcc=True``.  On
           :class:`MayaStandaloneDispatcher` this callback fires
           synchronously, so the flip is observed immediately.

        Returns
        -------
        bool
            ``True`` when binding fully succeeded (dispatcher already
            live and dcc probe scheduled).  ``False`` when the server
            has no dispatcher yet or the probe couldn't be scheduled —
            the readiness object is still usable and may be advanced
            later via :meth:`mark_dispatcher_ready` and
            :meth:`mark_dcc_ready`.
        """
        if self.bound_server is server:
            return self.dcc_scheduled
        self.bound_server = server

        # Step 2 — publish to the inner Rust config if supported.
        self._maybe_publish_to_config(server)

        # Step 1 — dispatcher attached already?
        dispatcher = getattr(server, "_maya_dispatcher", None)
        if dispatcher is None:
            dispatcher = getattr(server, "_host_dispatcher", None)
        if dispatcher is None:
            logger.debug(
                "readiness.bind: no dispatcher attached to %r yet; dispatcher/dcc stay red until one is wired",
                type(server).__name__,
            )
            return False

        self.bound_dispatcher = dispatcher
        self.mark_dispatcher_ready()

        # Step 3 — schedule the dcc probe.
        scheduled = self.probe_scheduler(dispatcher, self.mark_dcc_ready)
        self.dcc_scheduled = bool(scheduled)
        if not scheduled:
            logger.debug(
                "readiness.bind: dispatcher %r did not accept the probe; dcc stays red until another trigger flips it",
                type(dispatcher).__name__,
            )
        return self.dcc_scheduled

    def mark_dispatcher_ready(self, value: bool = True) -> None:
        """Flip the ``dispatcher`` bit.  Idempotent."""
        self.readiness.set_dispatcher_ready(value)
        if value:
            logger.debug("readiness: dispatcher=true")

    def mark_dcc_ready(self, value: bool = True) -> None:
        """Flip the ``dcc`` bit.  Typically called from the probe callback."""
        self.readiness.set_dcc_ready(value)
        if value:
            logger.info("[maya] readiness: dcc-ready — main thread is pumping")

    # ── Internals ───────────────────────────────────────────────────────

    def _maybe_publish_to_config(self, server: Any) -> bool:
        """Best-effort publish of :attr:`readiness` to ``server._config``.

        Core 0.14.27 is expected to expose a ``readiness`` attribute on
        :class:`dcc_mcp_core.McpHttpConfig` (tracked by the companion
        core issue cited in #184).  Until then this method is a silent
        no-op — the Maya adapter still tracks readiness in-process for
        tests and any REST surface the adapter owns directly.

        Returns
        -------
        bool
            ``True`` when the config attribute was set successfully;
            ``False`` otherwise.  Failures are logged at debug level to
            keep startup quiet on older core wheels.
        """
        config = getattr(server, "_config", None)
        if config is None:
            return False
        # Inspect the attribute without touching it — ``hasattr`` also
        # guards against property getters that raise.
        if not hasattr(config, "readiness"):
            return False
        try:
            setattr(config, "readiness", self.readiness)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "readiness: could not publish probe to inner config: %s",
                exc,
            )
            return False
        logger.debug("readiness: probe published to inner Rust config")
        return True


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------


def install_readiness(
    server: Any,
    *,
    timeout_secs: Optional[int] = None,
    probe_scheduler: Optional[ProbeScheduler] = None,
) -> ReadinessProbe:
    """One-shot helper used by :class:`MayaMcpServer.register_builtin_actions`.

    Always returns a :class:`ReadinessProbe` — even when binding fails
    (no dispatcher yet, older core, etc.) — so callers can stash the
    probe on the server and consult :meth:`ReadinessProbe.report` from
    tests or diagnostic endpoints.
    """
    probe = ReadinessProbe(
        timeout_secs=timeout_secs,
        probe_scheduler=probe_scheduler,
    )
    probe.bind(server)
    return probe
