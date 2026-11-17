"""Opaque, signed pagination cursors for :mod:`app.graph_service`.

A cursor pins the exact committed state its paths were computed from:

* ``rev``   -- the graph revision (a successful ``POST /graph`` changes it);
* ``sv``    -- the fault-state version (an *applied* report changes it;
  idempotent retries and rejected reports do not);
* ``off``   -- the global flat path offset of the next page;
* ``limit`` -- page size this paging walk was started with, so follow-up
  requests that only carry the cursor keep the original page size.

Cursors are HMAC-signed with a random secret kept inside the database, so
clients cannot forge or tamper with them.  A well-formed cursor whose
revision/state version no longer matches the committed state is rejected as
*stale*; malformed or forged tokens are rejected as *invalid*.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3

from .errors import InvalidCursor

_SECRET_BYTES = 32


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    try:
        padding = "=" * (-len(text) % 4)
        return base64.urlsafe_b64decode(text + padding)
    except (ValueError, TypeError) as exc:  # pragma: no cover - exercised via API
        raise InvalidCursor("cursor is not valid base64") from exc


def get_or_create_secret(conn: sqlite3.Connection) -> bytes:
    """Load the signing secret, generating and persisting one on first use."""
    row = conn.execute("SELECT cursor_secret FROM query_meta WHERE id = 1").fetchone()
    if row is not None and row["cursor_secret"]:
        return _b64d(row["cursor_secret"])
    secret = secrets.token_bytes(_SECRET_BYTES)
    conn.execute(
        "INSERT INTO query_meta (id, state_version, cursor_secret) "
        "VALUES (1, 0, ?) ON CONFLICT(id) DO UPDATE SET cursor_secret = excluded.cursor_secret",
        (_b64e(secret),),
    )
    return secret


def rotate_secret(conn: sqlite3.Connection) -> None:
    """Forget all previously issued cursors (used by the admin reset)."""
    conn.execute(
        "INSERT INTO query_meta (id, state_version, cursor_secret) VALUES (1, 0, ?) "
        "ON CONFLICT(id) DO UPDATE SET cursor_secret = excluded.cursor_secret, "
        "state_version = 0",
        (_b64e(secrets.token_bytes(_SECRET_BYTES)),),
    )


def encode_cursor(
    conn: sqlite3.Connection,
    revision: int,
    state_version: int,
    offset: int,
    limit: int,
) -> str:
    secret = get_or_create_secret(conn)
    body = _b64e(
        json.dumps(
            {"rev": revision, "sv": state_version, "off": offset, "limit": limit},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    sig = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64e(sig)}"


def decode_cursor(conn: sqlite3.Connection, token: str) -> dict:
    """Verify a cursor token and return its payload.

    Raises :class:`InvalidCursor` on any structural problem or signature
    mismatch.  Staleness against the committed state is checked separately by
    the caller (it needs DB access and a dedicated error code).
    """
    if not isinstance(token, str) or token.count(".") != 1:
        raise InvalidCursor("malformed pagination cursor")
    body_b64, sig_b64 = token.split(".")
    secret = get_or_create_secret(conn)
    expected = hmac.new(secret, body_b64.encode("ascii"), hashlib.sha256).digest()
    try:
        given = _b64d(sig_b64)
    except InvalidCursor:
        raise
    if not hmac.compare_digest(expected, given):
        raise InvalidCursor("cursor signature does not match")
    try:
        payload = json.loads(_b64d(body_b64).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidCursor("cursor payload is not valid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"rev", "sv", "off", "limit"}:
        raise InvalidCursor("cursor payload has unexpected fields")
    if not all(isinstance(payload[k], int) and not isinstance(payload[k], bool)
               for k in ("rev", "sv", "off", "limit")):
        raise InvalidCursor("cursor payload fields must be integers")
    if payload["rev"] < 1 or payload["sv"] < 0 or payload["off"] < 0 or payload["limit"] < 1:
        raise InvalidCursor("cursor payload contains non-positive values")
    return payload
