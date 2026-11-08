import uuid
import json

from dataset_service.legacy_scenes import LegacySceneStore


def test_legacy_tree_migrates_to_scene_instances_and_restores_visibility(tmp_path):
    scenes = LegacySceneStore(tmp_path)
    scene_id = scenes.create("owner", "迁移测试", None, 2, 2)["Id"]
    folder_id = scenes.create_folder(scene_id, "混合目录", None)["id"]
    resource_id = str(uuid.uuid4())
    dataset_id = str(uuid.uuid4())
    video_id = str(uuid.uuid4())
    scenes.add_resources(scene_id, [{"ResourceId": resource_id, "Type": 12000008,
                                     "Label": "栅格", "MetaData": '{"legacy":true}',
                                     "SceneTreesId": folder_id}])
    poi_id = scenes.add_poi_layer(scene_id, dataset_id, [120, 30, 121, 31], "点位")["id"]
    scenes.add_video_group(scene_id, video_id)

    catalog = [{"Id": resource_id, "Type": 12000008, "BigResourceType": 12, "Label": "栅格"}]
    assert scenes.migrate_legacy_tree(scene_id, catalog) == {
        "folders": 1, "resources": 1, "poi": 1, "video": 1,
    }
    assert scenes.migrate_legacy_tree(scene_id, catalog) == {
        "folders": 0, "resources": 0, "poi": 0, "video": 0,
    }
    folders = scenes.layer_folders(scene_id)
    assert next(folder for folder in folders if folder["id"] == folder_id)["layer_category"] == "mixed"
    layers = scenes.layers(scene_id)
    assert len(layers) == 3
    raster = next(layer for layer in layers if layer["source_id"] == resource_id)
    assert raster["layer_id"] != resource_id
    assert raster["parent_folder_id"] == folder_id
    assert raster["default_visible"] is False
    assert raster["legacy_payload"]["metadata"] == '{"legacy":true}'
    with scenes._db() as db:
        assert db.execute("SELECT count(*) FROM scene_tree_migration_backup WHERE scene_id=?",
                          (scene_id,)).fetchone()[0] == 4
    assert scenes.resource_ids(scene_id) == [resource_id]
    assert scenes.update_layer(scene_id, raster["layer_id"], {"default_visible": True})
    assert next(layer for layer in scenes.layers(scene_id) if layer["layer_id"] == raster["layer_id"])["default_visible"] is True
    assert next(layer for layer in layers if layer["source_id"] == dataset_id)["layer_id"] == poi_id
    assert scenes.remove_poi_layer(scene_id, poi_id)
    assert scenes.remove_video_group(scene_id, video_id)


def test_poi_query_definition_survives_reopen(tmp_path):
    scene_id = LegacySceneStore(tmp_path).create("owner", "查询测试", None, 2, 1)["Id"]
    scenes = LegacySceneStore(tmp_path)
    dataset_id = str(uuid.uuid4())
    geometry = {"type": "Polygon", "coordinates": [[[120, 30], [121, 30], [121, 31], [120, 31], [120, 30]]]}
    layer, reused = scenes.add_layer({
        "scene_id": scene_id, "layer_category": "poi", "category_label": "POI",
        "source_kind": "spatial_dataset", "source_id": dataset_id,
        "display_name": "按类别筛选", "scope_geometry": geometry, "scope_crs": "EPSG:4326",
        "filter_properties": {"category": "school"}, "default_visible": True,
    })
    assert reused is False
    restored = next(item for item in LegacySceneStore(tmp_path).layers(scene_id)
                    if item["layer_id"] == layer["layer_id"])
    assert restored["scope_geometry"] == geometry
    assert restored["filter_properties"] == {"category": "school"}
    assert restored["default_visible"] is True
    assert scenes.update_layer(scene_id, layer["layer_id"], {"filter_properties": None, "default_visible": False})
    cleared = next(item for item in scenes.layers(scene_id) if item["layer_id"] == layer["layer_id"])
    assert cleared["filter_properties"] is None
    assert cleared["default_visible"] is False


def test_legacy_subset_keeps_source_and_subset_ids(tmp_path):
    scenes = LegacySceneStore(tmp_path)
    scene_id = scenes.create("owner", "子集测试", None, 2, 1)["Id"]
    source_id, subset_id = str(uuid.uuid4()), str(uuid.uuid4())
    scenes.add_resources(scene_id, [{"ResourceId": subset_id, "Type": 4000001, "Label": "影像子集"}])
    catalog = [{"Id": subset_id, "Type": 4000001, "BigResourceType": 4,
                "MetaData": json.dumps({"ResourceSubset": {"source_resource_id": source_id}})}]
    scenes.migrate_legacy_tree(scene_id, catalog)
    layer = scenes.layers(scene_id)[0]
    assert layer["source_id"] == source_id
    assert layer["subset_resource_id"] == subset_id
    assert scenes.resource_ids(scene_id) == [subset_id]


