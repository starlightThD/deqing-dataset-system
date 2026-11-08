#!/usr/bin/env python3
"""Index existing oblique photos in PostGIS without copying image bytes."""

import argparse
import getpass
import json
import os
from pathlib import Path

from dataset_service.oblique import import_legacy_catalog
from dataset_service.spatial import SpatialStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(os.getenv("DEQING_LEGACY_OUT", "/workspace/legacy/out")))
    parser.add_argument("--mirror-root", type=Path, default=Path(os.getenv("DEQING_LEGACY_CACHE_DIR", "/workspace/data/legacy-mirror")))
    parser.add_argument("--data-dir", type=Path, default=Path(os.getenv("DEQING_DATA_DIR", "/workspace/data")))
    parser.add_argument("--agl-m", type=float, default=120)
    parser.add_argument("--ground-window-m", type=float, default=30)
    parser.add_argument("--buffer-m", type=float, default=15)
    parser.add_argument("--max-horizontal-factor", type=float, default=8)
    parser.add_argument("--confirm-location-review", action="store_true",
                        help="confirm that source CRS and camera-derived positions were reviewed")
    parser.add_argument("--location-review-source", required=True,
                        help="review evidence; note that generated footprints are approximate, not guaranteed")
    args = parser.parse_args()
    if not args.confirm_location_review:
        parser.error("--confirm-location-review is required before importing spatial image positions")
    result = import_legacy_catalog(SpatialStore(args.data_dir), args.source_root, args.mirror_root,
                                   args.agl_m, args.ground_window_m, args.buffer_m, args.max_horizontal_factor,
                                   args.confirm_location_review, args.location_review_source, getpass.getuser())
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
