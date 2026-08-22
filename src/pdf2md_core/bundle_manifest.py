from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

BUNDLE_MANIFEST_VERSION = "0.2.0"
BUNDLE_MANIFEST_NAME = "bundle_manifest.json"
BUNDLE_ARCHIVE_NAME = "bundle.zip"
BUNDLE_CAPACITY_POLICY_ID = "markovo.bundle-capacity.v1"
MAX_PUBLIC_BUNDLE_BYTES = 256 * 1024 * 1024
MAX_PUBLIC_BUNDLE_FILE_BYTES = 16 * 1024 * 1024
MAX_PUBLIC_BUNDLE_FILES = 2048
MAX_PUBLIC_BUNDLE_EXPANSION_RATIO = 32
MIN_PUBLIC_BUNDLE_EXPANSION_ALLOWANCE_BYTES = 16 * 1024 * 1024


class BundleLimitExceededError(ValueError):
    """A public Bundle exceeded a stable, non-retryable capacity boundary."""

    error_code = "output_bundle_limit_exceeded"

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason

_CONTENT_TYPES = {
    ".aac": "audio/aac",
    ".avi": "video/x-msvideo",
    ".bmp": "image/bmp",
    ".csv": "text/csv",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".epub": "application/epub+zip",
    ".flac": "audio/flac",
    ".gif": "image/gif",
    ".html": "text/html",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".json": "application/json",
    ".m4a": "audio/mp4",
    ".m4v": "video/x-m4v",
    ".md": "text/markdown",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".svg": "image/svg+xml",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".txt": "text/plain",
    ".wav": "audio/wav",
    ".webm": "video/webm",
    ".webp": "image/webp",
    ".wma": "audio/x-ms-wma",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xml": "application/xml",
    ".zip": "application/zip",
}


def build_bundle_manifest(
    bundle_dir: str | Path,
    *,
    source_bytes: int | None = None,
) -> dict[str, Any]:
    bundle = Path(bundle_dir)
    files = []
    for item in sorted(bundle.rglob("*")):
        if not item.is_file() or item.name in {BUNDLE_MANIFEST_NAME, BUNDLE_ARCHIVE_NAME}:
            continue
        files.append(
            {
                "path": item.relative_to(bundle).as_posix(),
                "bytes": item.stat().st_size,
                "sha256": _sha256(item),
                "content_type": _content_type(item),
            }
        )
    manifest = {
        "schema_version": "markovo.bundle-manifest.v1",
        "version": BUNDLE_MANIFEST_VERSION,
        "files_total": len(files),
        "bytes_total": sum(int(item["bytes"]) for item in files),
        "files": files,
    }
    manifest["capacity"] = validate_bundle_capacity(
        manifest,
        source_bytes=source_bytes,
    )
    return manifest


