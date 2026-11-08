#!/usr/bin/env python3
"""Rebuild the 7110 PostGIS spatial file index from published on-volume bytes.

Run inside deqing-data with DEQING_POSTGIS_DSN_FILE and PYTHONPATH set.
Every run is idempotent.  A source becomes queryable only after full byte and
file-count reconciliation; failed sources remain explicitly unavailable.
"""

import argparse
import json
from pathlib import Path

from dataset_service.resource_index import ensure_schema, index_pointcloud_version, index_raster_epoch, index_native_assets
from dataset_service.spatial import SpatialStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path("/workspace/data"))
    parser.add_argument("--source-kind", choices=["all", "pointcloud_resource", "raster_collection",
                                                  "oblique_resource","video_group"], default="all")
    parser.add_argument("--source-id")
    parser.add_argument("--version-id")
    args = parser.parse_args()
    store = SpatialStore(args.data_dir)
    ensure_schema(store)
    with store.connect() as db:
        pointclouds = db.execute("""SELECT resource_id::text AS source_id,id::text AS version_id FROM pointcloud_version
            WHERE status='published' AND format='3dtiles' ORDER BY published_at,id""").fetchall()
        rasters = db.execute("""SELECT collection_id::text AS source_id,acquisition_date::text AS version_id
            FROM raster_epoch WHERE status='published' ORDER BY acquisition_date,collection_id""").fetchall()
        oblique = db.execute("SELECT id::text AS source_id FROM oblique_resource ORDER BY id").fetchall()
        videos = db.execute("SELECT id::text AS source_id FROM video_group WHERE status='published' ORDER BY id").fetchall()
    failed = 0
    if args.source_kind in {"all", "pointcloud_resource"}:
        for row in pointclouds:
            source_id,version=row["source_id"],row["version_id"]
            if args.source_id and args.source_id != source_id or args.version_id and args.version_id != version:
                continue
            try:
                print(json.dumps(index_pointcloud_version(store,source_id,version)),flush=True)
            except Exception as error:
                print(json.dumps({"source_kind":"pointcloud_resource","source_resource_id":source_id,
                    "source_version_id":version,"status":"failed","detail":str(error)}),flush=True)
                failed += 1
    for kind, records in (("oblique_resource",oblique),("video_group",videos)):
        if args.source_kind not in {"all",kind}:
            continue
        for row in records:
            source_id=row["source_id"]
            if args.source_id and args.source_id != source_id or args.version_id and args.version_id != source_id:
                continue
            try:
                print(json.dumps(index_native_assets(store,kind,source_id)),flush=True)
            except Exception as error:
                print(json.dumps({"source_kind":kind,"source_resource_id":source_id,
                    "source_version_id":source_id,"status":"failed","detail":str(error)}),flush=True)
                failed += 1
    if args.source_kind in {"all", "raster_collection"}:
        for row in rasters:
            source_id,version=row["source_id"],row["version_id"]
            if args.source_id and args.source_id != source_id or args.version_id and args.version_id != version:
                continue
            try:
                print(json.dumps(index_raster_epoch(store,source_id,version)),flush=True)
            except Exception as error:
                print(json.dumps({"source_kind":"raster_collection","source_resource_id":source_id,
                    "source_version_id":version,"status":"failed","detail":str(error)}),flush=True)
                failed += 1
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
