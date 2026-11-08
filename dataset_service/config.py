import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    sources: dict[str, Path]
    expected_images: int = 185

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.getenv("DEQING_DATA_DIR", "/workspace/data")).resolve()
        raw = os.getenv("DEQING_SOURCE_ALIASES", '{"garden":"/imports/garden"}')
        aliases = json.loads(raw)
        if not isinstance(aliases, dict) or not aliases:
            raise ValueError("DEQING_SOURCE_ALIASES must be a nonempty JSON object")
        sources: dict[str, Path] = {}
        for alias, path in aliases.items():
            if not isinstance(alias, str) or not alias.isidentifier() or not isinstance(path, str):
                raise ValueError("invalid source alias configuration")
            candidate = Path(path)
            if not candidate.is_absolute() or ".." in candidate.parts:
                raise ValueError("source paths must be absolute and cannot contain '..'")
            sources[alias] = candidate
        return cls(data_dir, sources, int(os.getenv("DEQING_EXPECTED_IMAGES", "185")))
