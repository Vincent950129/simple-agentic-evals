"""Per-user API keys, read from the token store the demo site issues them into.

Users get a key by signing in at the MAS-Orchestra demo and opening *MyAuthtoken*;
that app owns the store. This module reads credentials and, after a successful
verification, updates only the owner's ``last_used`` fields. Updates are throttled
and written through the authoritative SQLite store plus its atomic CSV mirror;
token, ownership, creation, and rotation fields are never changed here.

Two formats are understood, because the export has changed shape before: a CSV with
a ``token`` column, or a SQLite database containing any table with one. Whichever
paths exist are pooled, so a CSV export and a live database can be served at once.

Tokens are held as SHA-256 digests rather than plaintext: lookups become a
digest-to-digest comparison, which sidesteps the timing question that a plain
string compare against a dict of secrets would otherwise raise, and a proxy core
dump or stray log line can't leak anybody's key.

The store re-reads itself when a file's mtime or size changes, so a key issued a
minute ago works without restarting anything.

Failure is deliberately asymmetric:

* store **configured but unreadable** -> every request is denied, loudly. A gate
  that quietly falls open when its key list goes missing is worse than no gate,
  because nothing about the deployment looks wrong.
* store **not configured** -> this module reports ``enabled=False`` and the caller
  keeps whatever behaviour it had (shared ``EVAL_SERVICE_API_KEY``, or open).
"""

from __future__ import annotations

import csv
import hashlib
import os
import sqlite3
import threading
import time
from dataclasses import dataclass

__all__ = ["TokenStore", "Identity", "default_store", "SIGNUP_URL", "SIGNUP_HINT",
           "unauthorized_detail"]

#: Where users get a key. Surfaced in 401 bodies and on the landing page, because
#: "unauthorized" without a next step is a support ticket.
SIGNUP_URL = "https://mas-orchestra.salesforceresearch.ai/mas_r1/demo/"
SIGNUP_HINT = (f"sign in at {SIGNUP_URL} and open MyAuthtoken to get a key, then send it as "
               f"'Authorization: Bearer <key>'")


def unauthorized_detail(presented: bool, *, broken: bool = False) -> str:
    """The message a rejected caller sees.

    This goes in the response's ``detail`` field rather than only in a sibling
    field, because that is the one part of an error body every client surfaces --
    curl, requests, and the SDK's ``ServiceError`` alike. Guidance parked anywhere
    else is guidance the user never reads.

    ``presented`` distinguishes *no key* (probably never configured one) from *bad
    key* (probably rotated), because the two need different next steps.
    """
    if broken:
        return ("the API key gate is misconfigured on this deployment: its token store "
                "is unreadable, so no key can be verified right now. This is a server-side "
                "problem, not a problem with your key -- please report it.")
    if not presented:
        return (f"no API key provided. Sign in at {SIGNUP_URL} and open MyAuthtoken to get "
                f"one, then send it as 'Authorization: Bearer <key>' (or set "
                f"$EVAL_SERVICE_API_KEY, which EvalClient() reads automatically).")
    return (f"the API key provided was not recognized -- it may have been rotated or "
            f"revoked. Sign in at {SIGNUP_URL} and open MyAuthtoken to get a current one, "
            f"then send it as 'Authorization: Bearer <key>' (or set $EVAL_SERVICE_API_KEY).")

#: Eval-owned locations, in the order they are pooled. The DB is authoritative;
#: the CSV adds readable email/name labels. Override with
#: ``EVAL_SERVICE_AUTH_TOKENS`` (os.pathsep-separated).
_AUTH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auth")
DEFAULT_PATHS = (
    os.path.join(_AUTH_DIR, "authtokens.db"),
    os.path.join(_AUTH_DIR, "authtokens.csv"),
)

