import argparse
import hashlib
import json
import os
import secrets
from pathlib import Path


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def ensure_credentials(data_dir: Path) -> dict[str, str]:
    path = data_dir / "credentials.json"
    data_dir.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        values = {role: secrets.token_urlsafe(48) for role in ("admin", "reader")}
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(values, stream)
            stream.write("\n")
    if path.stat().st_mode & 0o077:
        raise RuntimeError("credentials.json must be readable only by its owner")
    values = json.loads(path.read_text())
    if set(values) != {"admin", "reader"} or any(not isinstance(v, str) or len(v) < 32 for v in values.values()):
        raise RuntimeError("invalid credentials.json")
    return values


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or read local service credentials")
    parser.add_argument("--data-dir", type=Path, default=Path(os.getenv("DEQING_DATA_DIR", "/workspace/data")))
    args = parser.parse_args()
    values = ensure_credentials(args.data_dir)
    print(json.dumps(values, indent=2))


if __name__ == "__main__":
    main()
