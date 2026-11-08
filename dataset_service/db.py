import sqlite3
import shutil
from pathlib import Path


class ManagedConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS dataset (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
  current_version TEXT
);
CREATE TABLE IF NOT EXISTS dataset_version (
  version TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES dataset(id),
  status TEXT NOT NULL CHECK(status IN ('validated','published')),
  source_alias TEXT NOT NULL, content_sha256 TEXT NOT NULL,
  file_count INTEGER NOT NULL, image_count INTEGER NOT NULL, total_bytes INTEGER NOT NULL,
  created_at TEXT NOT NULL, published_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_version_digest ON dataset_version(dataset_id, content_sha256);
CREATE TABLE IF NOT EXISTS dataset_file (
  version TEXT NOT NULL REFERENCES dataset_version(version), relative_path TEXT NOT NULL,
  size INTEGER NOT NULL, sha256 TEXT NOT NULL, kind TEXT NOT NULL,
  PRIMARY KEY(version, relative_path)
);
CREATE TABLE IF NOT EXISTS import_job (
  id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, source_alias TEXT NOT NULL,
  idempotency_key TEXT, status TEXT NOT NULL, version TEXT,
  copied_files INTEGER NOT NULL DEFAULT 0, error TEXT,
  created_at TEXT NOT NULL, finished_at TEXT,
  UNIQUE(dataset_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS audit_event (
  id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL,
  dataset_id TEXT, target TEXT, result TEXT NOT NULL, detail TEXT,
  created_at TEXT NOT NULL
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(db_path, timeout=30, check_same_thread=False, factory=ManagedConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    return db


def initialize(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > 1:
            raise RuntimeError(f"unsupported database schema version: {version}")
        db.executescript(SCHEMA)
        db.execute("PRAGMA user_version=1")
        db.execute("INSERT OR IGNORE INTO dataset(id,name) VALUES('garden','garden')")
        interrupted = [row[0] for row in db.execute("SELECT id FROM import_job WHERE status IN ('queued','copying','validating')")]
        db.execute("UPDATE import_job SET status='failed', error='service restarted during import', finished_at=datetime('now') WHERE status IN ('queued','copying','validating')")
        validated = [(row[0], row[1]) for row in db.execute("SELECT id,version FROM import_job WHERE status='validated'")]
    for import_id in interrupted:
        shutil.rmtree(db_path.parent / "staging" / import_id, ignore_errors=True)
    for import_id, version in validated:
        stage = db_path.parent / "staging" / import_id
        target = db_path.parent / "datasets" / "garden" / "versions" / version
        if target.is_dir() and not stage.exists():
            target.rename(stage)


def audit(db: sqlite3.Connection, actor: str, action: str, target: str | None, result: str, detail: str | None = None) -> None:
    db.execute(
        "INSERT INTO audit_event(actor,action,dataset_id,target,result,detail,created_at) VALUES(?,?,?,?,?,?,datetime('now'))",
        (actor, action, "garden", target, result, detail),
    )
