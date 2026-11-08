"""Read the fields needed to validate a COLMAP sparse model."""
import math
import struct
from pathlib import Path


MODEL_PARAMS = {0: 3, 1: 4, 2: 4, 3: 5, 4: 8, 5: 8, 6: 12, 7: 5, 8: 4, 9: 5, 10: 12, 11: 4}
MODEL_NAMES = {
    "SIMPLE_PINHOLE": 0, "PINHOLE": 1, "SIMPLE_RADIAL": 2, "RADIAL": 3,
    "OPENCV": 4, "OPENCV_FISHEYE": 5, "FULL_OPENCV": 6, "FOV": 7,
    "SIMPLE_RADIAL_FISHEYE": 8, "RADIAL_FISHEYE": 9,
    "THIN_PRISM_FISHEYE": 10, "RAD_TAN_THIN_PRISM_FISHEYE": 11,
}


def _unpack(stream, fmt: str):
    size = struct.calcsize("<" + fmt)
    data = stream.read(size)
    if len(data) != size:
        raise ValueError("truncated COLMAP binary model")
    return struct.unpack("<" + fmt, data)


def _cstring(stream) -> str:
    data = bytearray()
    while True:
        char = stream.read(1)
        if not char:
            raise ValueError("unterminated image name in binary model")
        if char == b"\0":
            return data.decode("utf-8")
        data.extend(char)
        if len(data) > 4096:
            raise ValueError("image name too long")


def read_binary(root: Path):
    cameras, images, points = {}, {}, {}
    with (root / "cameras.bin").open("rb") as stream:
        for _ in range(_unpack(stream, "Q")[0]):
            camera_id, model, width, height = _unpack(stream, "iiQQ")
            if model not in MODEL_PARAMS or camera_id in cameras:
                raise ValueError("unknown camera model or duplicate camera ID")
            params = _unpack(stream, "d" * MODEL_PARAMS[model])
            cameras[camera_id] = (model, width, height, params)
        if stream.read(1):
            raise ValueError("trailing bytes in cameras.bin")
    with (root / "images.bin").open("rb") as stream:
        for _ in range(_unpack(stream, "Q")[0]):
            image_id = _unpack(stream, "i")[0]
            pose = _unpack(stream, "7d")
            camera_id = _unpack(stream, "i")[0]
            name = _cstring(stream)
            count = _unpack(stream, "Q")[0]
            observations = tuple(_unpack(stream, "ddq") for _ in range(count))
            if image_id in images:
                raise ValueError("duplicate image ID")
            images[image_id] = (pose, camera_id, name, observations)
        if stream.read(1):
            raise ValueError("trailing bytes in images.bin")
    with (root / "points3D.bin").open("rb") as stream:
        for _ in range(_unpack(stream, "Q")[0]):
            point_id = _unpack(stream, "Q")[0]
            xyz = _unpack(stream, "3d")
            rgb = _unpack(stream, "3B")
            error = _unpack(stream, "d")[0]
            count = _unpack(stream, "Q")[0]
            track = tuple(_unpack(stream, "ii") for _ in range(count))
            if point_id in points:
                raise ValueError("duplicate point ID")
            points[point_id] = (xyz, rgb, error, track)
        if stream.read(1):
            raise ValueError("trailing bytes in points3D.bin")
    return cameras, images, points


def _lines(path: Path):
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if line and not line.startswith("#"):
                yield line


def read_text(root: Path):
    cameras, images, points = {}, {}, {}
    for line in _lines(root / "cameras.txt"):
        fields = line.split()
        camera_id, model, width, height = int(fields[0]), MODEL_NAMES[fields[1]], int(fields[2]), int(fields[3])
        params = tuple(map(float, fields[4:]))
        if len(params) != MODEL_PARAMS[model] or camera_id in cameras:
            raise ValueError("invalid or duplicate text camera")
        cameras[camera_id] = (model, width, height, params)
    with (root / "images.txt").open("r", encoding="utf-8") as stream:
        iterator = iter(stream)
        for line in iterator:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            fields = line.split(maxsplit=9)
            if len(fields) != 10:
                raise ValueError("invalid images.txt record")
            image_id = int(fields[0])
            pose = tuple(map(float, fields[1:8]))
            camera_id, name = int(fields[8]), fields[9]
            observation_line = next(iterator, None)
            if observation_line is None:
                raise ValueError("missing images.txt observations")
            values = observation_line.split()
            if len(values) % 3:
                raise ValueError("invalid images.txt observations")
            observations = tuple((float(values[i]), float(values[i + 1]), int(values[i + 2])) for i in range(0, len(values), 3))
            if image_id in images:
                raise ValueError("duplicate image ID")
            images[image_id] = (pose, camera_id, name, observations)
    for line in _lines(root / "points3D.txt"):
        fields = line.split()
        if len(fields) < 8 or (len(fields) - 8) % 2:
            raise ValueError("invalid points3D.txt record")
        point_id = int(fields[0])
        xyz = tuple(map(float, fields[1:4]))
        rgb = tuple(map(int, fields[4:7]))
        error = float(fields[7])
        track = tuple((int(fields[i]), int(fields[i + 1])) for i in range(8, len(fields), 2))
        if point_id in points:
            raise ValueError("duplicate point ID")
        points[point_id] = (xyz, rgb, error, track)
    return cameras, images, points


def _same(a, b) -> bool:
    if type(a) is not type(b):
        return False
    if isinstance(a, float):
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, tuple):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


def validate_model(root: Path, image_names: set[str]) -> dict:
    binary = read_binary(root)
    text = read_text(root)
    if not _same(binary, text):
        raise ValueError("COLMAP text and binary models differ")
    cameras, images, points = binary
    if not cameras or not images or not points:
        raise ValueError("COLMAP model must contain cameras, images and 3D points")
    registered = [image[2] for image in images.values()]
    if len(set(registered)) != len(registered):
        raise ValueError("duplicate registered image name")
    missing = set(registered) - image_names
    if missing:
        raise ValueError(f"registered image missing: {sorted(missing)[0]}")
    if any(image[1] not in cameras for image in images.values()):
        raise ValueError("image references missing camera")
    for image_id, image in images.items():
        for _, _, point_id in image[3]:
            if point_id != -1 and point_id not in points:
                raise ValueError(f"image {image_id} references missing point {point_id}")
    for point_id, (_, _, _, track) in points.items():
        for image_id, observation_index in track:
            if image_id not in images or observation_index < 0 or observation_index >= len(images[image_id][3]):
                raise ValueError(f"invalid track for point {point_id}")
            if images[image_id][3][observation_index][2] != point_id:
                raise ValueError(f"inconsistent track for point {point_id}")
    return {"cameras": len(cameras), "registered_images": len(images), "points3D": len(points)}