def test_mixed_folder_can_receive_and_move_different_layer_categories(tmp_path):
    scenes = LegacySceneStore(tmp_path)
    scene_id = scenes.create("owner", "混合目录测试", None, 2, 2)["Id"]
    first = scenes.create_layer_folder(scene_id, "资料一", "mixed")
    second = scenes.create_layer_folder(scene_id, "资料二", "mixed")
    resource_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    layers = []
    for category, source_id in zip(("raster", "model"), resource_ids):
        layer, _ = scenes.add_layer({"scene_id": scene_id, "parent_folder_id": first["id"],
                                     "layer_category": category, "source_kind": "legacy_resource",
                                     "source_id": source_id, "display_name": category})
        layers.append(layer)
    assert scenes.apply_tree_order(scene_id, [{"id": second["id"],
        "children": [{"id": layer["layer_id"], "children": []} for layer in layers]}])["updated"] == 3
    assert {layer["parent_folder_id"] for layer in scenes.layers(scene_id)} == {second["id"]}


def test_existing_scene_instance_wins_over_old_link(tmp_path):
    scenes = LegacySceneStore(tmp_path)
    scene_id = scenes.create("owner", "重复关联测试", None, 2, 1)["Id"]
    source_id = str(uuid.uuid4())
    saved, _ = scenes.add_layer({"scene_id": scene_id, "layer_category": "raster",
                                 "source_kind": "legacy_resource", "source_id": source_id,
                                 "display_name": "新版名称", "default_visible": True})
    scenes.add_resources(scene_id, [{"ResourceId": source_id, "Type": 12000008, "Label": "旧版名称"}])
    assert scenes.migrate_legacy_tree(scene_id, [{"Id": source_id, "Type": 12000008,
                                                   "BigResourceType": 12}])["resources"] == 0
    layers = scenes.layers(scene_id)
    assert len(layers) == 1
    assert layers[0]["layer_id"] == saved["layer_id"]
    assert layers[0]["default_visible"] is True


def test_poi_migration_recognizes_reordered_rectangle(tmp_path):
    scenes = LegacySceneStore(tmp_path)
    scene_id = scenes.create("owner", "范围去重测试", None, 2, 1)["Id"]
    dataset_id = str(uuid.uuid4())
    canonical = {"type": "Polygon", "coordinates": [[[120.0, 30.0], [120.0, 31.0],
                  [121.0, 31.0], [121.0, 30.0], [120.0, 30.0]]]}
    saved, _ = scenes.add_layer({"scene_id": scene_id, "layer_category": "poi",
                                 "source_kind": "spatial_dataset", "source_id": dataset_id,
                                 "scope_geometry": canonical, "display_name": "新版范围",
                                 "default_visible": True})
    scenes.add_poi_layer(scene_id, dataset_id, [120, 30, 121, 31], "旧版范围")
    assert scenes.migrate_legacy_tree(scene_id, [])["poi"] == 0
    layers = scenes.layers(scene_id)
    assert len(layers) == 1
    assert layers[0]["layer_id"] == saved["layer_id"]
    assert layers[0]["default_visible"] is True


def test_scene_tree_type_only_selects_initial_folder_template(tmp_path):
    scenes = LegacySceneStore(tmp_path)
    typed = scenes.create("owner", "分类模板", None, 2, 1)["Id"]
    custom = scenes.create("owner", "空白模板", None, 2, 2)["Id"]
    assert {folder["layer_category"] for folder in scenes.layer_folders(typed)} == {
        "terrain", "vector", "imagery", "model", "video", "raster", "pointcloud", "poi",
    }
    assert scenes.layer_folders(custom) == []


def test_odm_resource_uses_scene_layer_id_and_restores_visibility(tmp_path):
    scenes = LegacySceneStore(tmp_path)
    scene_id = scenes.create("owner", "ODM 影像", None, 2, 2)["Id"]
    source_id = str(uuid.uuid4())
    layer, reused = scenes.add_layer({
        "scene_id": scene_id, "layer_category": "imagery", "category_label": "影像图层",
        "source_kind": "oblique_resource", "source_id": source_id,
        "display_name": "ODM 航摄包", "default_visible": True,
    })
    assert reused is False
    assert layer["layer_id"] != source_id
    restored = LegacySceneStore(tmp_path).layers(scene_id)[0]
    assert restored["source_kind"] == "oblique_resource"
    assert restored["source_id"] == source_id
    assert restored["default_visible"] is True
    assert scenes.update_layer(scene_id, layer["layer_id"], {"default_visible": False})
    assert LegacySceneStore(tmp_path).layers(scene_id)[0]["default_visible"] is False


def test_raster_collection_uses_scene_layer_id_and_restores_visibility(tmp_path):
    scenes = LegacySceneStore(tmp_path)
    scene_id = scenes.create("owner", "四期影像", None, 2, 2)["Id"]
    source_id = str(uuid.uuid4())
    layer, reused = scenes.add_layer({
        "scene_id": scene_id, "layer_category": "raster", "category_label": "区域栅格图层",
        "source_kind": "raster_collection", "source_id": source_id,
        "display_name": "德清卫星影像_2025-01-10", "default_visible": False,
    })
    assert reused is False
    assert layer["layer_id"] != source_id
    assert scenes.update_layer(scene_id, layer["layer_id"], {"default_visible": True})
    restored = LegacySceneStore(tmp_path).layers(scene_id)[0]
    assert restored["source_kind"] == "raster_collection"
    assert restored["source_id"] == source_id
    assert restored["default_visible"] is True