_RELOAD_DEBOUNCE_S = 2.0
_LAST_USED_RESOLUTION_S = float(
    os.environ.get("EVAL_SERVICE_AUTH_LAST_USED_RESOLUTION_SEC", "60")
)
_SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
_AUTHTOKENS_CSV_COLUMNS = [
    "user_sub", "email", "name", "token", "created_at", "created_at_epoch",
    "rotated_at", "rotated_at_epoch", "last_used", "last_used_epoch",
    "rotation_count",
]


@dataclass(frozen=True)
class Identity:
    """Who a valid token belongs to. Used for logging and rate accounting."""
    user_sub: str = ""
    email: str = ""
    name: str = ""
    source: str = ""

    @property
    def label(self) -> str:
        return self.email or self.name or self.user_sub or "unknown user"


def _digest(token: str) -> str:
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


def _iso(epoch: float | None) -> str:
    if epoch is None:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(float(epoch)))


def _dump_authtokens_csv(conn: sqlite3.Connection, path: str) -> None:
    """Atomically mirror the demo's authoritative authtokens/users tables."""
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if not {"authtokens", "users"}.issubset(tables):
        return
    rows = conn.execute(
        "SELECT t.user_sub, u.email, u.name, t.token, t.created_at, "
        "t.rotated_at, t.last_used, t.rotation_count "
        "FROM authtokens t LEFT JOIN users u ON u.sub=t.user_sub "
        "ORDER BY t.created_at ASC"
    ).fetchall()
    tmp = f"{path}.eval-service-{os.getpid()}.tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(_AUTHTOKENS_CSV_COLUMNS)
        for sub, email, name, token, created, rotated, used, count in rows:
            writer.writerow([
                sub, email or "", name or "", token,
                _iso(created), created if created is not None else "",
                _iso(rotated), rotated if rotated is not None else "",
                _iso(used), used if used is not None else "",
                int(count) if count is not None else 0,
            ])
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _mark_used_sqlite(path: str, token: str, now: float) -> bool:
    """Update a token row if present; return whether this DB owns the token."""
    conn = sqlite3.connect(path, timeout=5.0)
    try:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )]
        for table in tables:
            quoted = table.replace('"', '""')
            columns = {row[1] for row in conn.execute(
                f'PRAGMA table_info("{quoted}")'
            )}
            if not {"token", "last_used"}.issubset(columns):
                continue
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f'SELECT last_used FROM "{quoted}" WHERE token=?', (token,)
            ).fetchone()
            if row is None:
                conn.rollback()
                continue
            previous = row[0]
            if previous is not None and now - float(previous) < _LAST_USED_RESOLUTION_S:
                conn.rollback()
                return True
            conn.execute(
                f'UPDATE "{quoted}" SET last_used=? WHERE token=?', (now, token)
            )
            # Keep the human-readable CSV mirror aligned while holding SQLite's
            # cross-process write lock so a concurrent rotation cannot be lost.
            if table == "authtokens":
                csv_path = os.path.join(os.path.dirname(path), "authtokens.csv")
                try:
                    tables = {item[0] for item in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )}
                    if "users" in tables:
                        _dump_authtokens_csv(conn, csv_path)
                    elif os.path.exists(csv_path):
                        _mark_used_csv(csv_path, token, now)
                except OSError:
                    pass
            conn.commit()
            return True
        return False
    finally:
        conn.close()


def _mark_used_csv(path: str, token: str, now: float) -> bool:
    """Fallback for deployments whose only configured token store is a CSV."""
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if not {"token", "last_used", "last_used_epoch"}.issubset(fieldnames):
        return False
    found = False
    for row in rows:
        if _digest(row.get("token") or "") != _digest(token):
            continue
        found = True
        previous = row.get("last_used_epoch")
        if previous:
            try:
                if now - float(previous) < _LAST_USED_RESOLUTION_S:
                    return True
            except ValueError:
                pass
        row["last_used"] = _iso(now)
        row["last_used_epoch"] = str(now)
        break
    if not found:
        return False
    tmp = f"{path}.eval-service-{os.getpid()}.tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return True


