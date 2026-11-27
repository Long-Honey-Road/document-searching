"""Add missing bundled demo documents to a writable, persistent DOCS_DIR."""

import argparse
import os
import shutil
from pathlib import Path
from tempfile import NamedTemporaryFile

import yaml


def _read(path: Path) -> dict:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("documents"), list):
        raise ValueError(f"Некорректный манифест: {path}")
    return value


def sync_corpus(source: Path, target: Path) -> int:
    """Append missing documents; existing entries and files belong to the host."""
    bundled = _read(source / "manifest.yaml")
    target.mkdir(parents=True, exist_ok=True)
    manifest_path = target / "manifest.yaml"
    current = _read(manifest_path) if manifest_path.exists() else {"areas": [], "documents": []}
    existing = {entry["doc_id"] for entry in current["documents"]}
    additions = [entry for entry in bundled["documents"] if entry["doc_id"] not in existing]
    if not additions:
        return 0

    for entry in additions:
        filename = entry["file"]
        if Path(filename).name != filename or filename in {".", ".."}:
            raise ValueError(f"Недопустимое имя файла: {filename}")
        original = source / filename
        destination = target / filename
        if not original.is_file():
            raise FileNotFoundError(original)
        if destination.exists() and destination.read_bytes() != original.read_bytes():
            raise ValueError(f"Файл на стенде отличается от файла в репозитории: {filename}")

    for entry in additions:
        original = source / entry["file"]
        destination = target / entry["file"]
        if destination.exists():
            continue
        with NamedTemporaryFile(dir=target, delete=False) as staged:
            staged_path = Path(staged.name)
            with original.open("rb") as stream:
                shutil.copyfileobj(stream, staged)
        try:
            os.replace(staged_path, destination)
        finally:
            staged_path.unlink(missing_ok=True)

    areas = {area["code"] for area in current.get("areas", [])}
    current.setdefault("areas", []).extend(
        area for area in bundled.get("areas", []) if area["code"] not in areas
    )
    current["documents"].extend(additions)
    with NamedTemporaryFile(dir=target, mode="w", encoding="utf-8", delete=False) as staged:
        staged_path = Path(staged.name)
        yaml.safe_dump(current, staged, allow_unicode=True, sort_keys=False)
    try:
        os.replace(staged_path, manifest_path)
    finally:
        staged_path.unlink(missing_ok=True)
    return len(additions)


def main() -> None:
    repository = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=repository / "data/demo_corpus")
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    print(f"Добавлено документов: {sync_corpus(args.source, args.target)}")


if __name__ == "__main__":
    main()
