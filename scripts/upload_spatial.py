#!/usr/bin/env python3
"""Upload one Shapefile or a directory of Shapefiles to the spatial API."""

import argparse
import http.client
import json
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


SUFFIXES = (".shp", ".shx", ".dbf", ".prj", ".cpg")
REQUIRED = SUFFIXES[:3]
CHUNK_SIZE = 1024 * 1024


def token_from_args(path: Path | None) -> str:
    if path is None:
        token = os.getenv("DEQING_ADMIN_TOKEN") or os.getenv("DEQING_TOKEN")
    else:
        raw = path.read_text(encoding="utf-8").strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = raw
        token = data.get("admin") if isinstance(data, dict) else data
    if not isinstance(token, str) or not token.strip():
        raise ValueError("provide --token-file (credentials.json or plain token) or DEQING_ADMIN_TOKEN")
    return token.strip()


def api_url(base_url: str, path: str) -> str:
    base = base_url.rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("--base-url must be an http(s) URL without query or fragment")
    return base + path


def request_json(url: str, token: str) -> dict:
    request = Request(url, headers={"Authorization": "Bearer " + token})
    with urlopen(request, timeout=60) as response:
        return json.load(response)


def existing_datasets(base_url: str, token: str) -> dict[str, dict]:
    found = {}
    offset = 0
    while True:
        page = request_json(api_url(base_url, f"/spatial/datasets?limit=1000&offset={offset}"), token)["datasets"]
        for row in page:
            found[row["name"]] = row
        if len(page) < 1000:
            return found
        offset += len(page)


def components(shp: Path) -> dict[str, Path]:
    if not shp.is_file() or shp.suffix.lower() != ".shp":
        raise ValueError(f"not a .shp file: {shp}")
    files = {}
    for path in shp.parent.iterdir():
        suffix = path.suffix.lower()
        if path.is_file() and path.stem == shp.stem and suffix in SUFFIXES:
            if suffix in files:
                raise ValueError(f"multiple {suffix} files for {shp.stem}")
            files[suffix] = path
    missing = [suffix for suffix in REQUIRED if suffix not in files]
    if missing:
        raise ValueError(f"{shp.stem}: missing {', '.join(missing)}")
    return files


def multipart_parts(files: dict[str, Path], fields: dict[str, str], boundary: str):
    for name, value in fields.items():
        head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n\r\n").encode("utf-8")
        yield head, value.encode("utf-8"), None
    for suffix in SUFFIXES:
        if suffix not in files:
            continue
        path = files[suffix]
        if any(char in path.name for char in ('"', "\r", "\n")):
            raise ValueError(f"unsupported filename: {path.name}")
        name = suffix[1:]
        head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                f"filename=\"{path.name}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode("utf-8")
        yield head, None, path


def upload(base_url: str, token: str, files: dict[str, Path], fields: dict[str, str]) -> dict:
    url = urlsplit(api_url(base_url, "/spatial/datasets/import"))
    boundary = "deqing-" + uuid.uuid4().hex
    parts = list(multipart_parts(files, fields, boundary))
    closing = f"--{boundary}--\r\n".encode()
    length = len(closing) + sum(len(head) + (len(value) if value is not None else path.stat().st_size) + 2
                                for head, value, path in parts)
    connection_type = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(url.hostname, url.port, timeout=7200)
    try:
        connection.putrequest("POST", url.path)
        connection.putheader("Authorization", "Bearer " + token)
        connection.putheader("Content-Type", "multipart/form-data; boundary=" + boundary)
        connection.putheader("Content-Length", str(length))
        connection.endheaders()
        for head, value, path in parts:
            connection.send(head)
            if value is not None:
                connection.send(value)
            else:
                with path.open("rb") as source:
                    while chunk := source.read(CHUNK_SIZE):
                        connection.send(chunk)
            connection.send(b"\r\n")
        connection.send(closing)
        response = connection.getresponse()
        raw = response.read()
        if response.status != 201:
            raise ValueError(f"HTTP {response.status}: {raw.decode('utf-8', errors='replace')[:1000]}")
        return json.loads(raw)
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="a .shp file or a directory containing Shapefile groups")
    parser.add_argument("--base-url", default=os.getenv("DEQING_DATA_URL", "http://192.168.150.41:7110"))
    parser.add_argument("--token-file", type=Path, help="credentials.json or a plain-text admin token")
    parser.add_argument("--name", help="dataset name; only valid for a single .shp file")
    parser.add_argument("--source-srid", type=int, help="EPSG code when .prj is absent")
    parser.add_argument("--encoding", help="DBF encoding override")
    parser.add_argument("--vertical-crs", help="declared vertical CRS for Z values, e.g. EPSG:5703")
    parser.add_argument("--vertical-unit", help="unit label for preserved Z values, e.g. metre; does not transform Z")
    parser.add_argument("--confirm-location-review", action="store_true",
                        help="confirm that CRS, axis order and WGS84 bounds were reviewed")
    parser.add_argument("--location-review-source", required=True,
                        help="review evidence such as the source CRS document or GIS inspection record")
    parser.add_argument("--skip-existing", action="store_true", help="skip matching names when record counts agree")
    args = parser.parse_args()
    try:
        if not args.confirm_location_review:
            parser.error("--confirm-location-review is required before importing map data")
        if len(args.location_review_source.strip()) < 5:
            parser.error("--location-review-source must identify the reviewed location evidence")
        token = token_from_args(args.token_file)
        if args.source.is_dir():
            if args.name:
                raise ValueError("--name is only valid for a single .shp file")
            sources = sorted(path for path in args.source.iterdir() if path.is_file() and path.suffix.lower() == ".shp")
            if not sources:
                raise ValueError(f"no .shp files found in {args.source}")
        else:
            sources = [args.source]
        existing = existing_datasets(args.base_url, token)
        errors = []
        for source in sources:
            try:
                files = components(source)
                name = args.name or source.stem
                if name in existing:
                    if not args.skip_existing:
                        raise ValueError(f"dataset name already exists: {name}; use --skip-existing to resume")
                    expected = (files[".shx"].stat().st_size - 100) // 8
                    actual = existing[name]["feature_count"]
                    if expected != actual:
                        raise ValueError(f"existing {name} has {actual} records; source has {expected}")
                    print(f"SKIP {name}: already imported ({actual} features)", flush=True)
                    continue
                fields = {"name": name, "location_review_confirmed": "true",
                          "location_review_source": args.location_review_source.strip()}
                if args.source_srid is not None:
                    fields["source_srid"] = str(args.source_srid)
                if args.encoding:
                    fields["encoding"] = args.encoding
                if args.vertical_crs:
                    fields["vertical_crs"] = args.vertical_crs
                if args.vertical_unit:
                    fields["vertical_unit"] = args.vertical_unit
                row = upload(args.base_url, token, files, fields)
                existing[name] = row
                print(f"OK {name}: id={row['id']} features={row['feature_count']}", flush=True)
            except (OSError, ValueError, URLError, http.client.HTTPException, json.JSONDecodeError) as error:
                print(f"ERROR {source.name}: {error}", file=sys.stderr, flush=True)
                errors.append(source.name)
        return 1 if errors else 0
    except (OSError, ValueError, HTTPError, URLError, KeyError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
