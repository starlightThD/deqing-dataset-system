#!/usr/bin/env python3
"""Resume point-cloud 3D Tiles mirroring through the PointCload route."""

import argparse
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


# Some resources have identical roots and child JSON but different GLB bytes.
# All ten resource IDs must be mirrored independently.
RESOURCE_IDS = (
    "f800becb-b378-49c4-a197-178c36b27a04",
    "1a5820e1-fc80-430d-9808-e3626befd630",
    "8f76b23a-5921-4a71-b212-5c2a8cfa27e1",
    "e2e70b8d-213e-4de2-af9b-07ec6d188bf3",
    "9f0eddcc-646f-4389-9c4d-559b88a24fdd",
    "843d5f4b-78f0-408c-a47b-d75f54c46148",
    "3bb33107-8019-4a4b-a445-629efedd64f3",
    "a1db7038-42f1-4c21-8c45-85a2730929f8",
    "0d797e9e-a21c-4268-bb2d-4a2da6a5c878",
    "711d320a-b285-45e2-89b4-51992aa5365d",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=10)
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    stage = here / "out/transfer-20261102"
    cache = stage / "pointcloud-cache"
    prefix = "/api/PointCload/GetPointCloud3DTilesFile"

    def requeue_transport_errors(resource_id):
        # Retry transport failures, but preserve genuine HTTP misses.
        with sqlite3.connect(cache / "mirror.sqlite3") as db:
            retried = db.execute(
                "UPDATE queue SET state='pending', detail=NULL "
                "WHERE resource_id=? AND state='error' AND detail LIKE 'curl=% http=0'",
                (resource_id,),
            ).rowcount
            db.commit()
        return retried

    def remote_available(resource_id):
        target = f"http://1jk412ww3207.vicp.fun:16761{prefix}/{resource_id}/root"
        result = subprocess.run([
            "curl", "--interface", "wlp0s20f3", "--resolve",
            "1jk412ww3207.vicp.fun:16761:175.27.226.195",
            "--connect-timeout", "8", "--max-time", "12", "-sS",
            "-o", "/dev/null", "-w", "%{http_code}", target,
        ], capture_output=True, text=True)
        return result.returncode == 0 and result.stdout == "200"

    def counts(resource_id):
        with sqlite3.connect(cache / "mirror.sqlite3") as db:
            return dict(db.execute(
                "SELECT state,COUNT(*) FROM queue WHERE resource_id=? GROUP BY state",
                (resource_id,),
            ).fetchall())

    def pull(resource_id, *extra):
        subprocess.run([
            sys.executable, str(here / "pull_mesh_tiles.py"),
            "--resources", str(stage / "resources/resources_all.json"),
            "--indexes", str(here / "out/indexes"),
            "--out", str(cache), "--resource-id", resource_id,
            "--resource-type", "13000006", "--target-prefix", prefix,
            "--root-route", "root", "--max-bytes", "150000000000",
            "--workers", str(args.workers), *extra,
        ], check=True)

    def sync(resource_id):
        subprocess.run([
            sys.executable, str(here / "sync_mesh_cache.py"),
            "--cache", str(cache), "--resource-id", resource_id,
            "--target-prefix", prefix, "--batch-kind", "pointcloud-batches",
            "--prune-synced",
        ], check=True)

    for resource_id in RESOURCE_IDS:
        print("discovering JSON", resource_id, flush=True)
        pull(resource_id, "--json-only", "--max-files", "200000")
        sync(resource_id)
        while True:
            before = counts(resource_id)
            pending = before.get("pending", 0)
            if pending == 0:
                retried = requeue_transport_errors(resource_id)
                if not retried:
                    print("finished", resource_id, before, flush=True)
                    break
                print("requeued transport failures", resource_id, retried, flush=True)
                pending = retried
            while not remote_available(resource_id):
                print("remote unavailable; retrying in 60 seconds", resource_id, flush=True)
                time.sleep(60)
            print("fetching", resource_id, before, flush=True)
            pull(resource_id, "--max-files", str(min(args.batch_size, pending)))
            sync(resource_id)
            after = counts(resource_id)
            retried = requeue_transport_errors(resource_id)
            if retried:
                print("requeued transport failures", resource_id, retried, flush=True)
                time.sleep(60)
                after = counts(resource_id)
            print("synchronized", resource_id, after, flush=True)


if __name__ == "__main__":
    main()
