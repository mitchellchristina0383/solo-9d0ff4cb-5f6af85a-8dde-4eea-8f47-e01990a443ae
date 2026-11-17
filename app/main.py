"""FastAPI application: dependency-alert API."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .cursors import rotate_secret
from .database import close_connection, get_connection, init_db, write_tx
from .errors import (
    GraphError,
    InvalidCursor,
    NoGraph,
    ReportConflict,
    StaleCursor,
    UnknownService,
)
from .graph_service import (
    MAX_PAGE_LIMIT,
    get_graph,
    query_alerts,
    replace_graph,
)
from .report_service import submit_report
from .schemas import (
    AlertsResponse,
    GraphPayload,
    GraphResponse,
    ReplaceResponse,
    ReportPayload,
    ReportResponse,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Dependency Alert API",
    version="1.0.0",
    description=(
        "Service dependency graph + fault/recovery reports with monotonic "
        "sequence numbers and upstream-root-cause alert suppression."
    ),
    lifespan=lifespan,
)


@app.middleware("http")
async def _db_session(request: Request, call_next):
    # One SQLite connection per request (thread-local); always released.
    get_connection()
    try:
        return await call_next(request)
    finally:
        close_connection()


@app.exception_handler(GraphError)
async def _graph_error(_: Request, exc: GraphError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": str(exc)},
    )


@app.exception_handler(ReportConflict)
async def _conflict(_: Request, exc: ReportConflict):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": str(exc)},
    )


@app.exception_handler(UnknownService)
async def _unknown(_: Request, exc: UnknownService):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": str(exc)},
    )


@app.exception_handler(NoGraph)
async def _no_graph(_: Request, exc: NoGraph):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": str(exc)},
    )


@app.exception_handler(InvalidCursor)
async def _invalid_cursor(_: Request, exc: InvalidCursor):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": str(exc)},
    )


@app.exception_handler(StaleCursor)
async def _stale_cursor(_: Request, exc: StaleCursor):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": str(exc)},
    )


@app.post("/graph", response_model=ReplaceResponse, status_code=200)
def put_graph(payload: GraphPayload) -> ReplaceResponse:
    """Replace the **whole** dependency graph.

    Invalid payloads (cycles, unknown references, duplicate names/edges) are
    rejected and the previously committed graph is retained.  States of
    same-named services survive the replacement.
    """
    return ReplaceResponse(**replace_graph(payload))


@app.get("/graph", response_model=GraphResponse)
def read_graph() -> GraphResponse:
    """Return the currently committed graph."""
    return get_graph()


@app.post("/reports", response_model=ReportResponse, status_code=200)
def post_report(payload: ReportPayload) -> ReportResponse:
    """Report a service fault or recovery with a per-service increasing seq."""
    return ReportResponse(**submit_report(payload))


@app.get(
    "/alerts",
    response_model=AlertsResponse,
    # Keeps legacy responses byte-for-byte identical: the optional pagination
    # block is omitted entirely unless the result is actually paged.
    response_model_exclude_unset=True,
    responses={
        400: {"description": "Malformed or forged pagination cursor"},
        409: {"description": "Stale cursor (graph revision or fault state changed)"},
    },
)
def read_alerts(
    limit: int | None = Query(
        default=None,
        ge=1,
        le=MAX_PAGE_LIMIT,
        description=(
            "Maximum suppression paths per page. Omit it for legacy behaviour: "
            "small graphs return every path without a pagination block; large "
            "ones return the first page plus a next_cursor."
        ),
    ),
    cursor: str | None = Query(
        default=None,
        description="Opaque next_cursor token returned by a previous /alerts page.",
    ),
) -> AlertsResponse:
    """Return actionable faults, suppressed faults and their upstream root causes.

    Fault classification (actionable / suppressed / root cause) is unchanged;
    only the potentially exponential path lists are paged.  Each cursor pins
    the graph revision and fault-state version it was issued for: a graph
    replacement or an applied report invalidates it (409 stale_cursor), while
    duplicate retries and rejected reports do not.
    """
    return AlertsResponse(**query_alerts(limit=limit, cursor=cursor))


@app.post("/admin/reset", status_code=200)
def admin_reset() -> dict:
    """Clear all data (test/operations helper)."""
    with write_tx() as conn:
        conn.execute("DELETE FROM events")
        conn.execute("DELETE FROM service_state")
        conn.execute("DELETE FROM edges")
        conn.execute("DELETE FROM nodes")
        conn.execute("DELETE FROM graph_meta")
        # Rotate the cursor secret as well so tokens issued before the reset
        # cannot be replayed against the freshly initialised database.
        rotate_secret(conn)
    return {"reset": True}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
