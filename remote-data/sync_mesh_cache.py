#!/usr/bin/env python3
"""Copy one resource from a Wi-Fi HTTP cache into the 7110 mirror.

The transfer goes over the local wired SSH connection. Imported cache objects
are hard-linked inside the container so staging and the live cache share bytes.
"""

import argparse
import sqlite3
import subprocess
import tempfile
import uuid
from pathlib import Path


SCHEMA = """CREATE TABLE http_cache (
    cache_key TEXT PRIMARY KEY, method TEXT NOT NULL, target TEXT NOT NULL,
    request_json TEXT, status INTEGER NOT NULL, content_type TEXT NOT NULL,
    object_name TEXT NOT NULL, size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL, fetched_at TEXT NOT NULL
)"""

IMPORTER = """
import hashlib, os, sqlite3, sys
from pathlib import Path

stage = Path(sys.argv[1])
live = Path('/workspace/data/legacy-mirror')
source = sqlite3.connect(stage / 'manifest.sqlite3')
target = sqlite3.connect(live / 'mirror.sqlite3', timeout=60)
rows = source.execute('SELECT * FROM http_cache').fetchall()
for row in rows:
    name = row[6]
    origin = stage / 'objects' / name
    if origin.stat().st_size != row[7]:
        raise RuntimeError('wrong transfer size: ' + name)
    destination = live / 'objects' / name
    if not destination.exists():
        digest = hashlib.sha256()
        with origin.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        if digest.hexdigest() != row[8]:
            raise RuntimeError('wrong transfer checksum: ' + name)
        os.link(origin, destination)
    target.execute('INSERT OR REPLACE INTO http_cache VALUES (?,?,?,?,?,?,?,?,?,?)', row)
target.commit()
print('imported', len(rows), 'bytes', sum(row[7] for row in rows))
"""


def run(command, **kwargs):
    subprocess.run(command, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--resource-id", required=True)
    parser.add_argument("--server", default="192.168.150.41")
    parser.add_argument("--target-prefix", default="/api/Mesh/GetMesh3DTilesFile")
    parser.add_argument("--batch-kind", default="mesh-batches")
    parser.add_argument("--prune-synced", action="store_true",
                        help="remove local transfer copies after server checksum verification")
    args = parser.parse_args()
    resource_id = str(uuid.UUID(args.resource_id))
    source = sqlite3.connect(args.cache / "mirror.sqlite3")
    source.execute("""CREATE TABLE IF NOT EXISTS server_synced (
        cache_key TEXT PRIMARY KEY, sha256 TEXT NOT NULL
    )""")
    rows = source.execute(
        """SELECT h.* FROM http_cache h LEFT JOIN server_synced s
             ON s.cache_key=h.cache_key AND s.sha256=h.sha256
             WHERE h.target LIKE ? AND s.cache_key IS NULL""",
        (f"{args.target_prefix}/{resource_id}/%",),
    ).fetchall()
    if not rows:
        print("already synchronized", resource_id)
        return
    stage = f"/workspace/data/remote-refresh-20261102/{args.batch_kind}/{resource_id}"
    run(["ssh", "-o", "BatchMode=yes", args.server,
         f"docker exec deqing-data mkdir -p {stage}/objects"])

    with tempfile.TemporaryDirectory(prefix="mesh-sync-") as scratch:
        temporary = Path(scratch)
        manifest = sqlite3.connect(temporary / "manifest.sqlite3")
        manifest.execute(SCHEMA)
        manifest.executemany("INSERT INTO http_cache VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        manifest.commit()
        manifest.close()
        filelist = temporary / "filelist"
        filelist.write_bytes(b"\0".join(row[6].encode("ascii") for row in rows) + b"\0")
        with filelist.open("rb") as names:
            tar = subprocess.Popen(
                ["tar", "-C", str(args.cache / "objects"), "--null", "-T", "-", "-cf", "-"],
                stdin=names, stdout=subprocess.PIPE,
            )
            try:
                run(["ssh", "-o", "BatchMode=yes", args.server,
                     f"docker exec -i deqing-data tar -xf - -C {stage}/objects"], stdin=tar.stdout)
            finally:
                tar.stdout.close()
                if tar.wait() != 0:
                    raise RuntimeError("local tar failed")
        with (temporary / "manifest.sqlite3").open("rb") as stream:
            run(["ssh", "-o", "BatchMode=yes", args.server,
                 f"docker exec -i deqing-data tee {stage}/manifest.sqlite3"],
                stdin=stream, stdout=subprocess.DEVNULL)
    run(["ssh", "-o", "BatchMode=yes", args.server,
         f"docker exec -i deqing-data python3 - {stage}"], input=IMPORTER.encode())
    source.executemany("INSERT OR REPLACE INTO server_synced VALUES (?,?)",
                       [(row[0], row[8]) for row in rows])
    source.commit()
    if args.prune_synced:
        for row in rows:
            (args.cache / "objects" / row[6]).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
