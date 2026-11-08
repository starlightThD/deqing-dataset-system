"""Publish all consolidated POIs as one logical PostGIS resource.

The old source-package metadata remains for provenance, but is hidden from the
public catalog. Existing POI scene layers are repointed to the unified ID.
"""

import argparse
import sqlite3
from pathlib import Path

from dataset_service.poi_identity import UNIFIED_POI_ID
from dataset_service.spatial import (
    SpatialStore, _ensure_unified_poi_dataset, _refresh_unified_poi_dataset,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--postgres-backup", type=Path, required=True)
    parser.add_argument("--scene-backup", type=Path, required=True)
    args = parser.parse_args()
    for backup in (args.postgres_backup, args.scene_backup):
        if not backup.is_file() or backup.stat().st_size < 1024:
            parser.error(f"missing nonempty backup: {backup}")

    store = SpatialStore(args.data_dir)
    store.ensure_schema()
    with store.connect() as db:
        db.execute("SELECT pg_advisory_xact_lock(7110, 2)")
        total = db.execute("SELECT count(*) AS n FROM poi_feature").fetchone()["n"]
        if total == 0:
            parser.error("poi_feature is empty")
        _ensure_unified_poi_dataset(db)
        moved = db.execute("UPDATE poi_feature SET dataset_id=%s WHERE dataset_id<>%s",
                           (UNIFIED_POI_ID, UNIFIED_POI_ID)).rowcount
        _refresh_unified_poi_dataset(db)
        row = db.execute("SELECT feature_count FROM spatial_dataset WHERE id=%s",
                         (UNIFIED_POI_ID,)).fetchone()
        if row["feature_count"] != total:
            raise RuntimeError("unified POI count mismatch; transaction will roll back")
        print(f"unified {total} POIs under {UNIFIED_POI_ID}; reassigned {moved} rows")

    scenes_path = args.data_dir / "legacy_scenes.sqlite3"
    with sqlite3.connect(scenes_path, timeout=30) as db:
        scene_layers = db.execute("""UPDATE scene_layer SET source_id=?
            WHERE source_kind='spatial_dataset' AND layer_category='poi' AND source_id<>?""",
            (str(UNIFIED_POI_ID), str(UNIFIED_POI_ID))).rowcount
        old_layers = db.execute("SELECT id,scene_id,bbox_json FROM scene_poi_layer").fetchall()
        for layer_id, scene_id, bbox_json in old_layers:
            conflict = db.execute("""SELECT id FROM scene_poi_layer
                WHERE scene_id=? AND dataset_id=? AND bbox_json=? AND id<>?""",
                (scene_id, str(UNIFIED_POI_ID), bbox_json, layer_id)).fetchone()
            if conflict:
                raise RuntimeError("duplicate legacy POI layer scope; scene update rolled back")
        legacy_layers = db.execute("UPDATE scene_poi_layer SET dataset_id=? WHERE dataset_id<>?",
                                   (str(UNIFIED_POI_ID), str(UNIFIED_POI_ID))).rowcount
    print(f"repointed {scene_layers} scene layers and {legacy_layers} legacy POI layers")


if __name__ == "__main__":
    main()
