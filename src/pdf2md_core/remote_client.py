from __future__ import annotations

import http.client
import json
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from .bundle_manifest import verify_bundle_manifest
from .credit_amounts import format_credits, parse_credit_units

DEFAULT_BASE_URL = "https://markovo.net"
# The public export flips this to True. Internal release canaries keep explicit
# endpoint selection, while the PyPI package is pinned to the production origin.
PUBLIC_DISTRIBUTION = True
DEFAULT_POLL_ATTEMPTS = 400
IDEMPOTENT_READ_ATTEMPTS = 3
IDEMPOTENT_READ_BACKOFF_SECONDS = 0.5
# Result downloads resume from the bytes already received instead of
# restarting the whole GET when the stream is cut mid-download.
DOWNLOAD_RESUME_ATTEMPTS = 3
DOWNLOAD_CHUNK_BYTES = 64 * 1024
INLINE_UPLOAD_BYTES = 8 * 1024 * 1024


def _export_fields(download_format: str) -> dict[str, str]:
    if download_format not in {"zip", "md", "docx", "txt"}:
        raise ValueError("download_format must be zip, md, docx, or txt.")
    return {"output_format": download_format} if download_format in {"docx", "txt"} else {}

_REMOTE_CAPABILITY_BY_SUFFIX = {
    ".pdf": ("pdf-to-markdown", "application/pdf"),
    ".txt": ("text-to-markdown", "text/plain"),
    ".md": ("text-to-markdown", "text/markdown"),
    ".markdown": ("text-to-markdown", "text/markdown"),
    ".json": ("text-to-markdown", "application/json"),
    ".xml": ("text-to-markdown", "application/xml"),
    ".rtf": ("text-to-markdown", "application/rtf"),
    ".ipynb": ("text-to-markdown", "application/x-ipynb+json"),
    ".eml": ("text-to-markdown", "message/rfc822"),
    ".msg": ("text-to-markdown", "application/vnd.ms-outlook"),
    ".html": ("html-to-markdown", "text/html"),
    ".htm": ("html-to-markdown", "text/html"),
    ".xhtml": ("html-to-markdown", "application/xhtml+xml"),
    ".csv": ("csv-to-markdown", "text/csv"),
    ".docx": (
        "docx-to-markdown",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    ".pptx": (
        "pptx-to-markdown",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
    ".xlsx": (
        "xlsx-to-markdown",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    ".xls": ("spreadsheet-to-markdown", "application/vnd.ms-excel"),
    ".ods": (
        "spreadsheet-to-markdown",
        "application/vnd.oasis.opendocument.spreadsheet",
    ),
    ".epub": ("epub-to-markdown", "application/epub+zip"),
    ".zip": ("notion-to-markdown", "application/zip"),
    ".png": ("image-to-markdown", "image/png"),
    ".jpg": ("image-to-markdown", "image/jpeg"),
    ".jpeg": ("image-to-markdown", "image/jpeg"),
    ".webp": ("image-to-markdown", "image/webp"),
    ".tif": ("image-to-markdown", "image/tiff"),
    ".tiff": ("image-to-markdown", "image/tiff"),
    ".bmp": ("image-to-markdown", "image/bmp"),
    ".mp3": ("audio-to-markdown", "audio/mpeg"),
    ".wav": ("audio-to-markdown", "audio/wav"),
    ".m4a": ("audio-to-markdown", "audio/mp4"),
    ".aac": ("audio-to-markdown", "audio/aac"),
    ".flac": ("audio-to-markdown", "audio/flac"),
    ".ogg": ("audio-to-markdown", "audio/ogg"),
    ".opus": ("audio-to-markdown", "audio/ogg"),
    ".wma": ("audio-to-markdown", "audio/x-ms-wma"),
    ".mp4": ("video-to-markdown", "video/mp4"),
    ".mov": ("video-to-markdown", "video/quicktime"),
    ".m4v": ("video-to-markdown", "video/x-m4v"),
    ".webm": ("video-to-markdown", "video/webm"),
    ".mkv": ("video-to-markdown", "video/x-matroska"),
    ".avi": ("video-to-markdown", "video/x-msvideo"),
    ".wmv": ("video-to-markdown", "video/x-ms-wmv"),
}

REMOTE_FILE_CAPABILITY_IDS = tuple(
    sorted(
        {
            capability_id
            for capability_id, _mime_type in _REMOTE_CAPABILITY_BY_SUFFIX.values()
        }
    )
)


class RemoteClientError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.payload = payload or {}

    def to_dict(self) -> dict[str, Any]:
        error = self.payload.get("error")
        if isinstance(error, dict):
            public_error = {"status": self.status, **error}
            if PUBLIC_DISTRIBUTION and public_error.get("code") == "quota_exceeded":
                public_error.setdefault("action", "open_checkout")
                public_error.setdefault(
                    "portal_url", f"{DEFAULT_BASE_URL}/app#billing"
                )
            return public_error
        return {"status": self.status, "code": "remote_error", "message": str(self)}


def _normalize_base_url(value: str) -> str:
    resolved = value.rstrip("/")
    if PUBLIC_DISTRIBUTION:
        parsed = urllib.parse.urlsplit(resolved)
        if (
            resolved != DEFAULT_BASE_URL
            or parsed.scheme != "https"
            or parsed.hostname != "markovo.net"
            or parsed.port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise RemoteClientError(
                "The public Markovo client connects only to https://markovo.net.",
                status=400,
                payload={
                    "error": {
                        "code": "unsafe_api_origin",
                        "message": "The public Markovo client connects only to https://markovo.net.",
                    }
                },
            )
    return resolved


def _request_url(base_url: str, path: str) -> str:
    resolved_base_url = _normalize_base_url(base_url)
    parsed_path = urllib.parse.urlsplit(path)
    if (
        not path.startswith("/")
        or path.startswith("//")
        or parsed_path.scheme
        or parsed_path.netloc
        or parsed_path.fragment
    ):
        raise RemoteClientError(
            "Refused an unsafe Markovo API request path.",
            status=400,
            payload={
                "error": {
                    "code": "unsafe_api_path",
                    "message": "Refused an unsafe Markovo API request path.",
                }
            },
        )
    return f"{resolved_base_url}{path}"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _open_url(request: urllib.request.Request, *, timeout: float):
    if PUBLIC_DISTRIBUTION:
        return urllib.request.build_opener(_NoRedirectHandler).open(
            request, timeout=timeout
        )
    return urllib.request.urlopen(request, timeout=timeout)


def _download_expected_total(response) -> int | None:
    """Total object size from Content-Range (206) or Content-Length (200)."""
    content_range = response.headers.get("content-range") or ""
    unit, _, spec = content_range.partition(" ")
    bounds, _, total = spec.partition("/")
    if unit == "bytes" and total.isdigit():
        return int(total)
    content_length = response.headers.get("content-length") or ""
    if content_length.isdigit():
        return int(content_length)
    return None


def _download_content_range_start(response) -> int | None:
    content_range = response.headers.get("content-range") or ""
    unit, _, spec = content_range.partition(" ")
    bounds, _, _total = spec.partition("/")
    start, _, _end = bounds.partition("-")
    if unit == "bytes" and start.isdigit():
        return int(start)
    return None


def remote_capability_for_path(path: str | Path) -> tuple[str, str]:
    suffix = Path(path).suffix.lower()
    resolved = _REMOTE_CAPABILITY_BY_SUFFIX.get(suffix)
    if resolved is None:
        raise ValueError(
            f"{suffix or 'Files without an extension'} are not enabled in the Markovo product API."
        )
    return resolved


def _validate_layout_fidelity(layout_fidelity: str, capability_id: str) -> None:
    if layout_fidelity not in {"standard", "formula"}:
        raise ValueError("layout_fidelity must be standard or formula.")
    if layout_fidelity == "formula" and capability_id != "pdf-to-markdown":
        raise ValueError("layout_fidelity=formula is a PDF-only compatibility option.")


@dataclass(slots=True)
class RemoteClient:
    base_url: str
    api_key: str
    timeout_seconds: float = 30.0

    @classmethod
    def from_env(
        cls,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteClient:
        resolved_base_url = _normalize_base_url(
            base_url
            or os.getenv("MARKOVO_BASE_URL")
            or os.getenv("PDF2MD_BASE_URL")
            or DEFAULT_BASE_URL
        )
        resolved_api_key = (
            api_key or os.getenv("MARKOVO_API_KEY") or os.getenv("PDF2MD_API_KEY")
        )
        if not resolved_api_key:
            raise RemoteClientError(
                "MARKOVO_API_KEY is required for customer CLI/MCP/API usage. Create a key in /app#developer.",
                status=401,
                payload={
                    "error": {
                        "code": "missing_api_key",
                        "message": "MARKOVO_API_KEY is required for customer CLI/MCP/API usage.",
                        "action": "create_api_key",
                        "portal_url": f"{resolved_base_url}/app#developer",
                    }
                },
            )
        return cls(
            base_url=resolved_base_url,
            api_key=resolved_api_key,
            timeout_seconds=timeout_seconds,
        )

    @classmethod
    def for_capability_discovery(
        cls,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteClient:
        """Create a client for the public capability catalog without weakening protected APIs."""
        resolved_base_url = _normalize_base_url(
            base_url
            or os.getenv("MARKOVO_BASE_URL")
            or os.getenv("PDF2MD_BASE_URL")
            or DEFAULT_BASE_URL
        )
        resolved_api_key = (
            api_key or os.getenv("MARKOVO_API_KEY") or os.getenv("PDF2MD_API_KEY") or ""
        )
        return cls(
            base_url=resolved_base_url,
            api_key=resolved_api_key,
            timeout_seconds=timeout_seconds,
        )

    def convert_pdf(
        self,
        input_path: str | Path,
        out_dir: str | Path,
        *,
        max_credits: float | str | Decimal,
        mode: str = "fast",
        poll_interval_seconds: float = 1.8,
        poll_attempts: int = DEFAULT_POLL_ATTEMPTS,
        download_format: str = "zip",
        layout_fidelity: str = "standard",
        pages: str | None = None,
    ) -> dict[str, Any]:
        return self.convert_file(
            input_path,
            out_dir,
            max_credits=max_credits,
            mode=mode,
            poll_interval_seconds=poll_interval_seconds,
            poll_attempts=poll_attempts,
            download_format=download_format,
            capability_id="pdf-to-markdown",
            layout_fidelity=layout_fidelity,
            pages=pages,
        )

    def convert_file(
        self,
        input_path: str | Path,
        out_dir: str | Path,
        *,
        max_credits: float | str | Decimal,
        mode: str = "fast",
        poll_interval_seconds: float = 1.8,
        poll_attempts: int = DEFAULT_POLL_ATTEMPTS,
        download_format: str = "zip",
        capability_id: str | None = None,
        layout_fidelity: str = "standard",
        pages: str | None = None,
    ) -> dict[str, Any]:
        export_fields = _export_fields(download_format)
        input_path = Path(input_path)
        out_dir = Path(out_dir)
        estimate = self.create_estimate(
            input_path,
            mode=mode,
            capability_id=capability_id,
            layout_fidelity=layout_fidelity,
            pages=pages,
            **export_fields,
        )
        return self._complete_estimate(
            estimate,
            out_dir,
            max_credits=max_credits,
            poll_interval_seconds=poll_interval_seconds,
            poll_attempts=poll_attempts,
            download_format=download_format,
        )

    def convert_url(
        self,
        url: str,
        out_dir: str | Path,
        *,
        max_credits: float | str | Decimal,
        accept_remote_fetch: bool,
        poll_interval_seconds: float = 1.8,
        poll_attempts: int = DEFAULT_POLL_ATTEMPTS,
        download_format: str = "zip",
    ) -> dict[str, Any]:
        export_fields = _export_fields(download_format)
        estimate = self.create_url_estimate(
            url,
            accept_remote_fetch=accept_remote_fetch,
            **export_fields,
        )
        return self._complete_estimate(
            estimate,
            out_dir,
            max_credits=max_credits,
            poll_interval_seconds=poll_interval_seconds,
            poll_attempts=poll_attempts,
            download_format=download_format,
        )

    def create_url_estimate(
        self,
        url: str,
        *,
        accept_remote_fetch: bool,
        output_format: str | None = None,
    ) -> dict[str, Any]:
        if accept_remote_fetch is not True:
            raise ValueError("URL conversion requires explicit remote-fetch consent.")
        if not isinstance(url, str) or not url.strip():
            raise ValueError("url is required.")
        if output_format is not None and output_format not in {"md", "docx", "txt"}:
            raise ValueError("output_format must be md, docx, or txt.")
        payload = json.dumps(
            {
                "url": url.strip(),
                "accept_remote_fetch": True,
                **({"output_format": output_format} if output_format is not None else {}),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        return self._request_json(
            "POST",
            "/v1/url-estimates",
            body=payload,
            headers={"content-type": "application/json"},
        )

    def _complete_estimate(
        self,
        estimate: dict[str, Any],
        out_dir: str | Path,
        *,
        max_credits: float | str | Decimal,
        poll_interval_seconds: float,
        poll_attempts: int,
        download_format: str,
    ) -> dict[str, Any]:
        out_dir = Path(out_dir)
        upload = self.confirm_estimate(estimate, max_credits=max_credits)
        job = first_job(upload)
        job_id = str(job.get("id") or job.get("job_id") or upload.get("job_id") or "")
        if not job_id:
            raise RemoteClientError(
                "Remote API did not return a job id.", payload=upload
            )
        completed = self.wait_for_job(
            job_id,
            poll_interval_seconds=poll_interval_seconds,
            poll_attempts=poll_attempts,
        )
        download_path = self.download_job(job_id, out_dir, format=download_format)
        bundle_verification = None
        if download_format == "zip":
            bundle_verification = verify_bundle_manifest(download_path)
            if bundle_verification.get("status") != "passed":
                raise RemoteClientError(
                    "Downloaded Markovo bundle failed manifest verification.",
                    payload={"verification": bundle_verification},
                )
        return {
            "job_id": job_id,
            "status": completed.get("status"),
            "capability_id": completed.get("capability_id"),
            "estimated_credits": completed.get("estimated_credits"),
            "actual_credits": completed.get("actual_credits"),
            "credit_scale": completed.get("credit_scale", 1_000),
            "credit_model_version": completed.get("credit_model_version"),
            "rate_card_version": completed.get("rate_card_version"),
            "estimated_credit_units": completed.get("estimated_credit_units"),
            "actual_credit_units": completed.get("actual_credit_units"),
            "delivery_status": completed.get("delivery_status"),
            "job": completed,
            "output_dir": str(out_dir),
            "download_path": str(download_path),
            "download_format": download_format,
            **(
                {"bundle_verification": bundle_verification}
                if bundle_verification is not None
                else {}
            ),
        }

    def create_estimate(
        self,
        input_path: str | Path,
        *,
        mode: str = "fast",
        capability_id: str | None = None,
        layout_fidelity: str = "standard",
        pages: str | None = None,
        output_format: str | None = None,
    ) -> dict[str, Any]:
        """Upload once and receive the stable upload id used for retry-safe confirmation.

        Unconfirmed uploads expire after one hour and are removed by the server cleanup job.
        """
        path = Path(input_path)
        inferred_capability, declared_mime = remote_capability_for_path(path)
        if capability_id and capability_id != inferred_capability:
            raise ValueError(
                f"Capability mismatch: {capability_id} does not accept {path.suffix.lower()}."
            )
        _validate_layout_fidelity(layout_fidelity, inferred_capability)
        if output_format is not None and output_format not in {"md", "docx", "txt"}:
            raise ValueError("output_format must be md, docx, or txt.")
        fields = {
            "mode": mode,
            "capability_id": capability_id or inferred_capability,
            "layout_fidelity": layout_fidelity,
            **({"output_format": output_format} if output_format is not None else {}),
        }
        if pages is not None:
            if inferred_capability != "pdf-to-markdown":
                raise ValueError("pages is a pdf-to-markdown option only.")
            fields["pages"] = pages
        estimate_client = self
        if path.stat().st_size > INLINE_UPLOAD_BYTES:
            fields["upload_session_id"] = self._stage_upload(path, inferred_capability, declared_mime)
            files = []
            estimate_client = RemoteClient(self.base_url, self.api_key, max(self.timeout_seconds, 300))
        else:
            files = [("file", path.name, path.read_bytes(), declared_mime)]
        body, content_type = encode_multipart(fields, files)
        for attempt in range(3):
            try:
                return estimate_client._request_json(
                    "POST",
                    "/v1/estimates",
                    body=body,
                    headers={"content-type": content_type},
                )
            except RemoteClientError as error:
                if attempt == 2 or error.status not in {500, 502, 503, 504}:
                    raise
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError("estimate retry loop exhausted")  # pragma: no cover

    def _stage_upload(self, path: Path, capability_id: str, content_type: str) -> str:
        """Use the existing R2 part protocol without buffering a large source."""
        before = path.stat()
        session = self._request_json("POST", "/v1/uploads", body=json.dumps({
            "filename": path.name, "size": before.st_size,
            "capability_id": capability_id, "content_type": content_type,
        }).encode(), headers={"content-type": "application/json"})
        session_id = session.get("upload_session_id")
        part_size, part_count = session.get("part_size"), session.get("part_count")
        if (not isinstance(session_id, str) or not session_id.startswith("ups_")
                or len(session_id) != 36 or any(c not in "0123456789abcdef" for c in session_id[4:])
                or type(part_size) is not int or not 0 < part_size <= 32 * 1024 * 1024
                or type(part_count) is not int or part_count != (before.st_size + part_size - 1) // part_size):
            raise RemoteClientError("Markovo returned an invalid upload session.")
        # A 32MiB part can take longer than the ordinary API timeout on a slow
        # uplink. This allowance applies only to the byte transfer, before billing.
        transfer = RemoteClient(self.base_url, self.api_key, max(self.timeout_seconds, 300))
        try:
            with path.open("rb") as stream:
                for number in range(1, part_count + 1):
                    expected = min(part_size, before.st_size - (number - 1) * part_size)
                    chunk = stream.read(expected)
                    if len(chunk) != expected:
                        raise ValueError("The source file changed during upload. Retry with a stable file.")
                    for attempt in range(3):
                        try:
                            transfer._request_json("PUT", f"/v1/uploads/{session_id}/parts/{number}",
                                body=chunk, headers={"content-type": "application/octet-stream"})
                            break
                        except RemoteClientError as error:
                            if attempt == 2 or error.status not in {408, 429, 500, 502, 503, 504}:
                                raise
                            time.sleep(0.5 * (attempt + 1))
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("The source file changed during upload. Retry with a stable file.")
            return session_id
        except Exception:
            try:
                self._request_json("DELETE", f"/v1/uploads/{session_id}")
            except Exception:
                pass  # Server-side expiry remains the fallback cleanup.
            raise

    def confirm_estimate(
        self,
        estimate: dict[str, Any],
        *,
        max_credits: float | str | Decimal,
    ) -> dict[str, Any]:
        """Confirm a stable upload id; one ambiguous network failure is replayed safely."""
        upload_id = estimate.get("upload_id")
        if not isinstance(upload_id, str) or not upload_id:
            raise RemoteClientError(
                "Remote estimate did not return an upload id.", payload=estimate
            )
        try:
            max_credit_units = parse_credit_units(max_credits)
        except ValueError as exc:
            raise ValueError(f"max_credits is invalid: {exc}") from exc
        payload = json.dumps(
            {
                "upload_id": upload_id,
                "max_credit_units": max_credit_units,
                "max_credits": format_credits(max_credit_units).replace(",", ""),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        for attempt in range(2):
            try:
                return self._request_json(
                    "POST",
                    "/v1/jobs",
                    body=payload,
                    headers={"content-type": "application/json"},
                )
            except RemoteClientError as error:
                if attempt == 0 and error.to_dict().get("retryable") is True:
                    continue
                raise

    def upload(
        self,
        input_path: str | Path,
        *,
        mode: str = "fast",
        max_credits: float | str | Decimal,
        capability_id: str | None = None,
        layout_fidelity: str = "standard",
    ) -> dict[str, Any]:
        """Compatibility shortcut; new retry-safe integrations should use two-step confirmation."""
        try:
            max_credit_units = parse_credit_units(max_credits)
        except ValueError as exc:
            raise ValueError(f"max_credits is invalid: {exc}") from exc
        path = Path(input_path)
        inferred_capability, declared_mime = remote_capability_for_path(path)
        _validate_layout_fidelity(layout_fidelity, inferred_capability)
        if layout_fidelity == "formula":
            raise ValueError(
                "Formula enhancement requires the two-step estimate and confirmation workflow."
            )
        if capability_id and capability_id != inferred_capability:
            raise ValueError(
                f"Capability mismatch: {capability_id} does not accept {path.suffix.lower()}."
            )
        fields = {
            "mode": mode,
            "max_credit_units": str(max_credit_units),
            "max_credits": format_credits(max_credit_units).replace(",", ""),
            "capability_id": capability_id or inferred_capability,
            "layout_fidelity": layout_fidelity,
        }
        files = [("file", path.name, path.read_bytes(), declared_mime)]
        body, content_type = encode_multipart(fields, files)
        return self._request_json(
            "POST",
            "/v1/convert",
            body=body,
            headers={"content-type": content_type},
        )

    def wait_for_job(
        self,
        job_id: str,
        *,
        poll_interval_seconds: float = 1.8,
        poll_attempts: int = DEFAULT_POLL_ATTEMPTS,
    ) -> dict[str, Any]:
        current: dict[str, Any] = {}
        for attempt in range(poll_attempts):
            current = self.get_job(job_id)
            job = normalize_job(current)
            status = job.get("status")
            if status == "succeeded":
                return job
            if status in {"failed", "cancelled", "timed_out"}:
                cancelled = status == "cancelled"
                timed_out = status == "timed_out"
                message = (
                    "Conversion was cancelled. Credits were not charged."
                    if cancelled
                    else (
                        "Conversion timed out. Reserved Credits were released."
                        if timed_out
                        else str(job.get("error") or "Remote conversion failed.")
                    )
                )
                code = (
                    "conversion_cancelled"
                    if cancelled
                    else (
                        "conversion_timeout"
                        if timed_out
                        else str(
                            job.get("error_code")
                            or "conversion_failed"
                        )
                    )
                )
                error_payload: dict[str, Any] = {
                    "code": code,
                    "message": message,
                    "job_id": str(job.get("id") or job_id),
                }
                if cancelled or timed_out:
                    error_payload["retryable"] = False
                # This is already a public billing field on the job response.
                # Preserve only the canonical zero value so trusted automation
                # can prove a terminal failure incurred no customer charge;
                # it is not an internal route, Provider, or diagnostic detail.
                actual_credit_units = job.get("actual_credit_units")
                if (
                    isinstance(actual_credit_units, int)
                    and not isinstance(actual_credit_units, bool)
                    and actual_credit_units == 0
                ):
                    error_payload["actual_credit_units"] = actual_credit_units
                raise RemoteClientError(
                    message,
                    payload={"error": error_payload},
                )
            if attempt < poll_attempts - 1:
                time.sleep(poll_interval_seconds)
        raise RemoteClientError(
            "Job status polling ended before the conversion reached a terminal state.",
            payload={
                "error": {
                    "code": "job_poll_exhausted",
                    "message": "Job status polling ended before the conversion reached a terminal state.",
                    "job_id": job_id,
                    "retryable": True,
                }
            },
        )

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self._request_idempotent_json(f"/v1/jobs/{urllib.parse.quote(job_id)}")

    def download_job(
        self, job_id: str, out_dir: str | Path, *, format: str = "zip"
    ) -> Path:
        _export_fields(format)
        out_dir = Path(out_dir)
        data = self._download_bytes(
            f"/v1/jobs/{urllib.parse.quote(job_id)}/download?format={format}"
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        if format != "zip":
            target = out_dir / f"output.{format}"
            target.write_bytes(data)
            return target
        archive = out_dir / "bundle.zip"
        archive.write_bytes(data)
        verification = verify_bundle_manifest(archive)
        if verification.get("status") != "passed":
            raise RemoteClientError(
                "Downloaded Markovo bundle failed manifest verification before extraction.",
                payload={"verification": verification},
            )
        shutil.unpack_archive(str(archive), out_dir)
        return archive

    def list_job_assets(self, job_id: str) -> dict[str, Any]:
        """List manifest-verified image assets without exposing storage identifiers."""
        return self._request_json(
            "GET", f"/v1/jobs/{urllib.parse.quote(job_id)}/assets"
        )

    def create_asset_grant(
        self,
        job_id: str,
        asset_path: str,
        *,
        expires_in_seconds: int = 300,
    ) -> dict[str, Any]:
        """Issue a revocable, short-lived bearer URL for one Bundle image asset."""
        if (
            isinstance(expires_in_seconds, bool)
            or not isinstance(expires_in_seconds, int)
            or not 60 <= expires_in_seconds <= 600
        ):
            raise ValueError("expires_in_seconds must be an integer from 60 to 600.")
        if not asset_path.startswith("assets/"):
            raise ValueError("asset_path must name a file under assets/.")
        body = json.dumps(
            {
                "asset_path": asset_path,
                "expires_in_seconds": expires_in_seconds,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        return self._request_json(
            "POST",
            f"/v1/jobs/{urllib.parse.quote(job_id)}/asset-grants",
            body=body,
            headers={"content-type": "application/json"},
        )

    def revoke_asset_grant(self, job_id: str, grant_id: str) -> dict[str, Any]:
        """Revoke one temporary asset URL before its natural expiry."""
        return self._request_json(
            "DELETE",
            f"/v1/jobs/{urllib.parse.quote(job_id)}/asset-grants/{urllib.parse.quote(grant_id)}",
        )

    def account(self) -> dict[str, Any]:
        return self._request_json("GET", "/v1/account/access")

    def usage(self) -> dict[str, Any]:
        return self._request_json("GET", "/v1/account/usage")

    def capabilities(self) -> dict[str, Any]:
        """Return the live capability registry used by every customer surface."""
        return self._request_json("GET", "/v1/capabilities")

    def billing(self) -> dict[str, Any]:
        access = self.account()
        return {
            "authenticated": bool(access.get("authenticated")),
            "tier": access.get("tier"),
            "portal_url": f"{self.base_url}/app#billing",
            "message": "Billing changes require a signed-in browser session.",
        }

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        data = self._request_bytes(method, path, body=body, headers=headers)
        if not data:
            return {}
        payload = json.loads(data.decode("utf-8"))
        if not isinstance(payload, dict):
            raise RemoteClientError("Remote API returned a non-object JSON payload.")
        return payload

    def _request_idempotent_json(self, path: str) -> dict[str, Any]:
        for attempt in range(IDEMPOTENT_READ_ATTEMPTS):
            try:
                return self._request_json("GET", path)
            except RemoteClientError as error:
                if not self._should_retry_idempotent_read(error, attempt):
                    raise
                time.sleep(IDEMPOTENT_READ_BACKOFF_SECONDS * (attempt + 1))
        raise RuntimeError(
            "idempotent JSON read retry loop exhausted"
        )  # pragma: no cover

    def _download_bytes(self, path: str) -> bytes:
        """Stream a result download, resuming with Range/If-Range after a cut.

        Retries carry `Range: bytes=<received>-` and `If-Range: <etag>` so a
        mid-stream IncompleteRead or timeout continues where it stopped. A
        server that answers a resume with 200 (stale validator) means the
        object changed, so the buffered bytes are discarded and the download
        restarts from the fresh body — bounded by the same attempt budget.
        """
        received = bytearray()
        etag: str | None = None
        attempts = 0
        while True:
            headers: dict[str, str] = {
                "accept": "application/json,text/plain,*/*",
                "user-agent": "markovo-cli/0.1 (+https://markovo.net)",
            }
            if self.api_key:
                headers["authorization"] = f"Bearer {self.api_key}"
            if received and etag:
                headers["range"] = f"bytes={len(received)}-"
                headers["if-range"] = etag
            request = urllib.request.Request(
                _request_url(self.base_url, path),
                method="GET",
                headers=headers,
            )
            try:
                with _open_url(request, timeout=self.timeout_seconds) as response:
                    status = response.getcode() or 200
                    response_etag = response.headers.get("etag")
                    if response_etag:
                        etag = response_etag
                    if status == 200:
                        # The resume validator did not hold: the stored bytes
                        # are stale, restart from the fresh full body.
                        received.clear()
                    expected = _download_expected_total(response)
                    if status == 206:
                        start = _download_content_range_start(response)
                        if start is None or start != len(received):
                            # A misaligned partial cannot be appended safely;
                            # restart from scratch on the next attempt.
                            received.clear()
                            attempts += 1
                            if attempts > DOWNLOAD_RESUME_ATTEMPTS:
                                raise RemoteClientError(
                                    "The download was interrupted repeatedly. Please try again.",
                                    status=503,
                                    payload={
                                        "error": {
                                            "code": "download_interrupted",
                                            "message": "The download was interrupted repeatedly. Please try again.",
                                            "retryable": True,
                                        }
                                    },
                                )
                            continue
                    try:
                        while True:
                            chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                            if not chunk:
                                break
                            received.extend(chunk)
                    except http.client.IncompleteRead as cut:
                        partial = cut.partial or b""
                        received.extend(
                            partial if isinstance(partial, bytes) else bytes(partial)
                        )
                    if expected is None or len(received) >= expected:
                        return bytes(received)
                    # Truncated body that ended without an exception.
            except urllib.error.HTTPError as error:
                raw = error.read()
                payload = parse_json_object(raw)
                message = (
                    error_message(payload)
                    or error.reason
                    or f"Remote API request failed with status {error.code}."
                )
                raise RemoteClientError(
                    message, status=error.code, payload=payload
                ) from error
            except (
                urllib.error.URLError,
                TimeoutError,
                ConnectionError,
                http.client.HTTPException,
            ) as error:
                partial = getattr(error, "partial", b"") or b""
                received.extend(
                    partial if isinstance(partial, bytes) else bytes(partial)
                )
            attempts += 1
            if attempts > DOWNLOAD_RESUME_ATTEMPTS:
                raise RemoteClientError(
                    "The download was interrupted repeatedly. Please try again.",
                    status=503,
                    payload={
                        "error": {
                            "code": "download_interrupted",
                            "message": "The download was interrupted repeatedly. Please try again.",
                            "retryable": True,
                        }
                    },
                )
            # With nothing retained there is no resume position; the bounded
            # loop then simply restarts the GET like a normal read retry.
            time.sleep(IDEMPOTENT_READ_BACKOFF_SECONDS * attempts)

    @staticmethod
    def _should_retry_idempotent_read(error: RemoteClientError, attempt: int) -> bool:
        return (
            attempt + 1 < IDEMPOTENT_READ_ATTEMPTS
            and error.to_dict().get("retryable") is True
            and error.status in {429, 500, 502, 503, 504}
        )

    def _request_bytes(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        request_headers = {
            "accept": "application/json,text/plain,*/*",
            "user-agent": "markovo-cli/0.1 (+https://markovo.net)",
            **(headers or {}),
        }
        if self.api_key:
            request_headers["authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            _request_url(self.base_url, path),
            data=body,
            method=method,
            headers=request_headers,
        )
        try:
            with _open_url(request, timeout=self.timeout_seconds) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            raw = error.read()
            payload = parse_json_object(raw)
            message = (
                error_message(payload)
                or error.reason
                or f"Remote API request failed with status {error.code}."
            )
            raise RemoteClientError(
                message, status=error.code, payload=payload
            ) from error
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as error:
            raise RemoteClientError(
                "Could not reach the Markovo service. Please try again.",
                status=503,
                payload={
                    "error": {
                        "code": "service_unreachable",
                        "message": "Could not reach the Markovo service. Please try again.",
                        "retryable": True,
                    }
                },
            ) from error


def remote_product_diagnostics(
    *, base_url: str | None = None, api_key: str | None = None
) -> dict[str, Any]:
    """Report customer client/API readiness without probing local conversion engines."""
    discovery = RemoteClient.for_capability_discovery(
        base_url=base_url, api_key=api_key
    )
    catalog = discovery.capabilities()
    capabilities = catalog.get("capabilities")
    if not isinstance(capabilities, list):
        raise RemoteClientError(
            "Markovo capability registry returned an invalid response."
        )

    status_counts: dict[str, int] = {}
    for capability in capabilities:
        if not isinstance(capability, dict):
            continue
        status = str(capability.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    configured_key = (
        api_key or os.getenv("MARKOVO_API_KEY") or os.getenv("PDF2MD_API_KEY")
    )
    authentication: dict[str, Any]
    credit_scale: int | None = None
    if configured_key:
        account = RemoteClient.from_env(base_url=base_url, api_key=api_key).account()
        authentication = {"configured": True, "verified": True}
        raw_scale = account.get("credit_scale")
        if isinstance(raw_scale, int):
            credit_scale = raw_scale
    else:
        authentication = {
            "configured": False,
            "verified": False,
            "action": f"Create an API key at {discovery.base_url}/app#developer.",
        }

    try:
        client_version = version("markovo")
    except PackageNotFoundError:  # pragma: no cover - source-tree invocation
        client_version = "source"
    result: dict[str, Any] = {
        "status": "ready" if configured_key else "api_key_required",
        "client_version": client_version,
        "base_url": discovery.base_url,
        "service_reachable": True,
        "authentication": authentication,
        "capabilities": {
            "available": status_counts.get("available", 0),
            "beta": status_counts.get("beta", 0),
            "total": sum(status_counts.values()),
        },
        "commands": ["markovo", "markovo-mcp"],
    }
    if credit_scale is not None:
        result["credit_scale"] = credit_scale
    return result


def encode_multipart(
    fields: dict[str, str],
    files: list[tuple[str, str, bytes, str]],
) -> tuple[bytes, str]:
    boundary = f"----markovo{int(time.time() * 1000)}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                str(value).encode(),
                b"\r\n",
            ]
        )
    for field_name, filename, content, content_type in files:
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                content,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def parse_json_object(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def error_message(payload: dict[str, Any]) -> str | None:
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        return str(message) if message else None
    if isinstance(error, str):
        return error
    message = payload.get("message")
    return str(message) if message else None


def normalize_job(payload: dict[str, Any]) -> dict[str, Any]:
    job = payload.get("job")
    if isinstance(job, dict):
        normalized = dict(job)
    else:
        normalized = dict(payload)
    if "id" not in normalized and "job_id" in normalized:
        normalized["id"] = normalized["job_id"]
    return normalized


def first_job(payload: dict[str, Any]) -> dict[str, Any]:
    jobs = payload.get("jobs")
    if isinstance(jobs, list) and jobs and isinstance(jobs[0], dict):
        return normalize_job(jobs[0])
    return normalize_job(payload)