def write_bundle_manifest(
    bundle_dir: str | Path,
    *,
    source_bytes: int | None = None,
) -> dict[str, Any]:
    bundle = Path(bundle_dir)
    if source_bytes is None:
        source_bytes = _existing_source_bytes(bundle / BUNDLE_MANIFEST_NAME)
    manifest = build_bundle_manifest(bundle, source_bytes=source_bytes)
    (bundle / BUNDLE_MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


def validate_bundle_capacity(
    manifest: dict[str, Any],
    *,
    source_bytes: int | None = None,
) -> dict[str, int | str | None]:
    """Validate the public Bundle before billing, upload, or publication."""

    files = manifest.get("files")
    if not isinstance(files, list) or any(not isinstance(item, dict) for item in files):
        raise ValueError("Bundle manifest files must be an array of objects.")
    files_total = _safe_non_negative_integer(manifest.get("files_total"), "files_total")
    bytes_total = _safe_non_negative_integer(manifest.get("bytes_total"), "bytes_total")
    if files_total != len(files):
        raise ValueError("Bundle manifest files_total does not match its file inventory.")
    sizes = [
        _safe_non_negative_integer(item.get("bytes"), f"files[{index}].bytes")
        for index, item in enumerate(files)
    ]
    if bytes_total != sum(sizes):
        raise ValueError("Bundle manifest bytes_total does not match its file inventory.")
    if source_bytes is not None:
        source_bytes = _safe_non_negative_integer(source_bytes, "source_bytes")

    if files_total > MAX_PUBLIC_BUNDLE_FILES:
        raise BundleLimitExceededError(
            "files_total",
            f"Public Bundle contains {files_total} files; the limit is {MAX_PUBLIC_BUNDLE_FILES}.",
        )
    max_file_bytes = max(sizes, default=0)
    if max_file_bytes > MAX_PUBLIC_BUNDLE_FILE_BYTES:
        raise BundleLimitExceededError(
            "file_bytes",
            f"Public Bundle contains a {max_file_bytes}-byte file; the per-file limit is {MAX_PUBLIC_BUNDLE_FILE_BYTES} bytes.",
        )
    if bytes_total > MAX_PUBLIC_BUNDLE_BYTES:
        raise BundleLimitExceededError(
            "total_bytes",
            f"Public Bundle is {bytes_total} bytes; the total limit is {MAX_PUBLIC_BUNDLE_BYTES} bytes.",
        )

    expansion_allowance_bytes = (
        max(
            MIN_PUBLIC_BUNDLE_EXPANSION_ALLOWANCE_BYTES,
            source_bytes * MAX_PUBLIC_BUNDLE_EXPANSION_RATIO,
        )
        if source_bytes is not None
        else None
    )
    if expansion_allowance_bytes is not None and bytes_total > expansion_allowance_bytes:
        raise BundleLimitExceededError(
            "expansion_ratio",
            f"Public Bundle is {bytes_total} bytes; the source-bound allowance is {expansion_allowance_bytes} bytes.",
        )
    return {
        "policy_id": BUNDLE_CAPACITY_POLICY_ID,
        "source_bytes": source_bytes,
        "files_total": files_total,
        "bytes_total": bytes_total,
        "max_file_bytes": max_file_bytes,
        "expansion_allowance_bytes": expansion_allowance_bytes,
    }


def validate_bundle_archive_capacity(archive_path: str | Path) -> int:
    archive_bytes = Path(archive_path).stat().st_size
    if archive_bytes > MAX_PUBLIC_BUNDLE_BYTES:
        raise BundleLimitExceededError(
            "archive_bytes",
            f"Public Bundle archive is {archive_bytes} bytes; the archive limit is {MAX_PUBLIC_BUNDLE_BYTES} bytes.",
        )
    return archive_bytes


def verify_bundle_manifest(bundle_path: str | Path) -> dict[str, Any]:
    path = Path(bundle_path)
    if path.is_dir():
        return _verify_directory(path)
    if path.is_file() and path.suffix.lower() == ".zip":
        return _verify_zip(path)
    return {
        "status": "failed",
        "target": str(path),
        "files_checked": 0,
        "errors": [f"Bundle path is neither a directory nor a .zip file: {path}"],
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_non_negative_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Bundle manifest {field} must be a non-negative integer.")
    return value


def _existing_source_bytes(manifest_path: Path) -> int | None:
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        capacity = manifest.get("capacity") if isinstance(manifest, dict) else None
        source_bytes = capacity.get("source_bytes") if isinstance(capacity, dict) else None
        return _safe_non_negative_integer(source_bytes, "capacity.source_bytes")
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _content_type(path: str | Path) -> str:
    return _CONTENT_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


def _is_safe_manifest_path(rel: str) -> bool:
    if not rel or "\\" in rel or "\x00" in rel:
        return False
    path = PurePosixPath(rel)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def _manifest_files(manifest: dict[str, Any], errors: list[str]) -> list[dict[str, Any]]:
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        errors.append("Bundle manifest files must be an array.")
        return []
    files: list[dict[str, Any]] = []
    for index, item in enumerate(raw_files):
        if not isinstance(item, dict):
            errors.append(f"Bundle manifest file entry {index} must be an object.")
            continue
        files.append(item)
    return files


def _requires_content_type(manifest: dict[str, Any], errors: list[str]) -> bool:
    version = manifest.get("version")
    if version == "0.1.0":
        return False
    if version != BUNDLE_MANIFEST_VERSION:
        errors.append(f"Unsupported bundle manifest version: {version}")
    elif manifest.get("schema_version") != "markovo.bundle-manifest.v1":
        errors.append("Bundle manifest must declare schema_version markovo.bundle-manifest.v1.")
    return True


def _verify_directory(bundle: Path) -> dict[str, Any]:
    manifest_path = bundle / BUNDLE_MANIFEST_NAME
    if not manifest_path.exists():
        return _verification_result(bundle, 0, [f"Missing {BUNDLE_MANIFEST_NAME}."])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return _verification_result(bundle, 0, ["Bundle manifest must be an object."])
    files = _manifest_files(manifest, errors)
    require_content_type = _requires_content_type(manifest, errors)
    _check_capacity_contract(manifest, errors)
    checked = 0
    for item in files:
        rel = str(item.get("path", ""))
        if not _is_safe_manifest_path(rel):
            errors.append(f"Unsafe manifest path: {rel}")
            continue
        file_path = bundle / rel
        if not file_path.is_file():
            errors.append(f"Missing file: {rel}")
            continue
        checked += 1
        _check_entry(
            rel,
            file_path.stat().st_size,
            _sha256(file_path),
            item,
            errors,
            require_content_type=require_content_type,
        )
    _check_extra_files(
        expected={str(item.get("path")) for item in files if _is_safe_manifest_path(str(item.get("path", "")))},
        actual={
            item.relative_to(bundle).as_posix()
            for item in bundle.rglob("*")
            if item.is_file() and item.name not in {BUNDLE_MANIFEST_NAME, BUNDLE_ARCHIVE_NAME}
        },
        errors=errors,
    )
    return _verification_result(bundle, checked, errors)


def _verify_zip(path: Path) -> dict[str, Any]:
    errors: list[str] = []
    checked = 0
    try:
        with zipfile.ZipFile(path) as archive:
            archive_names = [name for name in archive.namelist() if not name.endswith("/")]
            names = set(archive_names)
            if len(names) != len(archive_names):
                errors.append("Bundle archive contains duplicate file names.")
            for name in archive_names:
                if not _is_safe_manifest_path(name):
                    errors.append(f"Unsafe archive path: {name}")
            if BUNDLE_MANIFEST_NAME not in names:
                return _verification_result(path, 0, [f"Missing {BUNDLE_MANIFEST_NAME}."])
            manifest = json.loads(archive.read(BUNDLE_MANIFEST_NAME).decode("utf-8"))
            if not isinstance(manifest, dict):
                return _verification_result(path, 0, ["Bundle manifest must be an object."])
            files = _manifest_files(manifest, errors)
            require_content_type = _requires_content_type(manifest, errors)
            _check_capacity_contract(manifest, errors)
            expected = {
                str(item.get("path"))
                for item in files
                if _is_safe_manifest_path(str(item.get("path", "")))
            }
            for item in files:
                rel = str(item.get("path", ""))
                if not _is_safe_manifest_path(rel):
                    errors.append(f"Unsafe manifest path: {rel}")
                    continue
                if rel not in names:
                    errors.append(f"Missing file: {rel}")
                    continue
                payload = archive.read(rel)
                checked += 1
                _check_entry(
                    rel,
                    len(payload),
                    hashlib.sha256(payload).hexdigest(),
                    item,
                    errors,
                    require_content_type=require_content_type,
                )
            _check_extra_files(
                expected=expected,
                actual={name for name in names if name != BUNDLE_MANIFEST_NAME and not name.endswith("/")},
                errors=errors,
            )
    except zipfile.BadZipFile:
        return _verification_result(path, 0, ["Invalid zip file."])
    return _verification_result(path, checked, errors)


def _check_entry(
    rel: str,
    size: int,
    sha256: str,
    item: dict[str, Any],
    errors: list[str],
    *,
    require_content_type: bool,
) -> None:
    if size != int(item.get("bytes", -1)):
        errors.append(f"Size mismatch for {rel}: expected {item.get('bytes')}, got {size}")
    if sha256 != item.get("sha256"):
        errors.append(f"SHA-256 mismatch for {rel}")
    declared_content_type = item.get("content_type")
    expected_content_type = _content_type(rel)
    if (require_content_type or declared_content_type is not None) and declared_content_type != expected_content_type:
        errors.append(
            f"Content-Type mismatch for {rel}: expected {expected_content_type}, got {declared_content_type}"
        )


def _check_capacity_contract(manifest: dict[str, Any], errors: list[str]) -> None:
    capacity = manifest.get("capacity")
    # Version 0.2.0 manifests written before the capacity hard gate remain
    # readable. New production manifests always include this source-bound block.
    if capacity is None:
        return
    if not isinstance(capacity, dict):
        errors.append("Bundle manifest capacity must be an object.")
        return
    if capacity.get("policy_id") != BUNDLE_CAPACITY_POLICY_ID:
        errors.append("Bundle manifest capacity policy is unsupported.")
        return
    try:
        expected = validate_bundle_capacity(
            manifest,
            source_bytes=capacity.get("source_bytes"),
        )
    except ValueError as exc:
        errors.append(str(exc))
        return
    if capacity != expected:
        errors.append("Bundle manifest capacity summary does not match its file inventory.")


def _check_extra_files(expected: set[str], actual: set[str], errors: list[str]) -> None:
    extra = sorted(actual - expected)
    if extra:
        errors.extend(f"Unexpected file: {path}" for path in extra)


def _verification_result(path: Path, checked: int, errors: list[str]) -> dict[str, Any]:
    return {
        "status": "passed" if not errors else "failed",
        "target": str(path),
        "files_checked": checked,
        "errors": errors,
    }
