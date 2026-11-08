import shutil
import struct
import time
from pathlib import Path

from fastapi.testclient import TestClient

from dataset_service.config import Settings
from dataset_service.credentials import ensure_credentials
from dataset_service.main import create_app


def make_fixture(root: Path, image_content: bytes = b"image") -> None:
    images = root / "images"
    sparse = root / "sparse" / "0"
    images.mkdir(parents=True)
    sparse.mkdir(parents=True)
    (images / "a.JPG").write_bytes(image_content)
    (sparse / "cameras.txt").write_text("1 PINHOLE 10 10 1 1 5 5\n")
    (sparse / "images.txt").write_text("1 1 0 0 0 0 0 0 1 a.JPG\n1 2 1\n")
    (sparse / "points3D.txt").write_text("1 1 2 3 255 0 0 0.5 1 0\n")
    (sparse / "cameras.bin").write_bytes(struct.pack("<QiiQQ4d", 1, 1, 1, 10, 10, 1, 1, 5, 5))
    (sparse / "images.bin").write_bytes(
        struct.pack("<Qi7di", 1, 1, 1, 0, 0, 0, 0, 0, 0, 1)
        + b"a.JPG\0" + struct.pack("<Qddq", 1, 1, 2, 1)
    )
    (sparse / "points3D.bin").write_bytes(
        struct.pack("<QQ3d3BdQii", 1, 1, 1, 2, 3, 255, 0, 0, 0.5, 1, 1, 0)
    )


def wait_import(client: TestClient, headers: dict, import_id: str) -> dict:
    for _ in range(100):
        job = client.get(f"/admin/imports/{import_id}", headers=headers).json()
        if job["status"] in {"validated", "failed", "duplicate"}:
            return job
        time.sleep(0.02)
    raise AssertionError("import did not finish")


def test_import_publish_update_and_read(tmp_path: Path) -> None:
    source = tmp_path / "source"
    second = tmp_path / "second"
    make_fixture(source)
    shutil.copytree(source, second)
    (second / "images" / "a.JPG").write_bytes(b"updated")
    data = tmp_path / "data"
    credentials = ensure_credentials(data)
    admin = {"Authorization": f"Bearer {credentials['admin']}"}
    reader = {"Authorization": f"Bearer {credentials['reader']}"}
    app = create_app(Settings(data, {"garden": source, "garden_update": second}, 1))
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/datasets", headers=reader).json() == []
        assert client.post("/admin/datasets/import", json={"source_alias": "garden"}, headers=reader).status_code == 403
        response = client.post("/admin/datasets/import", json={"source_alias": "garden"}, headers={**admin, "Idempotency-Key": "first"})
        assert response.status_code == 202
        import_id = response.json()["id"]
        assert client.post("/admin/datasets/import", json={"source_alias": "garden"}, headers={**admin, "Idempotency-Key": "first"}).json()["id"] == import_id
        job = wait_import(client, admin, import_id)
        assert job["status"] == "validated", job
        version1 = job["version"]
        assert client.get(f"/datasets/garden/versions/{version1}/manifest", headers=reader).status_code == 404
        assert client.post(f"/admin/imports/{import_id}/publish", headers=admin).status_code == 200
        duplicate_id = client.post("/admin/datasets/import", json={"source_alias": "garden"}, headers=admin).json()["id"]
        duplicate = wait_import(client, admin, duplicate_id)
        assert duplicate["status"] == "duplicate" and duplicate["version"] == version1
        assert duplicate["copied_files"] == 0
        manifest = client.get(f"/datasets/garden/versions/{version1}/manifest", headers=reader).json()
        assert manifest["file_count"] == 7
        assert manifest["validation"]["registered_images"] == 1
        file_url = f"/datasets/garden/versions/{version1}/files/images/a.JPG"
        assert client.get(file_url, headers=reader).content == b"image"
        partial = client.get(file_url, headers={**reader, "Range": "bytes=1-3"})
        assert partial.status_code == 206 and partial.content == b"mag"
        assert client.get(file_url, headers={**reader, "Range": "bytes=99-"}).status_code == 416
        assert client.patch("/admin/datasets/garden", headers=admin, json={"name": "Garden", "description": "Demo"}).json()["name"] == "Garden"
        second_id = client.post("/admin/datasets/import", json={"source_alias": "garden_update"}, headers=admin).json()["id"]
        job2 = wait_import(client, admin, second_id)
        assert job2["status"] == "validated", job2
        version2 = job2["version"]
        client.post(f"/admin/imports/{second_id}/publish", headers=admin).raise_for_status()
        assert client.get("/datasets/garden", headers=reader).json()["current_version"] == version2
        assert client.get(file_url, headers=reader).content == b"image"
        client.post(f"/admin/datasets/garden/versions/{version1}/activate", headers=admin).raise_for_status()
        assert client.get("/datasets/garden", headers=reader).json()["current_version"] == version1
        assert any(event["action"] == "update_metadata" for event in client.get("/admin/audit", headers=admin).json())


def test_rejects_inconsistent_model(tmp_path: Path) -> None:
    source = tmp_path / "source"
    make_fixture(source)
    (source / "sparse" / "0" / "images.txt").write_text("1 1 0 0 0 0 0 0 1 missing.JPG\n1 2 1\n")
    data = tmp_path / "data"
    admin = {"Authorization": f"Bearer {ensure_credentials(data)['admin']}"}
    with TestClient(create_app(Settings(data, {"garden": source}, 1))) as client:
        import_id = client.post("/admin/datasets/import", json={"source_alias": "garden"}, headers=admin).json()["id"]
        job = wait_import(client, admin, import_id)
        assert job["status"] == "failed"
        assert client.post(f"/admin/imports/{import_id}/publish", headers=admin).status_code == 409
