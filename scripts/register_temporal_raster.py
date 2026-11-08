#!/usr/bin/env python3
"""Register completed imagery_<YYYYMMDD> TMS trees already copied into DEQING_DATA_DIR/rasters."""

import argparse
import getpass
import json
from pathlib import Path

from dataset_service.config import Settings
from dataset_service.spatial import SpatialStore
from dataset_service.temporal_raster import register_local_collection


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True,
                        help="collection directory inside $DEQING_DATA_DIR/rasters")
    parser.add_argument("--collection-id", required=True)
    parser.add_argument("--name", default="德清多期卫星影像")
    parser.add_argument("--confirm-location-review", action="store_true",
                        help="confirm that source CRS, WGS84 bounds, tile matrix and scheme were reviewed")
    parser.add_argument("--location-review-source", required=True,
                        help="review evidence, such as a checked source metadata or GIS inspection record")
    args = parser.parse_args()
    if not args.confirm_location_review:
        parser.error("--confirm-location-review is required before registering a map raster")
    settings = Settings.from_env()
    store = SpatialStore(settings.data_dir)
    store.ensure_schema()
    result = register_local_collection(store, settings.data_dir, args.collection_id,
                                       args.root, name=args.name,
                                       location_review_confirmed=args.confirm_location_review,
                                       location_review_source=args.location_review_source,
                                       reviewer=getpass.getuser())
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
