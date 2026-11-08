#!/usr/bin/env python3
"""Keep mirroring discoverable terrain tiles, syncing each batch into 7110.

Run this on the dual-network workstation. It fetches over Wi-Fi and transfers
each finished batch to the server over the wired LAN. All state is in SQLite,
so restarting the process resumes from the last committed batch.
"""

import argparse
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


RESOURCE_IDS = (
    "4fe06da6-992b-4635-a9ef-976bcf1565f4",
    "57bca7c3-f7ec-4e4d-ae6d-bde4c8589bd8",
    "3b021b6d-e013-4ecd-a880-0d3c94051e34",
    "74d28e95-ce11-43b7-9e8d-60c3faf99411",
    "b0537ed6-a169-40dd-b79f-44e67d0e3496",
    "3eec8b17-2105-4239-808a-9d4a9d93372e",
    "3f010341-820f-4839-a09f-b065723c78a6",
    "48564a7a-e7ea-404a-b0ce-bb97d0779a7f",
    "2f8d7407-29ab-4ef7-9c44-e361eb780bdf",
    "c9a61d89-d845-4b92-8aca-3d53318511b2",
    "cf6517c7-c659-4716-bbce-23a8f4c81f62",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=4000)
    parser.add_argument("--workers", type=int, default=20)
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    stage = here / "out/transfer-20261102"
    cache = stage / "mesh-cache"

    def requeue_transport_errors(resource_id):
        # Errors without an HTTP response are safe to retry. Keep genuine HTTP
        # misses (for example 204/404) recorded as errors.
        with sqlite3.connect(cache / "mirror.sqlite3") as db:
            retried = db.execute(
                "UPDATE queue SET state='pending', detail=NULL "
                "WHERE resource_id=? AND state='error' AND detail LIKE 'curl=% http=0'",
                (resource_id,),
            ).rowcount
            db.commit()
        return retried

    def remote_available(resource_id):
        target = f"http://1jk412ww3207.vicp.fun:16761/api/Mesh/GetMesh3DTilesFile/{resource_id}/tileset.json"
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
                "SELECT state, COUNT(*) FROM queue WHERE resource_id=? GROUP BY state",
                (resource_id,),
            ).fetchall())

    for resource_id in RESOURCE_IDS:
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
            subprocess.run([
                sys.executable, str(here / "pull_mesh_tiles.py"),
                "--resources", str(stage / "resources/resources_all.json"),
                "--indexes", str(here / "out/indexes"),
                "--out", str(cache), "--resource-id", resource_id,
                "--max-files", str(min(args.batch_size, pending)),
                "--max-bytes", "150000000000",
                "--workers", str(args.workers),
            ], check=True)
            subprocess.run([
                sys.executable, str(here / "sync_mesh_cache.py"),
                "--cache", str(cache), "--resource-id", resource_id,
                "--prune-synced",
            ], check=True)
            after = counts(resource_id)
            retried = requeue_transport_errors(resource_id)
            if retried:
                print("requeued transport failures", resource_id, retried, flush=True)
                time.sleep(60)
                after = counts(resource_id)
            print("synchronized", resource_id, after, flush=True)


if __name__ == "__main__":
    main()
