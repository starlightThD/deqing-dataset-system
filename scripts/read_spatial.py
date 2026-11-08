#!/usr/bin/env python3
"""List spatial datasets and read their GeoJSON features."""

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


def token_from_args(path: Path | None) -> str:
    if path is None:
        token = os.getenv("DEQING_READER_TOKEN") or os.getenv("DEQING_TOKEN")
    else:
        raw = path.read_text(encoding="utf-8").strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = raw
        token = (data.get("reader") or data.get("admin")) if isinstance(data, dict) else data
    if not isinstance(token, str) or not token.strip():
        raise ValueError("provide --token-file (credentials.json or plain token) or DEQING_READER_TOKEN")
    return token.strip()


def request_json(base_url: str, path: str, token: str, params: dict | None = None) -> dict:
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("--base-url must be an http(s) URL without query or fragment")
    url = base_url.rstrip("/") + path
    if params:
        url += "?" + urlencode(params)
    request = Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urlopen(request, timeout=120) as response:
            return json.load(response)
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise ValueError(f"HTTP {error.code}: {body[:1000]}") from error


def feature_pages(base_url: str, token: str, dataset_id: str, bbox: str | None,
                  page_size: int, offset: int, all_pages: bool):
    while True:
        params = {"limit": page_size, "offset": offset}
        if bbox:
            params["bbox"] = bbox
        page = request_json(base_url, f"/spatial/datasets/{dataset_id}/features", token, params)
        yield page
        if not all_pages or not page.get("has_more"):
            break
        offset += len(page["features"])


def write_features(pages, output: Path | None) -> None:
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp") if output else None
    stream = temporary.open("w", encoding="utf-8") if temporary else sys.stdout
    complete = False
    try:
        stream.write('{"type":"FeatureCollection","features":[')
        first = True
        for page in pages:
            for feature in page["features"]:
                if not first:
                    stream.write(",")
                json.dump(feature, stream, ensure_ascii=False, separators=(",", ":"))
                first = False
        stream.write("]}\n")
        complete = True
    finally:
        if temporary:
            stream.close()
            if complete:
                temporary.replace(output)
            else:
                temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.getenv("DEQING_DATA_URL", "http://192.168.150.41:7110"))
    parser.add_argument("--token-file", type=Path, help="credentials.json or a plain-text reader/admin token")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list all datasets with IDs and feature counts")
    details = commands.add_parser("get", help="show one dataset's metadata")
    details.add_argument("dataset_id")
    features = commands.add_parser("features", help="read GeoJSON features")
    features.add_argument("dataset_id")
    features.add_argument("--bbox", help="minLon,minLat,maxLon,maxLat in EPSG:4326")
    features.add_argument("--limit", type=int, default=100, help="page size, 1..1000")
    features.add_argument("--offset", type=int, default=0)
    features.add_argument("--all", action="store_true", help="fetch every page")
    features.add_argument("--output", type=Path, help="write GeoJSON to this file instead of stdout")
    args = parser.parse_args()
    try:
        token = token_from_args(args.token_file)
        if args.command == "list":
            offset = 0
            while True:
                page = request_json(args.base_url, "/spatial/datasets", token, {"limit": 1000, "offset": offset})["datasets"]
                for row in page:
                    print(f"{row['id']}\t{row['feature_count']}\t{row['name']}")
                if len(page) < 1000:
                    break
                offset += len(page)
        elif args.command == "get":
            row = request_json(args.base_url, f"/spatial/datasets/{args.dataset_id}", token)
            print(json.dumps(row, ensure_ascii=False, indent=2))
        else:
            if not 1 <= args.limit <= 1000 or args.offset < 0:
                raise ValueError("--limit must be 1..1000 and --offset must be nonnegative")
            pages = feature_pages(args.base_url, token, args.dataset_id, args.bbox,
                                  args.limit, args.offset, args.all)
            write_features(pages, args.output)
        return 0
    except (OSError, ValueError, URLError, KeyError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
