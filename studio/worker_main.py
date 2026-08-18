"""Long-running worker entrypoint for Content Studio.

Constructs the production :class:`~studio.worker.StageDispatcher` from the
shipped :func:`~studio.handlers.build_dispatcher` and drives it via
:meth:`~studio.worker.StageDispatcher.run` until SIGTERM / SIGINT.

The module exposes :func:`main` so the systemd unit (``ExecStart=python -m
studio.worker_main``) and the Docker worker service (``CMD [...] python -m
studio.worker_main``) share the exact same code path. Tests drive the
dispatcher directly via :func:`build_dispatcher`; this entrypoint only adds
the signal handling and structured log lines the operator runbook expects
to tail.

Environment variables the entrypoint honours:

* ``STUDIO_WORKER_ID`` — overrides the auto-generated worker id. Useful
  when multiple workers run side-by-side and the operator wants stable
  names in ``stage_jobs`` lease rows.
* ``STUDIO_LOG_LEVEL`` — log level for the ``studio.worker_main`` logger.
  Defaults to ``INFO``.

The entrypoint refuses to start when ``STUDIO_DATABASE_URL`` /
``CONTENT_STUDIO_DB`` are not configured: a worker pointed at the wrong
database would silently absorb jobs it has no operator to recover from.
The check is cheap (a single ``Settings()`` instantiation) and surfaces a
clear error before any lease claim happens.

NOTE: :class:`~studio.worker.StageDispatcher.run` accepts a ``heartbeats``
buffer that the brief documents for long-running lease extension. The
current implementation handles each stage synchronously inside
:meth:`~studio.worker.StageDispatcher.dispatch_once`, so the lease window
(``studio.jobs.LEASE_SECONDS`` = 900 s) comfortably covers a single stage.
Future stages that exceed that budget can wire the heartbeat callback
without touching this entrypoint.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import uuid
from typing import Any

from sqlalchemy.orm import sessionmaker

from studio.config import Settings
from studio.db import get_engine
from studio.handlers import HandlerContext, build_dispatcher
from studio.providers import AnthropicProvider, HttpSearchProvider
from studio.providers.base import ModelProviderError

logger = logging.getLogger("studio.worker_main")


def _resolve_worker_id() -> str:
    """Pick a stable per-process worker id; fall back to a fresh one when unset."""

    explicit = os.environ.get("STUDIO_WORKER_ID")
    if explicit:
        return explicit
    return f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


def _build_provider(settings: Settings) -> tuple[Any, HttpSearchProvider | None]:
    """Construct the production model + search providers.

    The provider is intentionally constructed at startup (not lazily on the
    first job): a missing API key fails fast so the operator sees the error
    in the systemd journal rather than every dispatched job failing with a
    less actionable ``ModelProviderError``.
    """

    try:
        model = AnthropicProvider.from_settings(settings)
    except AttributeError:
        # The Anthropic adapter (Tasks 1–13) does not yet expose a
        # ``from_settings`` helper; fall back to constructing the client
        # directly so the entrypoint does not block the deploy.
        from anthropic import Anthropic as AnthropicClient

        model = AnthropicProvider(AnthropicClient())
    search = (
        HttpSearchProvider(settings) if settings.search_provider_url else None
    )
    return model, search


def main(argv: list[str] | None = None) -> int:
    """Block on the dispatch loop until a signal is received.

    ``argv`` is reserved for future flags (e.g. ``--once`` to drain the
    queue and exit). The current implementation does not consume it.
    """

    logging.basicConfig(
        level=os.environ.get("STUDIO_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = Settings()
    if not settings.database_url:
        logger.error("STUDIO_DATABASE_URL is not configured; refusing to start")
        return 2

    engine = get_engine()
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        provider, search = _build_provider(settings)
    except ModelProviderError as exc:
        logger.error("provider initialisation failed: %s", exc)
        return 3
    if search is None:
        logger.warning(
            "search_provider_url is empty; high-risk claim verification will "
            "fail closed until STUDIO_SEARCH_PROVIDER_URL is set"
        )

    session = factory()
    try:
        hctx = HandlerContext(provider=provider, search=search, session_factory=factory)
        dispatcher = build_dispatcher(hctx, session)
    finally:
        session.close()

    worker_id = _resolve_worker_id()
    shutdown = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: shutdown.set())
    signal.signal(signal.SIGINT, lambda *_: shutdown.set())

    logger.info("content-studio worker starting: worker_id=%s", worker_id)
    # ``heartbeats=[]`` disables the optional heartbeat buffer that the
    # dispatcher keeps for future long-running stages. The current stages
    # complete inside one dispatch cycle so a heartbeat is unnecessary.
    for _ in dispatcher.run(
        worker_id=worker_id,
        heartbeats=[],
        shutdown_event=shutdown,
    ):
        if shutdown.is_set():
            break
    logger.info("content-studio worker shutting down")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via systemd / docker
    raise SystemExit(main())


__all__ = ["main"]