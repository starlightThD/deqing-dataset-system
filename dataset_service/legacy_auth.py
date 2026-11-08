"""Credentials for the legacy login protocol. Kept separate from API bearer tokens."""
import argparse
import getpass
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from pathlib import Path


USER_ID = "00000000-0000-0000-0000-0000000000aa"
KDF_ITERATIONS = 260_000
USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_.@-]{3,64}\Z")


def _user_db_path(data_dir: Path) -> Path:
    return data_dir / "legacy_users.sqlite3"


def _password_digest(submitted_md5: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", bytes.fromhex(submitted_md5), salt, iterations)


def ensure_legacy_users(data_dir: Path, auth: dict) -> Path:
    """Create the local login store and import the existing compatibility account once."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = _user_db_path(data_dir)
    if not path.exists():
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
    if path.stat().st_mode & 0o077:
        raise RuntimeError("legacy_users.sqlite3 must be readable only by its owner")
    with sqlite3.connect(path) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS legacy_user (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                real_name TEXT NOT NULL,
                password_salt BLOB NOT NULL,
                password_hash BLOB NOT NULL,
                password_iterations INTEGER NOT NULL,
                auth_codes TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'reader' CHECK(role IN ('admin', 'reader')),
                is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        if db.execute("SELECT COUNT(*) FROM legacy_user").fetchone()[0] == 0:
            salt = secrets.token_bytes(16)
            digest = _password_digest(hashlib.md5(auth["password"].encode()).hexdigest(), salt, KDF_ITERATIONS)
            db.execute("""INSERT INTO legacy_user
                (id, username, real_name, password_salt, password_hash, password_iterations, auth_codes, role)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'admin')""",
                (USER_ID, auth["username"], auth["username"], salt, digest,
                 KDF_ITERATIONS, json.dumps(auth["auth_codes"], ensure_ascii=False)))
        columns = {row[1] for row in db.execute("PRAGMA table_info(legacy_user)")}
        if "role" not in columns:
            db.execute("ALTER TABLE legacy_user ADD COLUMN role TEXT NOT NULL DEFAULT 'reader'")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_legacy_user_username_nocase ON legacy_user(username COLLATE NOCASE)")
        db.execute("""
            CREATE TABLE IF NOT EXISTS legacy_session (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES legacy_user(id) ON DELETE CASCADE,
                expires_at INTEGER NOT NULL,
                revoked_at INTEGER,
                created_at INTEGER NOT NULL
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_legacy_session_user ON legacy_session(user_id, expires_at)")
        # The configured compatibility account is the only account that receives admin rights.
        db.execute("UPDATE legacy_user SET role='reader'")
        db.execute(
            "UPDATE legacy_user SET role='admin', auth_codes=? WHERE username=? COLLATE NOCASE",
            (json.dumps(auth["auth_codes"], ensure_ascii=False), auth["username"]),
        )
    return path


def authenticate_user(db_path: Path, username: str, submitted_md5: str) -> dict | None:
    if len(submitted_md5) != 32 or any(char not in "0123456789abcdefABCDEF" for char in submitted_md5):
        return None
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM legacy_user WHERE username=? COLLATE NOCASE AND is_active=1", (username,)).fetchone()
    if row is None:
        return None
    digest = _password_digest(submitted_md5.lower(), row["password_salt"], row["password_iterations"])
    if not secrets.compare_digest(digest, row["password_hash"]):
        return None
    return {"id": row["id"], "username": row["username"], "real_name": row["real_name"],
            "auth_codes": json.loads(row["auth_codes"]), "role": row["role"]}


def register_user(db_path: Path, username: str, real_name: str, submitted_md5: str) -> dict:
    """Create an ordinary user. The public registration path cannot choose a role."""
    username = username.strip()
    real_name = real_name.strip() or username
    if not USERNAME_PATTERN.fullmatch(username):
        raise ValueError("username must be 3-64 characters: letters, numbers, dot, underscore, @ or hyphen")
    if not 1 <= len(real_name) <= 100:
        raise ValueError("real name must be 1-100 characters")
    if len(submitted_md5) != 32 or any(char not in "0123456789abcdefABCDEF" for char in submitted_md5):
        raise ValueError("password must be a 32-character MD5 hex digest")
    user_id = str(uuid.uuid4())
    salt = secrets.token_bytes(16)
    digest = _password_digest(submitted_md5.lower(), salt, KDF_ITERATIONS)
    with sqlite3.connect(db_path) as db:
        try:
            db.execute("""INSERT INTO legacy_user
                (id, username, real_name, password_salt, password_hash, password_iterations, auth_codes, role)
                VALUES (?, ?, ?, ?, ?, ?, '[]', 'reader')""",
                (user_id, username, real_name, salt, digest, KDF_ITERATIONS))
        except sqlite3.IntegrityError as error:
            raise ValueError("username already exists") from error
    return {"id": user_id, "username": username, "real_name": real_name,
            "auth_codes": [], "role": "reader"}


def get_user_by_id(db_path: Path, user_id: str) -> dict | None:
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("""SELECT id, username, real_name, auth_codes, role
                           FROM legacy_user WHERE id=? AND is_active=1""", (user_id,)).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "username": row["username"], "real_name": row["real_name"],
            "auth_codes": json.loads(row["auth_codes"]), "role": row["role"]}


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def create_session(db_path: Path, user_id: str, token: str, expires_at: int) -> None:
    """Persist only a digest so each issued bearer token can be revoked independently."""
    now = int(time.time())
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("DELETE FROM legacy_session WHERE expires_at < ?", (now - 86400,))
        db.execute(
            "INSERT INTO legacy_session(token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (_token_hash(token), user_id, expires_at, now),
        )


