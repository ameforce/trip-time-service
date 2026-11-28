from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from hmac import compare_digest
from pathlib import Path
from threading import BoundedSemaphore, Lock

FAVORITE_LIMIT = 20
SESSION_MAX_AGE = 30 * 24 * 60 * 60
_SESSION_LIMIT = 20
_SESSION_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash BLOB NOT NULL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_account ON sessions(account_id);
CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
CREATE TABLE IF NOT EXISTS favorites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    address TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    UNIQUE(account_id, name)
);
CREATE INDEX IF NOT EXISTS favorites_account_order ON favorites(account_id, id);
COMMIT;
"""


@dataclass(frozen=True, slots=True)
class User:
    id: str
    username: str


@dataclass(frozen=True, slots=True)
class Place:
    name: str
    address: str
    lat: float
    lon: float


@dataclass(frozen=True, slots=True)
class Favorite(Place):
    id: int


@dataclass(frozen=True, slots=True)
class Snapshot:
    user: User | None
    favorites: list[Favorite]


@dataclass(frozen=True, slots=True)
class ImportSummary:
    imported: int
    skipped: int


@dataclass(frozen=True, slots=True)
class _Account:
    id: int
    username: str
    password_hash: str = field(repr=False)


class AccountError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _sign_in_required() -> AccountError:
    return AccountError(401, "로그인해 주세요.")


def _account_changed() -> AccountError:
    return AccountError(409, "현재 계정을 다시 확인해 주세요.")


def _credentials_mismatch() -> AccountError:
    return AccountError(401, "아이디 또는 비밀번호를 확인해 주세요.")


def default_account_db_path() -> Path:
    if os.name == "nt":
        app_data = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        app_data = Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return app_data / "trip-time-service" / "accounts.sqlite3"


def _password_hash(password: str, salt: bytes) -> str:
    # Each hash uses about 32 MiB. _AuthBudget limits simultaneous executions.
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**15,
        r=8,
        p=3,
        maxmem=64 * 1024 * 1024,
    )
    return f"scrypt-1${salt.hex()}${digest.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    _, salt_hex, _ = encoded.split("$")
    return compare_digest(_password_hash(password, bytes.fromhex(salt_hex)), encoded)


def _token_hash(token: str | None) -> bytes | None:
    if token is None or _SESSION_TOKEN.fullmatch(token) is None:
        return None
    return hashlib.sha256(token.encode("ascii")).digest()


class _AuthBudget:
    _WINDOW = 60
    _OPERATION_LIMITS = {"register": 12, "login": 120, "delete": 30}
    _ACCOUNT_LIMIT = 10

    def __init__(self) -> None:
        self._lock = Lock()
        self._hash_slots = BoundedSemaphore(2)
        self._window_start = time.monotonic()
        self._operation_counts: dict[str, int] = {}
        self._account_counts: dict[tuple[str, str], int] = {}

    @contextmanager
    def attempt(self, operation: str, username: str) -> Iterator[None]:
        limit_error = AccountError(429, "잠시 후 다시 시도해 주세요.")
        if not self._hash_slots.acquire(blocking=False):
            raise limit_error
        try:
            with self._lock:
                now = time.monotonic()
                if now - self._window_start >= self._WINDOW:
                    self._window_start = now
                    self._operation_counts.clear()
                    self._account_counts.clear()
                count = self._operation_counts.get(operation, 0)
                key = (operation, username)
                account_count = self._account_counts.get(key, 0)
                if (
                    count >= self._OPERATION_LIMITS[operation]
                    or account_count >= self._ACCOUNT_LIMIT
                ):
                    raise limit_error
                self._operation_counts[operation] = count + 1
                self._account_counts[key] = account_count + 1
            yield
        finally:
            self._hash_slots.release()


class AccountStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._initialization_lock = Lock()
        self._initialized = False
        self._budget = _AuthBudget()
        self._dummy_password_hash = f"scrypt-1${secrets.token_hex(16)}${'0' * 128}"

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # Use short-lived connections and SQLite's file transaction locks, which also
        # protect overlapping service processes when a deployment swaps containers.
        with self._initialization_lock:
            if not self._initialized:
                self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                self._path.touch(exist_ok=True, mode=0o600)
                with closing(sqlite3.connect(self._path)) as db:
                    db.executescript(_SCHEMA)
                self._initialized = True
        with closing(sqlite3.connect(self._path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            yield db

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with self._connect() as db, db:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db

    @staticmethod
    def _account_for_session(
        db: sqlite3.Connection,
        token_hash: bytes | None,
    ) -> _Account | None:
        if token_hash is None:
            return None
        row = db.execute(
            """
            SELECT accounts.id, accounts.username, accounts.password_hash
            FROM sessions JOIN accounts ON accounts.id = sessions.account_id
            WHERE sessions.token_hash = ? AND sessions.expires_at > ?
            """,
            (token_hash, int(time.time())),
        ).fetchone()
        return _Account(**row) if row else None

    @staticmethod
    def _check_account_id(account: _Account, expected_id: str | None) -> None:
        if expected_id != str(account.id):
            raise _account_changed()

    @classmethod
    def _require_account(
        cls,
        db: sqlite3.Connection,
        token_hash: bytes | None,
        expected_id: str | None,
    ) -> _Account:
        account = cls._account_for_session(db, token_hash)
        if account is None:
            raise _sign_in_required()
        cls._check_account_id(account, expected_id)
        return account

    @staticmethod
    def _start_session(
        db: sqlite3.Connection,
        account_id: int,
        previous_token: str | None,
    ) -> str:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        db.execute(
            "DELETE FROM sessions WHERE token_hash = ?", (_token_hash(previous_token),)
        )
        db.execute(
            "INSERT INTO sessions(token_hash, account_id, expires_at) VALUES (?, ?, ?)",
            (_token_hash(token), account_id, now + SESSION_MAX_AGE),
        )
        db.execute(
            """
            DELETE FROM sessions WHERE rowid IN (
                SELECT rowid FROM sessions WHERE account_id = ?
                ORDER BY rowid DESC LIMIT -1 OFFSET ?
            )
            """,
            (account_id, _SESSION_LIMIT),
        )
        return token

    @staticmethod
    def _snapshot(db: sqlite3.Connection, account: _Account) -> Snapshot:
        rows = db.execute(
            "SELECT id, name, address, lat, lon FROM favorites "
            "WHERE account_id = ? ORDER BY id",
            (account.id,),
        )
        return Snapshot(
            User(str(account.id), account.username),
            [Favorite(**row) for row in rows],
        )

    def snapshot(self, token: str | None, *, require_account: bool = False) -> Snapshot:
        token_hash = _token_hash(token)
        if token_hash is not None:
            with self._transaction() as db:
                account = self._account_for_session(db, token_hash)
                if account is not None:
                    return self._snapshot(db, account)
        if require_account:
            raise _sign_in_required()
        return Snapshot(None, [])

    def register(
        self,
        username: str,
        password: str,
        previous_token: str | None,
    ) -> tuple[str, Snapshot]:
        with self._budget.attempt("register", username):
            encoded = _password_hash(password, secrets.token_bytes(16))
        with self._transaction(write=True) as db:
            row = db.execute(
                "INSERT INTO accounts(username, password_hash) VALUES (?, ?) "
                "ON CONFLICT(username) DO NOTHING RETURNING id",
                (username, encoded),
            ).fetchone()
            if row is None:
                raise AccountError(409, "이 아이디를 사용할 수 없습니다.")
            token = self._start_session(db, row["id"], previous_token)
        return token, Snapshot(User(str(row["id"]), username), [])

    def login(
        self,
        username: str,
        password: str,
        previous_token: str | None,
    ) -> tuple[str, Snapshot]:
        with self._budget.attempt("login", username):
            with self._connect() as db:
                row = db.execute(
                    "SELECT id, username, password_hash FROM accounts "
                    "WHERE username = ?",
                    (username,),
                ).fetchone()
            encoded = row["password_hash"] if row else self._dummy_password_hash
            password_matches = _verify_password(password, encoded)
            if row is None or not password_matches:
                raise _credentials_mismatch()

        account = _Account(**row)
        with self._transaction(write=True) as db:
            # Verification runs outside the DB write lock. The account may have
            # been deleted while hashing, so confirm its identity before login.
            row = db.execute(
                "SELECT 1 FROM accounts WHERE id = ? AND password_hash = ?",
                (account.id, account.password_hash),
            ).fetchone()
            if row is None:
                raise _credentials_mismatch()
            token = self._start_session(db, account.id, previous_token)
            return token, self._snapshot(db, account)

    def logout(self, token: str | None, expected_id: str | None) -> None:
        token_hash = _token_hash(token)
        if token_hash is None:
            return
        with self._transaction(write=True) as db:
            account = self._account_for_session(db, token_hash)
            if account is not None:
                self._check_account_id(account, expected_id)
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def delete_account(
        self,
        token: str | None,
        expected_id: str | None,
        password: str,
    ) -> None:
        token_hash = _token_hash(token)
        if token_hash is None:
            raise _sign_in_required()
        with self._connect() as db:
            account = self._require_account(db, token_hash, expected_id)
        with self._budget.attempt("delete", account.username):
            if not _verify_password(password, account.password_hash):
                raise _credentials_mismatch()
        with self._transaction(write=True) as db:
            current = self._require_account(db, token_hash, expected_id)
            if current != account:
                raise _account_changed()
            db.execute("DELETE FROM accounts WHERE id = ?", (current.id,))

    def add_favorites(
        self,
        token: str | None,
        expected_id: str | None,
        places: Sequence[Place],
        *,
        importing: bool = False,
    ) -> tuple[Snapshot, ImportSummary]:
        token_hash = _token_hash(token)
        if token_hash is None:
            raise _sign_in_required()
        with self._transaction(write=True) as db:
            account = self._require_account(db, token_hash, expected_id)
            names = {
                row["name"]
                for row in db.execute(
                    "SELECT name FROM favorites WHERE account_id = ?",
                    (account.id,),
                )
            }
            imported = 0
            for place in places:
                if place.name in names:
                    if importing:
                        continue
                    raise AccountError(409, "같은 이름의 즐겨찾기가 있습니다.")
                if len(names) >= FAVORITE_LIMIT:
                    if importing:
                        break
                    raise AccountError(409, "즐겨찾기는 20개까지 저장할 수 있습니다.")
                db.execute(
                    "INSERT INTO favorites(account_id, name, address, lat, lon) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (account.id, place.name, place.address, place.lat, place.lon),
                )
                names.add(place.name)
                imported += 1
            return self._snapshot(db, account), ImportSummary(
                imported, len(places) - imported
            )

    def delete_favorite(
        self,
        token: str | None,
        expected_id: str | None,
        favorite_id: int,
    ) -> Snapshot:
        token_hash = _token_hash(token)
        if token_hash is None:
            raise _sign_in_required()
        with self._transaction(write=True) as db:
            account = self._require_account(db, token_hash, expected_id)
            result = db.execute(
                "DELETE FROM favorites WHERE account_id = ? AND id = ?",
                (account.id, favorite_id),
            )
            if result.rowcount == 0:
                raise AccountError(404, "즐겨찾기를 찾을 수 없습니다.")
            return self._snapshot(db, account)
