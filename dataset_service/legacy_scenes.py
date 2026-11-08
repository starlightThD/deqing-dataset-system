"""Persistent local scene records for the legacy web client API."""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="microseconds")


class LegacySceneStore:
    """Store scenes separately from the read-only remote API mirror."""

    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "legacy_scenes.sqlite3"
        with self._db() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS scene (
                    id TEXT PRIMARY KEY,
                    owner_user_id TEXT NOT NULL,
                    name TEXT NOT NULL COLLATE NOCASE,
                    remark TEXT,
                    scene_engine_type INTEGER NOT NULL DEFAULT 2,
                    scene_tree_type INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    config_json TEXT NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_local_scene_name ON scene(name COLLATE NOCASE)")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_local_scene_name_unique ON scene(name COLLATE NOCASE)")
            db.execute("""
                CREATE TABLE IF NOT EXISTS scene_resource (
                    scene_id TEXT NOT NULL REFERENCES scene(id) ON DELETE CASCADE,
                    resource_id TEXT NOT NULL,
                    resource_type INTEGER NOT NULL,
                    label TEXT,
                    metadata TEXT,
                    relate_resource_id TEXT,
                    extended_attributes TEXT,
                    osgb_id TEXT,
                    custom_tree_id TEXT,
                    sort INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(scene_id, resource_id)
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_scene_resource_scene_sort ON scene_resource(scene_id, sort)")
            db.execute("""
                CREATE TABLE IF NOT EXISTS legacy_scene_resource (
                    scene_id TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    resource_type INTEGER NOT NULL,
                    label TEXT,
                    metadata TEXT,
                    relate_resource_id TEXT,
                    extended_attributes TEXT,
                    osgb_id TEXT,
                    custom_tree_id TEXT,
                    sort INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(scene_id, resource_id)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS scene_poi_layer (
                    id TEXT PRIMARY KEY,
                    scene_id TEXT NOT NULL,
                    dataset_id TEXT NOT NULL,
                    bbox_json TEXT NOT NULL,
                    name TEXT NOT NULL,
                    sort INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(scene_id, dataset_id, bbox_json)
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_scene_poi_layer_scene ON scene_poi_layer(scene_id,sort)")
            db.execute("""
                CREATE TABLE IF NOT EXISTS scene_video_layer (
                    scene_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(scene_id, group_id)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS scene_tree_folder (
                    id TEXT PRIMARY KEY,
                    scene_id TEXT NOT NULL REFERENCES scene(id) ON DELETE CASCADE,
                    parent_id TEXT,
                    label TEXT NOT NULL,
                    sort INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_scene_tree_folder_scene_parent ON scene_tree_folder(scene_id,parent_id,sort)")
            db.execute("""CREATE TABLE IF NOT EXISTS scene_layer_folder (
                id TEXT PRIMARY KEY, scene_id TEXT NOT NULL, parent_id TEXT,
                label TEXT NOT NULL, layer_category TEXT NOT NULL,
                sort INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_scene_layer_folder_scene ON scene_layer_folder(scene_id,parent_id,sort)")
            db.execute("""CREATE TABLE IF NOT EXISTS scene_layer (
                id TEXT PRIMARY KEY, scene_id TEXT NOT NULL, parent_folder_id TEXT NOT NULL,
                layer_category TEXT NOT NULL, source_kind TEXT NOT NULL, source_id TEXT NOT NULL,
                subset_resource_id TEXT, scope_geometry_json TEXT, scope_crs TEXT,
                source_version_id TEXT, selection_mode TEXT,
                filter_properties_json TEXT,
                legacy_payload_json TEXT,
                display_name TEXT NOT NULL, default_visible INTEGER NOT NULL DEFAULT 0,
                sort INTEGER NOT NULL DEFAULT 0, request_id TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                CHECK(default_visible IN (0,1))
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_scene_layer_scene ON scene_layer(scene_id,parent_folder_id,sort)")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_scene_layer_request ON scene_layer(scene_id,request_id) WHERE request_id IS NOT NULL")
            if "filter_properties_json" not in {row["name"] for row in db.execute("PRAGMA table_info(scene_layer)")}:
                db.execute("ALTER TABLE scene_layer ADD COLUMN filter_properties_json TEXT")
            if "legacy_payload_json" not in {row["name"] for row in db.execute("PRAGMA table_info(scene_layer)")}:
                db.execute("ALTER TABLE scene_layer ADD COLUMN legacy_payload_json TEXT")
            if "source_version_id" not in {row["name"] for row in db.execute("PRAGMA table_info(scene_layer)")}:
                db.execute("ALTER TABLE scene_layer ADD COLUMN source_version_id TEXT")
            if "selection_mode" not in {row["name"] for row in db.execute("PRAGMA table_info(scene_layer)")}:
                db.execute("ALTER TABLE scene_layer ADD COLUMN selection_mode TEXT")
            db.execute("""CREATE TABLE IF NOT EXISTS scene_tree_migration_backup (
                scene_id TEXT NOT NULL, source_table TEXT NOT NULL, source_key TEXT NOT NULL,
                row_json TEXT NOT NULL, migrated_at TEXT NOT NULL,
                PRIMARY KEY(scene_id,source_table,source_key)
            )""")
        self.path.chmod(0o600)

    @contextmanager
    def _db(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def create(self, owner_user_id: str, name: str, remark: str | None,
               scene_engine_type: int, scene_tree_type: int) -> dict:
        scene_id = str(uuid.uuid4())
        created_at = _now()
        row = {
            "Id": scene_id,
            "Name": name,
            "OrganizationId": None,
            "Remark": remark,
            "FileId": None,
            "Matrix": None,
            "TenantId": "00000000-0000-0000-0000-000000000001",
            "LightSource": json.dumps({
                "angleYSlider": 45, "angleZSlider": 0, "intensity": 0.7,
                "value": True, "R": 0.7, "G": 0.7, "B": 0.7,
                "sAO": False, "progressiveRendering": False, "antiAliasing": 0,
            }, separators=(",", ":")),
            "ManipulatorType": 1,
            "SceneFinenessType": 1,
            "ModelFinenessType": 1,
            "CreateById": owner_user_id,
            "CreateByTime": created_at,
            "EditById": owner_user_id,
            "EditTime": created_at,
            "IsDeleted": False,
            "SceneBgColor": "#213D5E",
            "LogoFileId": None,
            "ResourceCommonAttr": None,
            "EffectsConfig": None,
            "SceneEngineType": scene_engine_type,
            "SceneApplicationType": 1,
            "SceneTreeType": scene_tree_type,
        }
        with self._db() as db:
            db.execute(
                """INSERT INTO scene
                   (id,owner_user_id,name,remark,scene_engine_type,scene_tree_type,created_at,updated_at,config_json)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (scene_id, owner_user_id, name, remark, scene_engine_type, scene_tree_type,
                 created_at, created_at, json.dumps(row, ensure_ascii=False, separators=(",", ":"))),
            )
            if scene_tree_type == 1:
                labels = {"terrain": "地形图层", "vector": "矢量图层", "imagery": "影像图层",
                          "model": "模型图层", "video": "视频图层", "raster": "栅格图层",
                          "pointcloud": "点云图层", "poi": "POI 图层"}
                for sort, (category, label) in enumerate(labels.items()):
                    db.execute("INSERT INTO scene_layer_folder VALUES (?,?,?,?,?,?,?,?)",
                               (self.category_folder_id(scene_id, category), scene_id, None,
                                label, category, sort, created_at, created_at))
        return row

    def get(self, scene_id: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT config_json FROM scene WHERE id=?", (scene_id,)).fetchone()
        return json.loads(row["config_json"]) if row else None

    def update(self, scene_id: str, name: str, remark: str | None, editor_user_id: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT config_json FROM scene WHERE id=?", (scene_id,)).fetchone()
            if row is None:
                return None
            duplicate = db.execute(
                "SELECT 1 FROM scene WHERE name=? COLLATE NOCASE AND id<>? LIMIT 1",
                (name, scene_id),
            ).fetchone()
            if duplicate is not None:
                raise ValueError("场景名称已存在")
            config = json.loads(row["config_json"])
            updated_at = _now()
            config.update({"Name": name, "Remark": remark, "EditById": editor_user_id, "EditTime": updated_at})
            db.execute(
                "UPDATE scene SET name=?,remark=?,updated_at=?,config_json=? WHERE id=?",
                (name, remark, updated_at, json.dumps(config, ensure_ascii=False, separators=(",", ":")), scene_id),
            )
        return config

    def delete(self, scene_id: str) -> bool:
        """Delete a locally managed scene and its scene-owned metadata.

        Referenced resources are not deleted: they may be shared by other scenes.
        Mirrored remote scenes are not present in this store and therefore cannot
        be removed through this method.
        """
        with self._db() as db:
            if db.execute("SELECT 1 FROM scene WHERE id=?", (scene_id,)).fetchone() is None:
                return False
            for table in (
                "legacy_scene_resource", "scene_poi_layer", "scene_video_layer",
                "scene_layer", "scene_layer_folder", "scene_tree_migration_backup",
            ):
                db.execute(f"DELETE FROM {table} WHERE scene_id=?", (scene_id,))
            return db.execute("DELETE FROM scene WHERE id=?", (scene_id,)).rowcount > 0

    def update_effects_config(self, scene_id: str, effects_config: dict, editor_user_id: str) -> bool:
        with self._db() as db:
            row = db.execute("SELECT config_json FROM scene WHERE id=?", (scene_id,)).fetchone()
            if row is None:
                return False
            config = json.loads(row["config_json"])
            updated_at = _now()
            config["EffectsConfig"] = json.dumps(effects_config, ensure_ascii=False, separators=(",", ":"))
            config["EditById"] = editor_user_id
            config["EditTime"] = updated_at
            db.execute("UPDATE scene SET config_json=?,updated_at=? WHERE id=?",
                (json.dumps(config, ensure_ascii=False, separators=(",", ":")), updated_at, scene_id))
        return True

    def rows(self) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT config_json FROM scene ORDER BY created_at DESC,id").fetchall()
        return [json.loads(row["config_json"]) for row in rows]

    def name_exists(self, name: str, exclude_id: str = "") -> bool:
        with self._db() as db:
            row = db.execute(
                "SELECT 1 FROM scene WHERE name=? COLLATE NOCASE AND id<>? LIMIT 1",
                (name, exclude_id),
            ).fetchone()
        return row is not None

    def resource_ids(self, scene_id: str) -> list[str]:
        table = "scene_resource" if self.get(scene_id) is not None else "legacy_scene_resource"
        with self._db() as db:
            rows = db.execute(
                f"SELECT resource_id FROM {table} WHERE scene_id=? ORDER BY sort,created_at,resource_id",
                (scene_id,),
            ).fetchall()
            layers = db.execute(
                "SELECT COALESCE(subset_resource_id,source_id) AS resource_id FROM scene_layer "
                "WHERE scene_id=? AND source_kind='legacy_resource' "
                "ORDER BY sort,created_at,id", (scene_id,),
            ).fetchall()
        return list(dict.fromkeys([row["resource_id"] for row in rows] + [row["resource_id"] for row in layers]))

    @staticmethod
    def resource_category(resource_type: int, big_type: int | None = None) -> str:
        categories = {1: "terrain", 2: "vector", 4: "imagery", 5: "model", 10: "video",
                      12: "raster", 13: "pointcloud", 14: "model", 21: "pointcloud"}
        try:
            group = int(big_type) if big_type is not None else resource_type // 1000000
        except (TypeError, ValueError):
            group = resource_type // 1000000
        return categories.get(group) or categories.get(resource_type // 1000000) or "model"

    @staticmethod
    def same_rectangle_scope(scope: dict | None, bbox: list[float]) -> bool:
        """Match a bbox polygon regardless of ring start and winding order."""
        if not isinstance(scope, dict) or scope.get("type") != "Polygon":
            return False
        try:
            rings = scope["coordinates"]
            ring = rings[0]
            if len(rings) != 1 or len(ring) != 5 or ring[0] != ring[-1]:
                return False
            west, south, east, north = bbox
            expected = {(west, south), (west, north), (east, south), (east, north)}
            return {(float(point[0]), float(point[1])) for point in ring[:-1]} == expected
        except (KeyError, TypeError, ValueError, IndexError):
            return False

    def migrate_legacy_tree(self, scene_id: str, resources: list[dict]) -> dict:
        """Move scene-owned legacy links into the instance model in one transaction.

        Folder UUIDs and POI UUIDs survive migration. Old resource links had no
        instance UUID, so their new IDs are deterministic per scene and source.
        Existing instance rows win when the same source is present in both models.
        """
        catalog = {str(item.get("Id")): item for item in resources}
        resource_table = "scene_resource" if self.get(scene_id) is not None else "legacy_scene_resource"
        counts = {"folders": 0, "resources": 0, "poi": 0, "video": 0}
        with self._db() as db:
            old_folders = db.execute(
                "SELECT * FROM scene_tree_folder WHERE scene_id=? ORDER BY sort,created_at,id", (scene_id,),
            ).fetchall()
            old_resources = db.execute(
                f"SELECT * FROM {resource_table} WHERE scene_id=? ORDER BY sort,created_at,resource_id", (scene_id,),
            ).fetchall()
            old_poi = db.execute("SELECT * FROM scene_poi_layer WHERE scene_id=?", (scene_id,)).fetchall()
            old_video = db.execute("SELECT * FROM scene_video_layer WHERE scene_id=?", (scene_id,)).fetchall()
            now = _now()
            for table, rows, key in (("scene_tree_folder", old_folders, "id"),
                                     (resource_table, old_resources, "resource_id"),
                                     ("scene_poi_layer", old_poi, "id"),
                                     ("scene_video_layer", old_video, "group_id")):
                for row in rows:
                    db.execute("INSERT OR IGNORE INTO scene_tree_migration_backup VALUES (?,?,?,?,?)",
                               (scene_id, table, row[key],
                                json.dumps(dict(row), ensure_ascii=False, separators=(",", ":")), now))
            for row in old_folders:
                cursor = db.execute(
                    "INSERT OR IGNORE INTO scene_layer_folder VALUES (?,?,?,?,?,?,?,?)",
                    (row["id"], scene_id, row["parent_id"], row["label"], "mixed",
                     row["sort"], row["created_at"], now),
                )
                counts["folders"] += cursor.rowcount

            def add_instance(layer_id: str, parent_id: str, category: str, source_kind: str,
                             source_id: str, label: str, sort: int, geometry: dict | None = None,
                             subset_id: str | None = None, legacy_payload: dict | None = None) -> bool:
                duplicate_sql = ("SELECT scope_geometry_json FROM scene_layer WHERE scene_id=? AND source_kind=? "
                                 "AND source_id=? AND subset_resource_id IS ?")
                candidates = db.execute(duplicate_sql, (scene_id, source_kind, source_id, subset_id)).fetchall()
                if source_kind == "spatial_dataset" and geometry:
                    ring = geometry["coordinates"][0]
                    bbox = [min(point[0] for point in ring), min(point[1] for point in ring),
                            max(point[0] for point in ring), max(point[1] for point in ring)]
                    duplicate = any(self.same_rectangle_scope(json.loads(row["scope_geometry_json"]), bbox)
                                    for row in candidates if row["scope_geometry_json"])
                else:
                    duplicate = bool(candidates)
                if duplicate:
                    return False
                cursor = db.execute(
                    """INSERT OR IGNORE INTO scene_layer
                    (id,scene_id,parent_folder_id,layer_category,source_kind,source_id,subset_resource_id,
                     scope_geometry_json,scope_crs,legacy_payload_json,display_name,default_visible,sort,request_id,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (layer_id, scene_id, parent_id, category, source_kind, source_id, subset_id,
                     json.dumps(geometry, separators=(",", ":")) if geometry else None,
                     "EPSG:4326" if geometry else None,
                     json.dumps(legacy_payload, ensure_ascii=False, separators=(",", ":")) if legacy_payload else None,
                     label, 0, sort, None, now, now),
                )
                return cursor.rowcount > 0

            for row in old_resources:
                source_id = row["resource_id"]
                source = catalog.get(source_id, {})
                try:
                    metadata = source.get("MetaData")
                    metadata = json.loads(metadata) if isinstance(metadata, str) else metadata
                    origin_id = metadata.get("ResourceSubset", {}).get("source_resource_id") if isinstance(metadata, dict) else None
                except (ValueError, TypeError, AttributeError):
                    origin_id = None
                subset_id = source_id if origin_id else None
                category = self.resource_category(int(row["resource_type"]), source.get("BigResourceType"))
                parent_id = row["custom_tree_id"] if any(folder["id"] == row["custom_tree_id"] for folder in old_folders) else self.category_folder_id(scene_id, category)
                if parent_id == self.category_folder_id(scene_id, category):
                    db.execute("INSERT OR IGNORE INTO scene_layer_folder VALUES (?,?,?,?,?,?,?,?)",
                               (parent_id, scene_id, None, category, category, 0, now, now))
                layer_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"deqing-scene:{scene_id}:resource:{source_id}"))
                counts["resources"] += add_instance(layer_id, parent_id, category, "legacy_resource", origin_id or source_id,
                    row["label"] or source.get("Label") or source_id, row["sort"], subset_id=subset_id,
                    legacy_payload={key: row[key] for key in ("metadata", "relate_resource_id", "extended_attributes", "osgb_id")})
            poi_parent = self.category_folder_id(scene_id, "poi")
            video_parent = self.category_folder_id(scene_id, "video")
            if old_poi:
                db.execute("INSERT OR IGNORE INTO scene_layer_folder VALUES (?,?,?,?,?,?,?,?)",
                           (poi_parent, scene_id, None, "区域 POI 图层", "poi", 0, now, now))
            if old_video:
                db.execute("INSERT OR IGNORE INTO scene_layer_folder VALUES (?,?,?,?,?,?,?,?)",
                           (video_parent, scene_id, None, "视频图层", "video", 0, now, now))
            for row in old_poi:
                west, south, east, north = json.loads(row["bbox_json"])
                geometry = {"type": "Polygon", "coordinates": [[[west, south], [east, south],
                             [east, north], [west, north], [west, south]]]}
                counts["poi"] += add_instance(row["id"], poi_parent, "poi", "spatial_dataset",
                                               row["dataset_id"], row["name"], row["sort"], geometry)
            for row in old_video:
                layer_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
                                          f"deqing-scene:{scene_id}:video-group:{row['group_id']}"))
                counts["video"] += add_instance(layer_id, video_parent, "video", "video_group",
                                                row["group_id"], row["group_id"], 0)
            # The legacy tables cease to be a second tree source after the
            # instance rows exist. A failed insert rolls back the whole block.
            db.execute(f"DELETE FROM {resource_table} WHERE scene_id=?", (scene_id,))
            db.execute("DELETE FROM scene_poi_layer WHERE scene_id=?", (scene_id,))
            db.execute("DELETE FROM scene_video_layer WHERE scene_id=?", (scene_id,))
            db.execute("DELETE FROM scene_tree_folder WHERE scene_id=?", (scene_id,))
        return counts

    def resource_rows(self, scene_id: str) -> list[dict]:
        table = "scene_resource" if self.get(scene_id) is not None else "legacy_scene_resource"
        with self._db() as db:
            rows = db.execute(
                f"SELECT * FROM {table} WHERE scene_id=? ORDER BY sort,created_at,resource_id",
                (scene_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def poi_layers(self, scene_id: str) -> list[dict]:
        with self._db() as db:
            rows = db.execute(
                "SELECT id,scene_id,dataset_id,bbox_json,name,sort FROM scene_poi_layer "
                "WHERE scene_id=? ORDER BY sort,created_at,id", (scene_id,),
            ).fetchall()
        return [{"id": row["id"], "scene_id": row["scene_id"],
                 "dataset_id": row["dataset_id"], "bbox": json.loads(row["bbox_json"]),
                 "name": row["name"], "sort": row["sort"]} for row in rows]

    def add_poi_layer(self, scene_id: str, dataset_id: str, bbox: list[float], name: str) -> dict:
        encoded = json.dumps(bbox, separators=(",", ":"))
        with self._db() as db:
            row = db.execute(
                "SELECT id FROM scene_poi_layer WHERE scene_id=? AND dataset_id=? AND bbox_json=?",
                (scene_id, dataset_id, encoded),
            ).fetchone()
            if row is None:
                layer_id = str(uuid.uuid4())
                sort = db.execute("SELECT COALESCE(MAX(sort),0)+1 FROM scene_poi_layer WHERE scene_id=?", (scene_id,)).fetchone()[0]
                db.execute("INSERT INTO scene_poi_layer VALUES (?,?,?,?,?,?,?)",
                           (layer_id, scene_id, dataset_id, encoded, name, sort, _now()))
            else:
                layer_id = row["id"]
        return next(item for item in self.poi_layers(scene_id) if item["id"] == layer_id)

    def remove_poi_layer(self, scene_id: str, layer_id: str) -> bool:
        with self._db() as db:
            result = db.execute("DELETE FROM scene_poi_layer WHERE scene_id=? AND id=?", (scene_id, layer_id))
            if result.rowcount:
                return True
            return db.execute("DELETE FROM scene_layer WHERE scene_id=? AND id=? AND source_kind='spatial_dataset' "
                              "AND layer_category='poi'", (scene_id, layer_id)).rowcount > 0

    def video_group_ids(self, scene_id: str) -> list[str]:
        with self._db() as db:
            rows = db.execute(
                "SELECT group_id FROM scene_video_layer WHERE scene_id=? ORDER BY created_at,group_id",
                (scene_id,),
            ).fetchall()
        return [row["group_id"] for row in rows]

    def add_video_group(self, scene_id: str, group_id: str) -> None:
        with self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO scene_video_layer(scene_id,group_id,created_at) VALUES (?,?,?)",
                (scene_id, group_id, _now()),
            )

    def remove_video_group(self, scene_id: str, group_id: str) -> bool:
        with self._db() as db:
            cursor = db.execute(
                "DELETE FROM scene_video_layer WHERE scene_id=? AND group_id=?",
                (scene_id, group_id),
            )
            if cursor.rowcount:
                return True
            return db.execute("DELETE FROM scene_layer WHERE scene_id=? AND source_id=? AND source_kind='video_group'",
                              (scene_id, group_id)).rowcount > 0

    def has_folder(self, scene_id: str, folder_id: str) -> bool:
        with self._db() as db:
            return db.execute(
                "SELECT 1 FROM scene_tree_folder WHERE scene_id=? AND id=?",
                (scene_id, folder_id),
            ).fetchone() is not None

    def folders(self, scene_id: str) -> list[dict]:
        with self._db() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT id,parent_id,label,sort FROM scene_tree_folder WHERE scene_id=? ORDER BY sort,created_at,id",
                (scene_id,),
            )]
        nodes = {}
        roots = []
        for row in rows:
            nodes[row["id"]] = {
                "id": row["id"], "label": row["label"], "type": "Group",
                "attribute": None, "isAdd": True, "isEdit": True, "isDelete": True,
                "iconUrl": "fold", "isSetResource": False, "sort": row["sort"],
                "children": [],
            }
        for row in rows:
            node = nodes[row["id"]]
            parent = nodes.get(row["parent_id"])
            (parent["children"] if parent else roots).append(node)
        return roots

    def create_folder(self, scene_id: str, label: str, parent_id: str | None) -> dict:
        folder_id = str(uuid.uuid4())
        with self._db() as db:
            if parent_id:
                exists = db.execute(
                    "SELECT 1 FROM scene_tree_folder WHERE scene_id=? AND id=?",
                    (scene_id, parent_id),
                ).fetchone()
                if exists is None:
                    raise ValueError("parent folder not found")
            sort = db.execute(
                "SELECT COALESCE(MAX(sort),0)+1 FROM scene_tree_folder WHERE scene_id=? AND parent_id IS ?",
                (scene_id, parent_id),
            ).fetchone()[0]
            db.execute(
                "INSERT INTO scene_tree_folder(id,scene_id,parent_id,label,sort,created_at) VALUES(?,?,?,?,?,?)",
                (folder_id, scene_id, parent_id, label, sort, _now()),
            )
            db.execute("UPDATE scene SET updated_at=? WHERE id=?", (_now(), scene_id))
        return {
            "id": folder_id, "label": label, "type": "Group", "attribute": None,
            "isAdd": True, "isEdit": True, "isDelete": True, "iconUrl": "fold",
            "isSetResource": False, "sort": sort, "children": [],
        }

    def edit_folder(self, scene_id: str, folder_id: str, label: str) -> bool:
        with self._db() as db:
            cursor = db.execute(
                "UPDATE scene_tree_folder SET label=? WHERE scene_id=? AND id=?",
                (label, scene_id, folder_id),
            )
            if cursor.rowcount:
                db.execute("UPDATE scene SET updated_at=? WHERE id=?", (_now(), scene_id))
            return cursor.rowcount > 0

    def delete_folder(self, scene_id: str, folder_id: str) -> bool:
        with self._db() as db:
            rows = db.execute(
                "SELECT id,parent_id FROM scene_tree_folder WHERE scene_id=?", (scene_id,),
            ).fetchall()
            children: dict[str | None, list[str]] = {}
            for row in rows:
                children.setdefault(row["parent_id"], []).append(row["id"])
            if not any(row["id"] == folder_id for row in rows):
                return False
            pending = [folder_id]
            removed_ids = []
            while pending:
                current = pending.pop()
                removed_ids.append(current)
                pending.extend(children.get(current, []))
            if not removed_ids:
                return False
            db.executemany("DELETE FROM scene_resource WHERE scene_id=? AND custom_tree_id=?",
                           [(scene_id, current) for current in removed_ids])
            db.executemany("DELETE FROM scene_tree_folder WHERE scene_id=? AND id=?",
                           [(scene_id, current) for current in removed_ids])
            db.execute("UPDATE scene SET updated_at=? WHERE id=?", (_now(), scene_id))
            return True

    def add_resources(self, scene_id: str, resources: list[dict]) -> None:
        local_scene = self.get(scene_id) is not None
        table = "scene_resource" if local_scene else "legacy_scene_resource"
        with self._db() as db:
            current = db.execute(
                f"SELECT COALESCE(MAX(sort),0) FROM {table} WHERE scene_id=?", (scene_id,),
            ).fetchone()[0]
            for item in resources:
                current += 1
                db.execute(
                    f"""INSERT INTO {table}
                       (scene_id,resource_id,resource_type,label,metadata,relate_resource_id,
                        extended_attributes,osgb_id,custom_tree_id,sort,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(scene_id,resource_id) DO UPDATE SET
                         resource_type=excluded.resource_type,label=excluded.label,metadata=excluded.metadata,
                         relate_resource_id=excluded.relate_resource_id,
                         extended_attributes=excluded.extended_attributes,osgb_id=excluded.osgb_id,
                         custom_tree_id=excluded.custom_tree_id""",
                    (scene_id, item["ResourceId"], int(item["Type"]), item.get("Label"),
                     item.get("MetaData"), item.get("RelateResourceId"),
                     item.get("ExtendedAttributes"), item.get("OsgbId"),
                     item.get("SceneTreesId"), current, _now()),
                )
            if local_scene:
                db.execute("UPDATE scene SET updated_at=? WHERE id=?", (_now(), scene_id))

    def remove_resource(self, scene_id: str, resource_id: str) -> bool:
        local_scene = self.get(scene_id) is not None
        table = "scene_resource" if local_scene else "legacy_scene_resource"
        with self._db() as db:
            cursor = db.execute(
                f"DELETE FROM {table} WHERE scene_id=? AND resource_id=?",
                (scene_id, resource_id),
            )
            if not cursor.rowcount:
                cursor = db.execute("DELETE FROM scene_layer WHERE scene_id=? AND source_kind='legacy_resource' "
                                    "AND COALESCE(subset_resource_id,source_id)=?", (scene_id, resource_id))
            if cursor.rowcount and local_scene:
                db.execute("UPDATE scene SET updated_at=? WHERE id=?", (_now(), scene_id))
            return cursor.rowcount > 0

    def remove_resource_everywhere(self, resource_id: str, *, dataset: bool = False,
                                   video_group: bool = False) -> int:
        """Remove stale scene references after an administrator deletes a resource."""
        removed = 0
        with self._db() as db:
            scene_ids = {row[0] for row in db.execute(
                "SELECT scene_id FROM scene_resource WHERE resource_id=? UNION "
                "SELECT scene_id FROM scene_layer WHERE source_id=? OR subset_resource_id=?",
                (resource_id, resource_id, resource_id),
            ).fetchall()}
            if dataset:
                scene_ids.update(row[0] for row in db.execute(
                    "SELECT scene_id FROM scene_poi_layer WHERE dataset_id=?", (resource_id,),
                ).fetchall())
            if video_group:
                scene_ids.update(row[0] for row in db.execute(
                    "SELECT scene_id FROM scene_video_layer WHERE group_id=?", (resource_id,),
                ).fetchall())
            for table in ("scene_resource", "legacy_scene_resource"):
                cursor = db.execute(f"DELETE FROM {table} WHERE resource_id=?", (resource_id,))
                removed += cursor.rowcount
            cursor = db.execute(
                "DELETE FROM scene_layer WHERE source_id=? OR subset_resource_id=?",
                (resource_id, resource_id),
            )
            removed += cursor.rowcount
            if dataset:
                cursor = db.execute("DELETE FROM scene_poi_layer WHERE dataset_id=?", (resource_id,))
                removed += cursor.rowcount
            if video_group:
                cursor = db.execute("DELETE FROM scene_video_layer WHERE group_id=?", (resource_id,))
                removed += cursor.rowcount
            if scene_ids:
                db.executemany("UPDATE scene SET updated_at=? WHERE id=?",
                               [(_now(), scene_id) for scene_id in scene_ids])
        return removed

    @staticmethod
    def category_folder_id(scene_id: str, category: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"deqing-scene:{scene_id}:category:{category}"))

    def layer_folders(self, scene_id: str) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM scene_layer_folder WHERE scene_id=? ORDER BY sort,created_at,id", (scene_id,)).fetchall()
        return [dict(row) for row in rows]

    def layers(self, scene_id: str) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM scene_layer WHERE scene_id=? ORDER BY sort,created_at,id", (scene_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["layer_id"] = item.pop("id")
            item["scope_geometry"] = json.loads(item.pop("scope_geometry_json")) if item["scope_geometry_json"] else None
            item["filter_properties"] = json.loads(item.pop("filter_properties_json")) if item["filter_properties_json"] else None
            item["legacy_payload"] = json.loads(item.pop("legacy_payload_json")) if item["legacy_payload_json"] else None
            item["default_visible"] = bool(item["default_visible"])
            item["render_descriptor"] = self.render_descriptor(item)
            result.append(item)
        return result

    @staticmethod
    def render_descriptor(layer: dict) -> dict:
        if layer.get("subset_resource_id"):
            return {"type":"legacy_subset","resource_id":layer["subset_resource_id"],
                    "entry_path":None,"deprecated":True}
        source_id = layer["source_id"]
        version = layer.get("source_version_id")
        layer_id = layer["layer_id"]
        kind = layer["source_kind"]
        if kind in {"pointcloud_resource", "legacy_resource"} and version and layer.get("scope_geometry"):
            return {"type": "3dtiles", "entry_path":
                    f"/spatial/resources/{source_id}/versions/{version}/tileset.json?scene_layer_id={layer_id}",
                    "auth": "bearer_header", "precision_notice": "按瓦片包围体相交筛选；边界瓦片仍可能含范围外内容。"}
        if kind == "raster_collection" and version and layer.get("scope_geometry"):
            return {"type": "raster_tms", "tile_url_template":
                    f"/spatial/resources/{source_id}/versions/{version}/tiles/{{z}}/{{x}}/{{y}}?scene_layer_id={layer_id}",
                    "auth": "bearer_header"}
        if layer.get("scope_geometry"):
            return {"type": kind, "query_path": "/spatial/resources/query",
                    "selection_mode": layer.get("selection_mode"), "auth": "bearer_header"}
        return {"type": kind, "entry_path": None}

    def find_layer(self, layer_id: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT scene_id FROM scene_layer WHERE id=?", (layer_id,)).fetchone()
        if row is None:
            return None
        return next((item for item in self.layers(row["scene_id"]) if item["layer_id"] == layer_id), None)

    def pin_existing_versions(self, spatial_store) -> dict:
        """Preserve old layer IDs while pinning resolvable published source versions."""
        from .resource_query import _version
        changed = skipped = 0
        with self._db() as db:
            rows = db.execute("""SELECT id,source_kind,source_id,scope_geometry_json
                FROM scene_layer WHERE source_version_id IS NULL AND source_kind IN
                ('pointcloud_version','pointcloud_resource','raster_collection',
                 'spatial_dataset','oblique_resource','video_group')""").fetchall()
        for row in rows:
            try:
                kind, source_id, version = _version(spatial_store,row["source_kind"],row["source_id"],None)
            except (LookupError,ValueError):
                skipped += 1
                continue
            mode = None
            if row["scope_geometry_json"]:
                mode = {"pointcloud_resource":"intersecting_tiles",
                        "raster_collection":"intersecting_tiles",
                        "spatial_dataset":"feature_intersects",
                        "oblique_resource":"candidate_footprint_intersects",
                        "video_group":"footprint_intersects"}.get(kind)
            with self._db() as db:
                changed += db.execute("""UPDATE scene_layer SET source_kind=?,source_id=?,
                    source_version_id=?,selection_mode=?,updated_at=?
                    WHERE id=? AND source_version_id IS NULL""",
                    (kind,source_id,version,mode,_now(),row["id"])).rowcount
        return {"pinned":changed,"unresolved":skipped}

    def create_layer_folder(self, scene_id: str, label: str, category: str,
                            parent_id: str | None = None, sort: int | None = None) -> dict:
        folder_id = str(uuid.uuid4())
        with self._db() as db:
            if parent_id and db.execute("SELECT 1 FROM scene_layer_folder WHERE scene_id=? AND id=?", (scene_id, parent_id)).fetchone() is None:
                raise ValueError("父目录不存在")
            if sort is None:
                sort = db.execute("SELECT COALESCE(MAX(sort),0)+1 FROM scene_layer_folder WHERE scene_id=? AND parent_id IS ?", (scene_id, parent_id)).fetchone()[0]
            now = _now()
            db.execute("INSERT INTO scene_layer_folder VALUES (?,?,?,?,?,?,?,?)",
                       (folder_id, scene_id, parent_id, label, category, sort, now, now))
        return next(row for row in self.layer_folders(scene_id) if row["id"] == folder_id)

    def update_layer_folder(self, scene_id: str, folder_id: str, changes: dict) -> bool:
        with self._db() as db:
            current = db.execute("SELECT * FROM scene_layer_folder WHERE scene_id=? AND id=?", (scene_id, folder_id)).fetchone()
            if current is None:
                return False
            parent_id = changes.get("parent_id", current["parent_id"])
            if parent_id == folder_id:
                raise ValueError("目录不能作为自己的父目录")
            if parent_id:
                parent = db.execute("SELECT id,parent_id FROM scene_layer_folder WHERE scene_id=? AND id=?", (scene_id, parent_id)).fetchone()
                if parent is None:
                    raise ValueError("父目录不存在")
                pending = parent
                while pending:
                    if pending["id"] == folder_id:
                        raise ValueError("目录不能移入自己的子目录")
                    pending = db.execute("SELECT id,parent_id FROM scene_layer_folder WHERE scene_id=? AND id=?", (scene_id, pending["parent_id"])).fetchone() if pending["parent_id"] else None
            db.execute("UPDATE scene_layer_folder SET parent_id=?,label=?,sort=?,updated_at=? WHERE scene_id=? AND id=?",
                       (parent_id, changes.get("label", current["label"]), changes.get("sort", current["sort"]), _now(), scene_id, folder_id))
        return True

    def delete_layer_folder(self, scene_id: str, folder_id: str) -> bool:
        with self._db() as db:
            if db.execute("SELECT 1 FROM scene_layer_folder WHERE scene_id=? AND parent_id=?", (scene_id, folder_id)).fetchone() or db.execute("SELECT 1 FROM scene_layer WHERE scene_id=? AND parent_folder_id=?", (scene_id, folder_id)).fetchone():
                raise ValueError("目录非空，请先移除或转移子目录和图层")
            return db.execute("DELETE FROM scene_layer_folder WHERE scene_id=? AND id=?", (scene_id, folder_id)).rowcount > 0

    def add_layer(self, item: dict) -> tuple[dict, bool]:
        scene_id = item["scene_id"]
        category = item["layer_category"]
        parent_id = item.get("parent_folder_id") or self.category_folder_id(scene_id, category)
        with self._db() as db:
            request_id = item.get("request_id")
            if request_id:
                existing = db.execute("SELECT id FROM scene_layer WHERE scene_id=? AND request_id=?", (scene_id, request_id)).fetchone()
                if existing:
                    return next(row for row in self.layers(scene_id) if row["layer_id"] == existing["id"]), True
            if parent_id == self.category_folder_id(scene_id, category):
                now = _now()
                db.execute("INSERT OR IGNORE INTO scene_layer_folder VALUES (?,?,?,?,?,?,?,?)",
                           (parent_id, scene_id, None, item.get("category_label") or category, category, 0, now, now))
            parent = db.execute("SELECT layer_category FROM scene_layer_folder WHERE scene_id=? AND id=?", (scene_id, parent_id)).fetchone()
            if parent is None or parent["layer_category"] not in {category, "mixed"}:
                raise ValueError("目标目录不存在或类型不匹配")
            sort = item.get("sort")
            if sort is None:
                sort = db.execute("SELECT COALESCE(MAX(sort),0)+1 FROM scene_layer WHERE scene_id=? AND parent_folder_id=?", (scene_id, parent_id)).fetchone()[0]
            layer_id = str(uuid.uuid4())
            now = _now()
            cursor = db.execute("""INSERT OR IGNORE INTO scene_layer
                (id,scene_id,parent_folder_id,layer_category,source_kind,source_id,subset_resource_id,
                 scope_geometry_json,scope_crs,source_version_id,selection_mode,filter_properties_json,
                 display_name,default_visible,sort,request_id,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (layer_id, scene_id, parent_id, category, item["source_kind"], item["source_id"],
                 item.get("subset_resource_id"), json.dumps(item["scope_geometry"], ensure_ascii=False, separators=(",", ":")) if item.get("scope_geometry") else None,
                 item.get("scope_crs"), item.get("source_version_id"),item.get("selection_mode"),
                 json.dumps(item["filter_properties"], ensure_ascii=False, separators=(",", ":")) if item.get("filter_properties") else None,
                 item["display_name"], int(item.get("default_visible", False)),
                 sort, request_id, now, now))
            if cursor.rowcount == 0:
                if not request_id:
                    raise sqlite3.IntegrityError("场景图层约束冲突")
                existing = db.execute("SELECT id FROM scene_layer WHERE scene_id=? AND request_id=?", (scene_id, request_id)).fetchone()
                if existing is None:
                    raise sqlite3.IntegrityError("场景图层请求冲突")
                return next(row for row in self.layers(scene_id) if row["layer_id"] == existing["id"]), True
        return next(row for row in self.layers(scene_id) if row["layer_id"] == layer_id), False

    def update_layer(self, scene_id: str, layer_id: str, changes: dict) -> bool:
        with self._db() as db:
            current = db.execute("SELECT * FROM scene_layer WHERE scene_id=? AND id=?", (scene_id, layer_id)).fetchone()
            if current is None:
                return False
            parent_id = changes.get("parent_folder_id", current["parent_folder_id"])
            parent = db.execute("SELECT layer_category FROM scene_layer_folder WHERE scene_id=? AND id=?", (scene_id, parent_id)).fetchone()
            if parent is None or parent["layer_category"] not in {current["layer_category"], "mixed"}:
                raise ValueError("目标目录不存在或类型不匹配")
            db.execute("""UPDATE scene_layer SET parent_folder_id=?,display_name=?,default_visible=?,sort=?,filter_properties_json=?,
                          scope_geometry_json=?,scope_crs=?,source_version_id=?,selection_mode=?,updated_at=?
                          WHERE scene_id=? AND id=?""",
                       (parent_id, changes.get("display_name", current["display_name"]),
                        int(changes.get("default_visible", current["default_visible"])),
                        changes.get("sort", current["sort"]),
                        (json.dumps(changes["filter_properties"], ensure_ascii=False, separators=(",", ":"))
                         if changes["filter_properties"] else None) if "filter_properties" in changes else current["filter_properties_json"],
                        (json.dumps(changes["scope_geometry"], ensure_ascii=False, separators=(",", ":"))
                         if changes["scope_geometry"] else None) if "scope_geometry" in changes else current["scope_geometry_json"],
                        ("EPSG:4326" if changes["scope_geometry"] else None) if "scope_geometry" in changes else current["scope_crs"],
                        changes.get("source_version_id", current["source_version_id"]),
                        changes.get("selection_mode", current["selection_mode"]),
                        _now(), scene_id, layer_id))
        return True

    def delete_layer(self, scene_id: str, layer_id: str) -> bool:
        with self._db() as db:
            return db.execute("DELETE FROM scene_layer WHERE scene_id=? AND id=?", (scene_id, layer_id)).rowcount > 0

    def apply_tree_order(self, scene_id: str, tree: list[dict]) -> dict:
        """Persist the order of known nodes in a legacy drag-and-drop tree."""
        local_scene = self.get(scene_id) is not None
        resource_table = "scene_resource" if local_scene else "legacy_scene_resource"
        with self._db() as db:
            new_folders = {row["id"]: row for row in db.execute(
                "SELECT id,layer_category FROM scene_layer_folder WHERE scene_id=?", (scene_id,))}
            new_layers = {row["id"]: row for row in db.execute(
                "SELECT id,layer_category FROM scene_layer WHERE scene_id=?", (scene_id,))}
            old_folders = {row["id"] for row in db.execute(
                "SELECT id FROM scene_tree_folder WHERE scene_id=?", (scene_id,))}
            old_resources = {row["resource_id"] for row in db.execute(
                f"SELECT resource_id FROM {resource_table} WHERE scene_id=?", (scene_id,))}
            seen = set()
            operations = []

            def visit(items: list, parent_id: str | None = None):
                for position, node in enumerate(items):
                    if not isinstance(node, dict):
                        continue
                    node_id = str(node.get("id") or "")
                    if node_id in seen:
                        raise ValueError("资源树包含重复节点 ID")
                    seen.add(node_id)
                    if node_id in new_folders:
                        if parent_id and parent_id in new_folders:
                            if new_folders[parent_id]["layer_category"] not in {new_folders[node_id]["layer_category"], "mixed"}:
                                raise ValueError("目录类别不匹配")
                            operations.append(("folder", node_id, parent_id, position))
                        elif parent_id in old_folders or parent_id in new_layers:
                            raise ValueError("目录不能移入该节点")
                        else:
                            operations.append(("folder", node_id, None, position))
                    elif node_id in new_layers:
                        if parent_id not in new_folders or new_folders[parent_id]["layer_category"] not in {new_layers[node_id]["layer_category"], "mixed"}:
                            raise ValueError("图层目标目录不存在或类别不匹配")
                        operations.append(("layer", node_id, parent_id, position))
                    elif node_id in old_folders:
                        if parent_id and parent_id not in old_folders:
                            raise ValueError("旧目录只能在旧目录内排序")
                        operations.append(("old_folder", node_id, parent_id, position))
                    elif node_id in old_resources and parent_id in old_folders:
                        operations.append(("old_resource", node_id, parent_id, position))
                    visit(node.get("children") or [], node_id)

            visit(tree)
            for kind, node_id, parent_id, position in operations:
                if kind == "folder":
                    db.execute("UPDATE scene_layer_folder SET parent_id=?,sort=?,updated_at=? WHERE scene_id=? AND id=?",
                               (parent_id, position, _now(), scene_id, node_id))
                elif kind == "layer":
                    db.execute("UPDATE scene_layer SET parent_folder_id=?,sort=?,updated_at=? WHERE scene_id=? AND id=?",
                               (parent_id, position, _now(), scene_id, node_id))
                elif kind == "old_folder":
                    db.execute("UPDATE scene_tree_folder SET parent_id=?,sort=? WHERE scene_id=? AND id=?",
                               (parent_id, position, scene_id, node_id))
                else:
                    db.execute(f"UPDATE {resource_table} SET custom_tree_id=?,sort=? WHERE scene_id=? AND resource_id=?",
                               (parent_id, position, scene_id, node_id))
        return {"updated": len(operations)}