def _read_csv(path: str) -> dict[str, Identity]:
    out: dict[str, Identity] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            token = (row.get("token") or "").strip()
            if not token:
                continue
            out[_digest(token)] = Identity(
                user_sub=(row.get("user_sub") or "").strip(),
                email=(row.get("email") or "").strip(),
                name=(row.get("name") or "").strip(),
                source=os.path.basename(path),
            )
    return out


def _read_sqlite(path: str) -> dict[str, Identity]:
    """Pull tokens from whichever table has a ``token`` column.

    The demo app owns this schema and has changed it before, so the table name is
    discovered rather than assumed; sibling columns are used when present.
    """
    out: dict[str, Identity] = {}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)          # never create it
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")]
        for table in tables:
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
            if "token" not in cols:
                continue
            pick = [c for c in ("token", "user_sub", "email", "name") if c in cols]
            sel = ", ".join(f'"{c}"' for c in pick)
            for row in con.execute(f'SELECT {sel} FROM "{table}"'):
                rec = dict(zip(pick, row))
                token = str(rec.get("token") or "").strip()
                if not token:
                    continue
                out[_digest(token)] = Identity(
                    user_sub=str(rec.get("user_sub") or "").strip(),
                    email=str(rec.get("email") or "").strip(),
                    name=str(rec.get("name") or "").strip(),
                    source=os.path.basename(path),
                )
    finally:
        con.close()
    return out


