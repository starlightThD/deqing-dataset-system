#!/usr/bin/env python3
"""Resumable mirror of relative files exposed by the legacy 3D Tiles route.

Run once with --json-only to discover nested tileset files, then without it to
download referenced binary tiles. Data is kept in a small SQLite manifest plus
content files named by the exact legacy-cache request key.
"""

import argparse
import concurrent.futures
import datetime
import hashlib
import json
import os
import posixpath
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote, urlsplit


def references(raw: bytes, parent: str):
    try:
        document = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        return set()
    found = set()

    def visit(value):
        if isinstance(value, dict):
            uri = value.get("uri")
            if isinstance(uri, str) and uri and not urlsplit(uri).scheme and not uri.startswith("/"):
                path = posixpath.normpath(posixpath.join(posixpath.dirname(parent), uri.split("?", 1)[0]))
                if path not in (".", "..") and not path.startswith("../"):
                    found.add(path)
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(document)
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resources", type=Path, required=True)
    parser.add_argument("--indexes", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--resource-id", action="append")
    parser.add_argument("--resource-type", type=int, default=1000012)
    parser.add_argument("--target-prefix", default="/api/Mesh/GetMesh3DTilesFile")
    parser.add_argument("--root-route", default="tileset.json")
    parser.add_argument("--json-only", action="store_true")
    parser.add_argument("--max-files", type=int, default=10000)
    parser.add_argument("--max-bytes", type=int, default=20_000_000_000)
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--retry-errors", action="store_true")
    parser.add_argument("--base", default="http://1jk412ww3207.vicp.fun:16761")
    parser.add_argument("--interface", default="wlp0s20f3")
    parser.add_argument("--address", default="175.27.226.195")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    objects = args.out / "objects"
    objects.mkdir(exist_ok=True)
    temporary = args.out / "temporary"
    temporary.mkdir(exist_ok=True)
    db = sqlite3.connect(args.out / "mirror.sqlite3")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS queue (
            resource_id TEXT NOT NULL, relative_path TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', detail TEXT,
            PRIMARY KEY(resource_id, relative_path)
        );
        CREATE TABLE IF NOT EXISTS http_cache (
            cache_key TEXT PRIMARY KEY, method TEXT NOT NULL, target TEXT NOT NULL,
            request_json TEXT, status INTEGER NOT NULL, content_type TEXT NOT NULL,
            object_name TEXT NOT NULL, size_bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL, fetched_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS queue_by_resource_state
            ON queue(resource_id, state, relative_path);
    """)
    resources = json.loads(args.resources.read_text(encoding="utf-8"))["Rows"]
    selected = set(args.resource_id or [])
    for resource in resources:
        if resource.get("Type") != args.resource_type or selected and resource["Id"] not in selected:
            continue
        resource_id = resource["Id"]
        root = args.indexes / resource_id / "root.json"
        if not root.is_file():
            continue
        raw = root.read_bytes()
        target = f"{args.target_prefix}/{resource_id}/{args.root_route}"
        key = hashlib.sha256(b"GET\0" + target.encode() + b"\0").hexdigest()
        (objects / key).write_bytes(raw)
        db.execute("INSERT OR REPLACE INTO http_cache VALUES (?,?,?,?,?,?,?,?,?,?)", (
            key, "GET", target, None, 200, "application/octet-stream", key, len(raw),
            hashlib.sha256(raw).hexdigest(), datetime.datetime.now(datetime.timezone.utc).isoformat(),
        ))
        db.execute("INSERT OR IGNORE INTO queue VALUES (?,?,?,NULL)", (resource_id, args.root_route, "done"))
        for path in references(raw, args.root_route):
            db.execute("INSERT OR IGNORE INTO queue VALUES (?,?,?,NULL)", (resource_id, path, "pending"))
    db.commit()
    if args.retry_errors:
        if selected:
            db.executemany(
                "UPDATE queue SET state='pending',detail=NULL WHERE resource_id=? AND state='error'",
                [(resource_id,) for resource_id in selected],
            )
        else:
            db.execute("UPDATE queue SET state='pending',detail=NULL WHERE state='error'")
        db.commit()

    parsed_base = urlsplit(args.base)
    resolved = f"{parsed_base.hostname}:{parsed_base.port or 80}:{args.address}"

    def fetch(item):
        resource_id, relpath = item
        target = f"{args.target_prefix}/{resource_id}/{quote(relpath, safe='/+._-')}"
        handle, filename = tempfile.mkstemp(prefix="mesh-", dir=temporary)
        os.close(handle)
        command = [
            "curl", "--interface", args.interface, "--resolve", resolved,
            "--connect-timeout", "15", "--max-time", "180", "--retry", "2",
            "--retry-all-errors", "-sS", "-H", "Accept-Encoding: identity",
            "-o", filename, "-w", "%{http_code}|%{content_type}",
            args.base.rstrip("/") + target,
        ]
        result = subprocess.run(command, capture_output=True)
        try:
            status, content_type = result.stdout.decode().split("|", 1)
            return resource_id, relpath, target, int(status), content_type, Path(filename), result.returncode
        except (ValueError, UnicodeError):
            return resource_id, relpath, target, 0, "", Path(filename), result.returncode

    processed = 0
    total = db.execute("SELECT COALESCE(SUM(size_bytes),0) FROM http_cache").fetchone()[0]
    while processed < args.max_files:
        remaining = args.max_files - processed
        where = "state='pending'"
        parameters = []
        if args.json_only:
            where += " AND relative_path LIKE '%.json'"
        if selected:
            where += " AND resource_id IN (" + ",".join("?" for _ in selected) + ")"
            parameters.extend(sorted(selected))
        parameters.append(min(args.workers * 8, remaining))
        pending = db.execute(
            "SELECT resource_id,relative_path FROM queue WHERE " + where
            + " ORDER BY resource_id,relative_path LIMIT ?", parameters,
        ).fetchall()
        if not pending:
            break
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            replies = [pool.submit(fetch, item) for item in pending]
            for reply in concurrent.futures.as_completed(replies):
                resource_id, relpath, target, status, content_type, filename, exit_code = reply.result()
                # Count every attempt toward --max-files. Previously only a
                # successful response advanced this counter, so repeated
                # transport failures could make one invocation drain the
                # entire pending queue into error state.
                processed += 1
                size = filename.stat().st_size
                if exit_code or status != 200:
                    db.execute("UPDATE queue SET state='error',detail=? WHERE resource_id=? AND relative_path=?",
                               (f"curl={exit_code} http={status}", resource_id, relpath))
                    filename.unlink(missing_ok=True)
                    continue
                key = hashlib.sha256(b"GET\0" + target.encode() + b"\0").hexdigest()
                raw = filename.read_bytes() if relpath.lower().endswith(".json") else None
                hasher = hashlib.sha256()
                with filename.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        hasher.update(chunk)
                digest = hasher.hexdigest()
                os.replace(filename, objects / key)
                db.execute("INSERT OR REPLACE INTO http_cache VALUES (?,?,?,?,?,?,?,?,?,?)", (
                    key, "GET", target, None, status, content_type or "application/octet-stream",
                    key, size, digest, datetime.datetime.now(datetime.timezone.utc).isoformat(),
                ))
                total += size
                db.execute("UPDATE queue SET state=?,detail=NULL WHERE resource_id=? AND relative_path=?",
                           ("done" if size else "empty", resource_id, relpath))
                if raw:
                    for discovered in references(raw, relpath):
                        db.execute("INSERT OR IGNORE INTO queue VALUES (?,?,?,NULL)",
                                   (resource_id, discovered, "pending"))
        db.commit()
        if processed % 1000 < args.workers:
            print("processed", processed, "bytes", total, flush=True)
        if total >= args.max_bytes:
            break
    print("queue", db.execute("SELECT state,COUNT(*) FROM queue GROUP BY state").fetchall())
    print("cache", db.execute("SELECT COUNT(*),SUM(size_bytes) FROM http_cache").fetchone())


if __name__ == "__main__":
    main()