def get_session_user(db_path: Path, user_id: str, token: str) -> dict | None:
    """Resolve an active JWT session to its active account; JWT signature is checked by caller."""
    try:
        digest = _token_hash(token)
    except UnicodeEncodeError:
        return None
    now = int(time.time())
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            """SELECT u.id, u.username, u.real_name, u.auth_codes, u.role
               FROM legacy_session s JOIN legacy_user u ON u.id=s.user_id
               WHERE s.token_hash=? AND s.user_id=? AND s.revoked_at IS NULL
                 AND s.expires_at>? AND u.is_active=1""",
            (digest, user_id, now),
        ).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "username": row["username"], "real_name": row["real_name"],
            "auth_codes": json.loads(row["auth_codes"]), "role": row["role"]}


def revoke_session(db_path: Path, user_id: str, token: str) -> None:
    """Revoke just this user's token; repeated logout calls are harmless."""
    try:
        digest = _token_hash(token)
    except UnicodeEncodeError:
        return
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE legacy_session SET revoked_at=COALESCE(revoked_at, ?) WHERE token_hash=? AND user_id=?",
            (int(time.time()), digest, user_id),
        )


def ensure_legacy_auth(data_dir: Path) -> dict:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "legacy_auth.json"
    if not path.exists():
        value = {
            "username": "admin",
            "password": secrets.token_urlsafe(32),
            "jwt_secret": secrets.token_hex(32),
            "auth_codes": ["BASEPLATFORM_DPS_ENGINE", "PLATFORM_MANAGE_ALL", "RESOURCE_SETTING"],
        }
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream)
            stream.write("\n")
    if path.stat().st_mode & 0o077:
        raise RuntimeError("legacy_auth.json must be readable only by its owner")
    value = json.loads(path.read_text())
    if not isinstance(value.get("username"), str) or not isinstance(value.get("password"), str) or not isinstance(value.get("jwt_secret"), str):
        raise RuntimeError("invalid legacy_auth.json")
    if len(value["jwt_secret"]) < 32 or not isinstance(value.get("auth_codes"), list):
        raise RuntimeError("invalid legacy_auth.json")
    return value


def set_password(data_dir: Path) -> None:
    value = ensure_legacy_auth(data_dir)
    db_path = ensure_legacy_users(data_dir, value)
    password = getpass.getpass("Legacy frontend password: ")
    if not password:
        raise ValueError("password cannot be empty")
    salt = secrets.token_bytes(16)
    digest = _password_digest(hashlib.md5(password.encode()).hexdigest(), salt, KDF_ITERATIONS)
    with sqlite3.connect(db_path) as db:
        changed = db.execute("""UPDATE legacy_user SET password_salt=?, password_hash=?,
            password_iterations=?, updated_at=CURRENT_TIMESTAMP WHERE username=?""",
            (salt, digest, KDF_ITERATIONS, value["username"]))
        if changed.rowcount != 1:
            raise RuntimeError("configured account is missing from legacy user database")
    value["password"] = password
    path = data_dir / "legacy_auth.json"
    temporary = data_dir / f".legacy_auth.{secrets.token_hex(8)}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream)
        stream.write("\n")
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure credentials accepted by the legacy login route")
    parser.add_argument("--data-dir", type=Path, default=Path(os.getenv("DEQING_DATA_DIR", "/workspace/data")))
    parser.add_argument("--set-password", action="store_true")
    args = parser.parse_args()
    if args.set_password:
        set_password(args.data_dir)
        print("Legacy password updated; restart the service to apply it.")
    else:
        value = ensure_legacy_auth(args.data_dir)
        db_path = ensure_legacy_users(args.data_dir, value)
        print(f"username: {value['username']}")
        print(f"user database: {db_path}")
        print(f"legacy login configuration/JWT secret file: {args.data_dir / 'legacy_auth.json'}")


if __name__ == "__main__":
    main()
