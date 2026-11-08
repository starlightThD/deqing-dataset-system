#!/usr/bin/env python3
"""Resume TMS tile discovery and publish each resource into the 7110 mirror."""

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    stage = here / "out/transfer-20261102"
    source = stage / "frontend-api-cache"
    cache = stage / "tms-cache"
    with sqlite3.connect(source / "mirror.sqlite3") as db:
        resource_ids = sorted({target.split("/")[4] for (target,) in db.execute(
            "SELECT target FROM http_cache WHERE target LIKE '/api/Raster/GetTmsFile/%/tilemapresource.xml'"
        )})
    resources = {row["Id"]: row for row in json.loads(
        (stage / "resources/resources_all.json").read_text(encoding="utf-8"))["Rows"]}
    for resource_id in resource_ids:
        print("fetching", resource_id, flush=True)
        command = [
            sys.executable, str(here / "pull_tms_tiles.py"),
            "--source", str(source),
            "--resources", str(stage / "resources/resources_all.json"),
            "--out", str(cache), "--resource-id", resource_id,
            "--workers", str(args.workers),
        ]
        subprocess.run(command, check=True)
        with sqlite3.connect(cache / "mirror.sqlite3") as db:
            count, size = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(size_bytes),0) FROM http_cache WHERE target LIKE ?",
                (f"/api/Raster/GetTmsFile/{resource_id}/%",),
            ).fetchone()
        expected = int(resources[resource_id].get("FileSize") or 0)
        if expected and size < expected:
            print("retrying empty tiles", resource_id, "missing bytes", expected - size, flush=True)
            subprocess.run(command + ["--retry-empty"], check=True)
        subprocess.run([
            sys.executable, str(here / "sync_mesh_cache.py"),
            "--cache", str(cache), "--resource-id", resource_id,
            "--target-prefix", "/api/Raster/GetTmsFile",
            "--batch-kind", "tms-batches",
        ], check=True)
        with sqlite3.connect(cache / "mirror.sqlite3") as db:
            actual = db.execute(
                "SELECT COALESCE(SUM(size_bytes),0) FROM http_cache WHERE target LIKE ?",
                (f"/api/Raster/GetTmsFile/{resource_id}/%",),
            ).fetchone()[0]
        print("synchronized", resource_id, "bytes", actual,
              "expected", expected, "match", actual == expected, flush=True)


if __name__ == "__main__":
    main()
