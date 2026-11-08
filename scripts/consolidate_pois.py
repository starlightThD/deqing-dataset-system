"""Move imported POI points and their overlapping exports into poi_feature.

Run after a PostgreSQL backup. The migration is atomic and refuses to overwrite
an existing consolidated catalog. Source dataset metadata and files are retained.
"""

import argparse
from pathlib import Path

from dataset_service.poi_catalog import CORE_FIELDS, SUPPLEMENT_FIELDS
from dataset_service.spatial import SpatialStore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--backup-file", type=Path, required=True)
    args = parser.parse_args()
    if not args.backup_file.is_file() or args.backup_file.stat().st_size < 1024:
        parser.error("a nonempty PostgreSQL backup file is required")
    store = SpatialStore(args.data_dir)
    store.ensure_schema()
    with store.connect() as db:
        db.execute("SELECT pg_advisory_xact_lock(7110, 1)")
        if db.execute("SELECT count(*) AS n FROM poi_feature").fetchone()["n"]:
            parser.error("poi_feature already contains data; migration was already run")
        datasets = db.execute("SELECT id, shape_type, fields FROM spatial_dataset").fetchall()
        core, supplemental = [], []
        for dataset in datasets:
            if dataset["shape_type"] not in (1, 11):
                continue
            names = {field.get("name") for field in dataset["fields"] if isinstance(field, dict)}
            if CORE_FIELDS <= names:
                core.append(dataset["id"])
            elif SUPPLEMENT_FIELDS <= names:
                supplemental.append(dataset["id"])
        if not core:
            parser.error("no canonical POI datasets found")
        counts = db.execute("""SELECT
            count(*) FILTER (WHERE dataset_id=ANY(%s::uuid[])) AS core_count,
            count(DISTINCT properties->>'id') FILTER (WHERE dataset_id=ANY(%s::uuid[])) AS unique_count,
            count(*) FILTER (WHERE dataset_id=ANY(%s::uuid[])) AS supplemental_count
            FROM spatial_feature""", (core, core, supplemental)).fetchone()
        if counts["core_count"] != counts["unique_count"]:
            parser.error("canonical POI IDs are missing or duplicated")
        bad = db.execute("""SELECT count(*) AS n FROM spatial_feature
            WHERE dataset_id=ANY(%s::uuid[]) AND
              (geom IS NULL OR ST_GeometryType(geom) <> 'ST_Point' OR ST_SRID(geom) <> 4326)""",
            (core,)).fetchone()["n"]
        if bad:
            parser.error(f"{bad} canonical records lack a WGS84 point")
        unmatched = db.execute("""SELECT count(*) AS n FROM spatial_feature s
            WHERE s.dataset_id=ANY(%s::uuid[]) AND NOT EXISTS
              (SELECT 1 FROM spatial_feature c WHERE c.dataset_id=ANY(%s::uuid[])
               AND c.properties->>'id'=s.properties->>'POI_ID')""",
            (supplemental, core)).fetchone()["n"]
        if unmatched:
            parser.error(f"{unmatched} supplemental records have no canonical POI")
        db.execute("""INSERT INTO poi_feature
            (poi_id,dataset_id,feature_id,source_fid,properties,geom,sources)
            SELECT f.properties->>'id',f.dataset_id,f.id,f.source_fid,f.properties,f.geom,
              jsonb_build_array(jsonb_build_object(
                'role','canonical','dataset_id',f.dataset_id::text,
                'dataset_name',d.name,'feature_id',f.id,'source_fid',f.source_fid))
            FROM spatial_feature f JOIN spatial_dataset d ON d.id=f.dataset_id
            WHERE f.dataset_id=ANY(%s::uuid[])""", (core,))
        db.execute("""WITH grouped AS (
            SELECT s.properties->>'POI_ID' AS poi_id,
              jsonb_agg(jsonb_build_object(
                'role','supplemental','dataset_id',s.dataset_id::text,
                'dataset_name',d.name,'feature_id',s.id,'source_fid',s.source_fid,
                'name',s.properties->>'NAME',
                'name_differs',(s.properties->>'NAME') IS DISTINCT FROM (p.properties->>'name'),
                'coordinate_offset_m',round(ST_Distance(s.geom::geography,p.geom::geography)::numeric,6),
                'properties',s.properties,'geometry',ST_AsGeoJSON(s.geom)::jsonb)
                ORDER BY d.feature_count DESC,s.id) AS records
            FROM spatial_feature s JOIN poi_feature p ON p.poi_id=s.properties->>'POI_ID'
            JOIN spatial_dataset d ON d.id=s.dataset_id
            WHERE s.dataset_id=ANY(%s::uuid[])
            GROUP BY s.properties->>'POI_ID'
        ) UPDATE poi_feature p SET sources=p.sources || grouped.records
          FROM grouped WHERE p.poi_id=grouped.poi_id""", (supplemental,))
        result = db.execute("""SELECT count(*) AS poi_count,
            sum(jsonb_array_length(sources)-1) AS supplement_count FROM poi_feature""").fetchone()
        if result["poi_count"] != counts["core_count"] or result["supplement_count"] != counts["supplemental_count"]:
            raise RuntimeError("migration verification failed; transaction will roll back")
        deleted = db.execute("DELETE FROM spatial_feature WHERE dataset_id=ANY(%s::uuid[])",
                             (core + supplemental,)).rowcount
        if deleted != counts["core_count"] + counts["supplemental_count"]:
            raise RuntimeError("source delete count mismatch; transaction will roll back")
        print(f"consolidated {result['poi_count']} POIs and {result['supplement_count']} supplemental records; "
              f"removed {deleted} source feature rows")


if __name__ == "__main__":
    main()
