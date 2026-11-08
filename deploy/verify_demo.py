"""Run the planned end-to-end garden demo from inside the service container."""
import argparse
import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:7110")
    parser.add_argument("--credentials", type=Path, default=Path("/workspace/data/credentials.json"))
    parser.add_argument("--second-alias", default=None)
    args = parser.parse_args()
    credentials = json.loads(args.credentials.read_text())

    def call(method, path, role="admin", body=None, extra=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": "Bearer " + credentials[role]}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if extra:
            headers.update(extra)
        request = urllib.request.Request(args.url + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read()
            return json.loads(payload) if response.headers.get_content_type() == "application/json" else payload

    def import_and_publish(alias):
        job = call("POST", "/admin/datasets/import", body={"source_alias": alias})
        for _ in range(300):
            job = call("GET", "/admin/imports/" + job["id"])
            if job["status"] in {"validated", "duplicate", "failed"}:
                break
            time.sleep(1)
        if job["status"] == "failed":
            raise RuntimeError("import failed: " + str(job["error"]))
        if job["status"] not in {"validated", "duplicate"}:
            raise TimeoutError("import did not finish")
        if job["status"] == "validated":
            call("POST", "/admin/imports/" + job["id"] + "/publish")
        return job["version"], job["status"]

    first, first_result = import_and_publish("garden")
    manifest = call("GET", f"/datasets/garden/versions/{first}/manifest", role="reader")
    assert manifest["image_count"] == 185 and manifest["file_count"] == 191
    for relative in ("images/DSC07956.JPG", "sparse/0/cameras.bin"):
        expected = next(item["sha256"] for item in manifest["files"] if item["relative_path"] == relative)
        content = call("GET", f"/datasets/garden/versions/{first}/files/{relative}", role="reader")
        assert hashlib.sha256(content).hexdigest() == expected
    try:
        call("PATCH", "/admin/datasets/garden", role="reader", body={"name": "blocked"})
    except urllib.error.HTTPError as error:
        assert error.code == 403
    else:
        raise AssertionError("reader was allowed to update metadata")
    call("PATCH", "/admin/datasets/garden", body={"name": "garden", "description": "FastGS garden 演示数据集"})

    second = None
    if args.second_alias:
        second, second_result = import_and_publish(args.second_alias)
        assert second != first
        assert call("GET", "/datasets/garden", role="reader")["current_version"] == second
        assert call("GET", f"/datasets/garden/versions/{first}/manifest", role="reader")["version"] == first
        call("POST", f"/admin/datasets/garden/versions/{first}/activate")
        assert call("GET", "/datasets/garden", role="reader")["current_version"] == first
        print(json.dumps({"first": first, "first_import": first_result, "second": second, "second_import": second_result, "active": first, "files_verified": 2, "reader_write": 403}, ensure_ascii=False))
    else:
        print(json.dumps({"first": first, "first_import": first_result, "files_verified": 2, "reader_write": 403}, ensure_ascii=False))


if __name__ == "__main__":
    main()
