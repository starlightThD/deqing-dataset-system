import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient

from dataset_service.config import Settings
from dataset_service.legacy import LegacyStore
from dataset_service.legacy_auth import ensure_legacy_auth
from dataset_service.main import create_app


ROOT = Path(__file__).resolve().parents[1] / "remote-data" / "out"


def test_legacy_contract_with_real_catalog(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    auth = ensure_legacy_auth(data_dir)
    store = LegacyStore(ROOT)
    key, path = next(iter(store.keys.items()))
    app = create_app(Settings(data_dir, {"garden": tmp_path / "unused"}, 1), legacy_root=ROOT)
    with TestClient(app) as client:
        payload = {"UserName": auth["username"], "Password": hashlib.md5(auth["password"].encode()).hexdigest()}
        assert client.post("/api/Login/Login", json=payload, headers={"Content-Type": "text/plain"}).status_code == 415
        login = client.post("/api/Login/Login", json=payload)
        assert login.status_code == 200 and login.json()["State"] == 0
        token = login.json()["Data"]["Token"]
        claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
        assert isinstance(claims["AuthInfo"], str)
        assert json.loads(claims["AuthInfo"])["ExpireDays"] == 3
        headers = {"Authorization": "Bearer " + token}
        endpoint = "/api/cim/CIMScene/GetCIMSceneResourceByPage"
        assert client.post(endpoint, json={"PageIndex": 1, "PageSize": 1}).status_code == 401
        page = client.post(endpoint, json={"PageIndex": 1, "PageSize": 3}, headers=headers).json()
        assert page["Total"] == 369 and len(page["Rows"]) == 3
        assert len(page["Rows"][0]) == 42
        assert isinstance(page["Rows"][0]["MetaData"], str)
        filtered = client.post(endpoint, json={"Type": 4000001, "PageIndex": 1, "PageSize": 100}, headers=headers).json()
        assert filtered["Total"] == 8
        file_url = "/api/DownLoad/GetFileStream?" + quote(key, safe="/")
        response = client.get(file_url, headers=headers)
        assert response.status_code == 200 and response.content == path.read_bytes()
        preflight = client.options("/api/Login/Login", headers={"Origin": "http://localhost:8050", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
        assert preflight.status_code == 200 and preflight.headers["access-control-allow-origin"] == "*"
        assert client.get("/api/DownLoad/GetFileStream?key=" + quote(key, safe="/"), headers=headers).status_code == 204
        assert client.get("/api/DownLoad/GetFileStream?group1/M00/none.jpg", headers=headers).status_code == 204
        assert client.post(file_url, headers=headers).status_code == 405
        assert client.get("/healthz").status_code == 200
