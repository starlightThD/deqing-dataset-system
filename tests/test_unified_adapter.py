import hashlib
import uuid
from pathlib import Path
from urllib.parse import urljoin

from fastapi.testclient import TestClient

from dataset_service.config import Settings
from dataset_service.legacy_auth import ensure_legacy_auth
from dataset_service.legacy_scenes import LegacySceneStore
from dataset_service.main import create_app
from dataset_service.resource_query import _scoped_content_uri


ROOT = Path(__file__).resolve().parents[1] / "remote-data" / "out"
GEOMETRY = {"type": "Polygon", "coordinates": [[[120, 30], [121, 30],
             [121, 31], [120, 31], [120, 30]]]}


def test_dynamic_tileset_children_keep_proxy_prefix():
    source_id, version_id, layer_id = (str(uuid.uuid4()) for _ in range(3))
    root = f"/data-api/spatial/resources/{source_id}/versions/{version_id}/tileset.json?scene_layer_id={layer_id}"
    nested = f"/data-api/spatial/resources/{source_id}/versions/{version_id}/files/Data/child.json?scene_layer_id={layer_id}"

    child_uri = _scoped_content_uri("tileset.json", "Data/child.json", layer_id)
    tile_uri = _scoped_content_uri("Data/child.json", "Data/Tile 1.b3dm", layer_id)

    assert urljoin(root, child_uri) == nested
    assert urljoin(nested, tile_uri) == (
        f"/data-api/spatial/resources/{source_id}/versions/{version_id}/files/Data/Tile%201.b3dm"
        f"?scene_layer_id={layer_id}")
    assert urljoin(root.replace("/data-api", "", 1), child_uri).startswith("/spatial/resources/")


def test_scene_tree_restores_scoped_layer_contract(tmp_path):
    data_dir = tmp_path / "data"
    auth = ensure_legacy_auth(data_dir)
    scenes = LegacySceneStore(data_dir)
    scene_id = scenes.create("owner", "统一查询场景", None, 2, 2)["Id"]
    source_id, version_id = str(uuid.uuid4()), str(uuid.uuid4())
    saved, _ = scenes.add_layer({
        "scene_id": scene_id, "layer_category": "model",
        "source_kind": "pointcloud_resource", "source_id": source_id,
        "source_version_id": version_id, "selection_mode": "intersecting_tiles",
        "scope_geometry": GEOMETRY, "scope_crs": "EPSG:4326",
        "display_name": "范围内三维瓦片", "default_visible": False,
    })

    app = create_app(Settings(data_dir, {"garden": tmp_path / "unused"}, 1), legacy_root=ROOT)
    with TestClient(app) as client:
        login = client.post("/api/Login/Login", json={
            "UserName": auth["username"],
            "Password": hashlib.md5(auth["password"].encode()).hexdigest(),
        })
        assert login.status_code == 200
        token = login.json()["Data"]["Token"]
        response = client.get(f"/api/cim/CIMScene/GetResourceTreeForCIMScene/{scene_id}",
                              headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200

    def find_layer(nodes):
        for node in nodes:
            if node.get("layerId") == saved["layer_id"]:
                return node
            child = find_layer(node.get("children") or [])
            if child:
                return child
        return None

    node = find_layer(response.json())
    assert node is not None
    assert node["type"] == "Scoped3DTiles"
    assert node["sourceId"] == source_id
    assert node["sourceVersionId"] == version_id
    assert node["selectionMode"] == "intersecting_tiles"
    assert node["scopeGeometry"] == GEOMETRY
    assert node["attribute"]["IsChecked"] is False
    assert node["renderDescriptor"]["entry_path"] == (
        f"/spatial/resources/{source_id}/versions/{version_id}/tileset.json"
        f"?scene_layer_id={saved['layer_id']}")