class TokenStore:
    """Pooled, hot-reloading view of the configured token files."""

    def __init__(self, paths: list[str] | tuple[str, ...] | None = None,
                 *, configured: bool | None = None) -> None:
        env = os.environ.get("EVAL_SERVICE_AUTH_TOKENS", "").strip()
        if paths is not None:
            self.paths = [str(p) for p in paths if str(p).strip()]
            self._explicit = configured if configured is not None else bool(self.paths)
        elif env:
            self.paths = [p for p in env.split(os.pathsep) if p.strip()]
            self._explicit = True          # asked for by name -> must work
        else:
            self.paths = [p for p in DEFAULT_PATHS if os.path.exists(p)]
            self._explicit = False         # auto-detected -> absent just means off
        self._lock = threading.Lock()
        self._tokens: dict[str, Identity] = {}
        self._stamps: dict[str, tuple[float, int]] = {}
        self._checked = 0.0
        self._last_used_attempt: dict[str, float] = {}
        self.last_used_error: str | None = None
        self.error: str | None = None
        self.loaded_at: float = 0.0
        self._load()

    # -- loading ------------------------------------------------------------ #
    def _fingerprint(self) -> dict[str, tuple[float, int]]:
        out = {}
        for p in self.paths:
            try:
                st = os.stat(p)
                out[p] = (st.st_mtime, st.st_size)
            except OSError:
                out[p] = (0.0, -1)
        return out

    def _load(self) -> None:
        tokens: dict[str, Identity] = {}
        loaded: list[tuple[str, dict[str, Identity]]] = []
        errors: list[str] = []
        for p in self.paths:
            try:
                if not os.path.exists(p):
                    errors.append(f"{p}: missing")
                    continue
                data = (_read_sqlite(p) if p.endswith(_SQLITE_SUFFIXES)
                        else _read_csv(p))
                loaded.append((p, data))
            except Exception as e:                      # noqa: BLE001
                errors.append(f"{p}: {type(e).__name__}: {e}")
        # When an authtokens.db + authtokens.csv pair exists, the DB decides
        # which credentials are valid and the CSV only enriches matching rows
        # with email/name labels. A stale CSV can therefore never resurrect a
        # key that was rotated out of the authoritative DB.
        authoritative_dirs = {
            os.path.dirname(p) for p in self.paths
            if os.path.basename(p) == "authtokens.db"
        }
        for path, data in loaded:
            if (os.path.dirname(path) in authoritative_dirs
                    and os.path.basename(path) == "authtokens.csv"):
                continue
            tokens.update(data)
        for path, data in loaded:
            if not (os.path.dirname(path) in authoritative_dirs
                    and os.path.basename(path) == "authtokens.csv"):
                continue
            for digest, label in data.items():
                authoritative = tokens.get(digest)
                if authoritative is None:
                    continue
                tokens[digest] = Identity(
                    user_sub=authoritative.user_sub or label.user_sub,
                    email=label.email,
                    name=label.name,
                    source=f"{authoritative.source}+{label.source}",
                )
        with self._lock:
            self._stamps = self._fingerprint()
            self._checked = time.time()
            # Only a total failure is fatal. If one of several sources is broken but
            # others yielded keys, serve those and keep the error visible in status().
            if tokens:
                self._tokens, self.error = tokens, ("; ".join(errors) or None)
            else:
                self._tokens = {}
                self.error = ("; ".join(errors)
                              or ("no tokens found in " + ", ".join(self.paths)
                                  if self.paths else "no token store configured"))
            self.loaded_at = time.time()

    def _maybe_reload(self) -> None:
        now = time.time()
        with self._lock:
            if now - self._checked < _RELOAD_DEBOUNCE_S:
                return
            self._checked = now
            stale = self._fingerprint() != self._stamps
        if stale:
            self._load()

    # -- query -------------------------------------------------------------- #
    @property
    def enabled(self) -> bool:
        """True when this store should be enforced at all.

        An explicitly configured store stays enabled even when broken -- that is
        what makes the failure closed rather than open.
        """
        return bool(self.paths) and (self._explicit or bool(self._tokens))

    @property
    def healthy(self) -> bool:
        return self.enabled and bool(self._tokens)

    def verify(self, token: str) -> Identity | None:
        if not token:
            return None
        self._maybe_reload()
        token_digest = _digest(token)
        with self._lock:
            identity = self._tokens.get(token_digest)
            last_attempt = self._last_used_attempt.get(token_digest, 0.0)
            now = time.time()
            should_mark = bool(identity) and now - last_attempt >= _LAST_USED_RESOLUTION_S
            if should_mark:
                self._last_used_attempt[token_digest] = now
        if should_mark:
            self._mark_last_used(token, now)
        return identity

    def identities(self) -> list[str]:
        """Return distinct display labels for issued keys, never key material."""
        self._maybe_reload()
        with self._lock:
            return sorted({identity.label for identity in self._tokens.values()})

    def _mark_last_used(self, token: str, now: float) -> None:
        """Best-effort activity stamp; authentication never depends on the write."""
        db_paths: list[str] = []
        csv_paths: list[str] = []
        for path in self.paths:
            if path.endswith(_SQLITE_SUFFIXES):
                db_paths.append(path)
            else:
                csv_paths.append(path)
        seen: set[str] = set()
        errors: list[str] = []
        for path in db_paths:
            if path in seen or not os.path.exists(path):
                continue
            seen.add(path)
            try:
                if _mark_used_sqlite(path, token, now):
                    with self._lock:
                        self.last_used_error = None
                    return
            except Exception as exc:  # noqa: BLE001 - valid auth must not fail over telemetry
                errors.append(f"{path}: {type(exc).__name__}: {exc}")
        for path in csv_paths:
            try:
                if _mark_used_csv(path, token, now):
                    with self._lock:
                        self.last_used_error = None
                    return
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{path}: {type(exc).__name__}: {exc}")
        with self._lock:
            self.last_used_error = "; ".join(errors) or "token source is not writable"

    def status(self) -> dict:
        with self._lock:
            n, err, at = len(self._tokens), self.error, self.loaded_at
            last_used_error = self.last_used_error
        return {
            "enabled": self.enabled,
            "healthy": self.healthy,
            "n_tokens": n,
            "sources": list(self.paths),
            "error": err,
            "loaded_at": at,
            "last_used_error": last_used_error,
            "signup_url": SIGNUP_URL,
        }


_default: TokenStore | None = None


def default_store() -> TokenStore:
    """Process-wide store built from the environment (created on first use)."""
    global _default
    if _default is None:
        _default = TokenStore()
    return _default
