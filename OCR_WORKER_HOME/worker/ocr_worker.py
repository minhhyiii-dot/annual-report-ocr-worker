from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from collections import Counter, deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any


SCHEMA_VERSION = "2.0"
REQUIRED_WORKER_PROFILE = "paddleocr-vl-v1.6-raw-maxtext-r1"
SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_SOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_ASSET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_TICKER = re.compile(r"^[A-Z0-9]{1,16}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
JOB_MUTABLE_ROOTS = {"output", "state", "logs"}
EXPECTED_OUTPUTS = {"markdown_suffix": ".md", "json_suffix": "_res.json"}
EXPECTED_OCR_CONFIG = {
    "engine": "PaddleOCR-VL",
    "pipeline_version": "v1.6",
    "dpi": 220,
    "page_batch_size": 1,
    "max_new_tokens": 1024,
    "page_timeout_seconds": 1200,
    "vl_rec_max_concurrency": 1,
    "use_ocr_for_image_block": True,
    "use_chart_recognition": False,
    "markdown_ignore_labels": ["header_image", "footer_image"],
}
COMMON_PACKAGES = {"paddleocr": "3.7.0", "paddlex": "3.7.2"}
ENGINE_STDERR_TAIL_MAX_LINES = 8
ENGINE_STDERR_TAIL_MAX_CHARS = 1200


class WorkerError(RuntimeError):
    pass


@dataclass(frozen=True)
class Source:
    source_id: str
    ticker: str
    segment: str
    lane: str
    total_pages: int
    source_pdf_sha256: str | None = None

    def portable(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "source_id": self.source_id,
            "ticker": self.ticker,
            "segment": self.segment,
            "lane": self.lane,
            "total_pages": self.total_pages,
        }
        if self.source_pdf_sha256 is not None:
            value["source_pdf_sha256"] = self.source_pdf_sha256
        return value


@dataclass(frozen=True)
class Asset:
    source_id: str
    ticker: str
    asset_id: str
    input_path: str
    sha256: str
    required_content: bool
    page_number: int | None = None

    @property
    def key(self) -> tuple[str, str]:
        return self.source_id, self.asset_id

    def portable(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "source_id": self.source_id,
            "ticker": self.ticker,
            "asset_id": self.asset_id,
            "input_path": self.input_path,
            "sha256": self.sha256,
            "required_content": self.required_content,
        }
        if self.page_number is not None:
            value["page_number"] = self.page_number
        return value


@dataclass(frozen=True)
class Job:
    job_id: str
    sources: tuple[Source, ...]
    assets: tuple[Asset, ...]
    probe_key: tuple[str, str]

    @property
    def assets_by_key(self) -> dict[tuple[str, str], Asset]:
        return {asset.key: asset for asset in self.assets}

    @property
    def assets_by_id(self) -> dict[str, Asset]:
        return {asset.asset_id.casefold(): asset for asset in self.assets}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(payload: object) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WorkerError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkerError(f"cannot read JSON {path.name}: {exc}") from exc


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def atomic_write_json(path: Path, payload: object) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _safe_name(value: Any, pattern: re.Pattern[str], field: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise WorkerError(f"unsafe {field}")
    if value.split(".", 1)[0].casefold() in WINDOWS_RESERVED or value.rstrip(" .") != value:
        raise WorkerError(f"reserved {field}")
    return value


def safe_relative_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or ":" in value
        or "//" in value
        or value.endswith("/")
        or any(ord(character) < 32 for character in value)
    ):
        raise WorkerError(f"unsafe relative path: {value!r}")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise WorkerError(f"unsafe relative path: {value!r}")
    if any(
        part.rstrip(" .") != part or part.split(".", 1)[0].casefold() in WINDOWS_RESERVED
        for part in pure.parts
    ):
        raise WorkerError(f"reserved Windows path: {value!r}")
    return pure.as_posix()


def _resolve_contained(
    root: Path,
    path: Path,
    *,
    must_exist: bool = True,
    description: str = "path",
) -> Path:
    """Resolve a path and require its destination to stay below root.

    Reparse metadata is deliberately irrelevant here.  OneDrive placeholders,
    symlinks, and junctions are accepted when normal path resolution keeps their
    destination inside the allowed root; an escaping destination is rejected by
    the same containment rule as any other path.
    """
    try:
        base = root.resolve(strict=True)
        resolved = path.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise WorkerError(f"cannot resolve {description}: {path}") from exc
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise WorkerError(f"{description} escapes allowed root: {path}") from exc
    return resolved


def ensure_directory(root: Path, directory: Path) -> None:
    root = root.resolve(strict=True)
    candidate = directory.absolute()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise WorkerError(f"directory escapes Worker Home: {directory}") from exc
    _resolve_contained(root, candidate, must_exist=False, description="directory")
    candidate.mkdir(parents=True, exist_ok=True)
    _resolve_contained(root, candidate, description="directory")


def resolve_inside(root: Path, relative: str, *, must_exist: bool = True) -> Path:
    relative = safe_relative_path(relative)
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    try:
        resolved = candidate.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise WorkerError(f"missing file: {relative}") from exc
    base = root.resolve(strict=True)
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise WorkerError(f"path escapes allowed root: {relative}") from exc
    return resolved


def _job_immutable_files(job_root: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for child in job_root.iterdir():
        if child.name in JOB_MUTABLE_ROOTS or child.name == "job_checksums.sha256":
            continue
        _resolve_contained(job_root, child, description="job immutable path")
        paths = [child] if child.is_file() else [path for path in child.rglob("*") if path.is_file()]
        for path in paths:
            _resolve_contained(job_root, path, description="job immutable file")
            relative = path.relative_to(job_root).as_posix()
            key = relative.casefold()
            if key in result:
                raise WorkerError(f"duplicate case-insensitive job path: {relative}")
            result[key] = path
    return result


def verify_job_checksums(job_root: Path) -> int:
    manifest_path = job_root / "job_checksums.sha256"
    if not manifest_path.is_file():
        raise WorkerError("missing or unsafe job_checksums.sha256")
    manifest_path = _resolve_contained(job_root, manifest_path, description="job checksum manifest")
    expected: dict[str, tuple[str, str]] = {}
    try:
        lines = manifest_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise WorkerError(f"cannot read job checksum manifest: {exc}") from exc
    for line in lines:
        if not line.strip():
            continue
        match = re.fullmatch(r"([0-9a-fA-F]{64})  (.+)", line)
        if not match:
            raise WorkerError("malformed job checksum line")
        relative = safe_relative_path(match.group(2))
        key = relative.casefold()
        if key in expected:
            raise WorkerError(f"duplicate job checksum path: {relative}")
        expected[key] = (relative, match.group(1).lower())
    actual = _job_immutable_files(job_root)
    if set(actual) != set(expected):
        missing = sorted(expected.keys() - actual.keys())
        extra = sorted(actual.keys() - expected.keys())
        raise WorkerError(f"job immutable file set mismatch; missing={missing}, extra={extra}")
    for key, path in actual.items():
        relative, expected_digest = expected[key]
        if sha256_file(path) != expected_digest:
            raise WorkerError(f"job checksum mismatch: {relative}")
    return len(expected)


def load_job(job_root: Path) -> Job:
    verify_job_checksums(job_root)
    payload = read_json(job_root / "job.json")
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version", "job_id", "required_worker_profile", "sources", "assets",
        "probe", "expected_outputs",
    }:
        raise WorkerError("job.json fields do not match schema 2.0")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise WorkerError("unsupported job schema")
    if payload["required_worker_profile"] != REQUIRED_WORKER_PROFILE:
        raise WorkerError("job requires an unsupported worker profile")
    if payload["expected_outputs"] != EXPECTED_OUTPUTS:
        raise WorkerError("unexpected output contract")
    job_id = _safe_name(payload["job_id"], SAFE_JOB_ID, "job_id")

    raw_sources = payload["sources"]
    if not isinstance(raw_sources, list) or not raw_sources:
        raise WorkerError("sources must be a non-empty list")
    sources: list[Source] = []
    by_source: dict[str, Source] = {}
    for raw in raw_sources:
        if not isinstance(raw, dict):
            raise WorkerError("invalid source entry")
        required = {"source_id", "ticker", "segment", "lane", "total_pages"}
        optional = {"source_pdf_sha256"}
        if not required.issubset(raw) or not set(raw).issubset(required | optional):
            raise WorkerError("invalid source fields")
        source_id = _safe_name(raw["source_id"], SAFE_SOURCE_ID, "source_id")
        ticker = _safe_name(raw["ticker"], SAFE_TICKER, "ticker")
        if ticker != ticker.upper():
            raise WorkerError("ticker must be uppercase")
        if source_id.casefold() in by_source:
            raise WorkerError("duplicate source_id")
        if raw["segment"] not in {"VN30", "VNMidcap"} or raw["lane"] not in {"scan", "mixed"}:
            raise WorkerError("invalid source segment or lane")
        total_pages = raw["total_pages"]
        if isinstance(total_pages, bool) or not isinstance(total_pages, int) or total_pages <= 0:
            raise WorkerError("invalid source total_pages")
        digest = raw.get("source_pdf_sha256")
        if digest is not None and (not isinstance(digest, str) or not SHA256_RE.fullmatch(digest)):
            raise WorkerError("invalid source_pdf_sha256")
        source = Source(source_id, ticker, raw["segment"], raw["lane"], total_pages, digest)
        sources.append(source)
        by_source[source_id.casefold()] = source

    raw_assets = payload["assets"]
    if not isinstance(raw_assets, list) or not raw_assets:
        raise WorkerError("assets must be a non-empty list")
    assets: list[Asset] = []
    asset_keys: set[tuple[str, str]] = set()
    destination_keys: set[tuple[str, str]] = set()
    for raw in raw_assets:
        if not isinstance(raw, dict):
            raise WorkerError("invalid asset entry")
        required = {"source_id", "ticker", "asset_id", "input_path", "sha256", "required_content"}
        optional = {"page_number"}
        if not required.issubset(raw) or not set(raw).issubset(required | optional):
            raise WorkerError("invalid asset fields")
        source_id = _safe_name(raw["source_id"], SAFE_SOURCE_ID, "asset source_id")
        source = by_source.get(source_id.casefold())
        if source is None:
            raise WorkerError("asset references an unknown source")
        if source_id != source.source_id:
            raise WorkerError("asset source_id case does not match source")
        if raw["ticker"] != source.ticker:
            raise WorkerError("asset ticker does not match source")
        asset_id = _safe_name(raw["asset_id"], SAFE_ASSET_ID, "asset_id")
        expected_input = f"input/{source.ticker}/{asset_id}.png"
        if raw["input_path"] != expected_input:
            raise WorkerError("asset input_path does not match ticker/asset_id")
        digest = raw["sha256"]
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise WorkerError("invalid asset SHA256")
        if not isinstance(raw["required_content"], bool):
            raise WorkerError("required_content must be boolean")
        page_number = raw.get("page_number")
        if page_number is not None and (
            isinstance(page_number, bool)
            or not isinstance(page_number, int)
            or not 1 <= page_number <= source.total_pages
        ):
            raise WorkerError("asset page_number is outside source report")
        key = (source_id.casefold(), asset_id.casefold())
        destination = (source.ticker.casefold(), asset_id.casefold())
        if key in asset_keys or destination in destination_keys:
            raise WorkerError("duplicate asset or output destination")
        input_file = resolve_inside(job_root, expected_input)
        if not input_file.is_file() or input_file.suffix.casefold() != ".png":
            raise WorkerError("asset input is not a PNG")
        if sha256_file(input_file) != digest:
            raise WorkerError(f"asset SHA256 mismatch: {expected_input}")
        asset = Asset(source_id, source.ticker, asset_id, expected_input, digest, raw["required_content"], page_number)
        assets.append(asset)
        asset_keys.add(key)
        destination_keys.add(destination)

    probe = payload["probe"]
    if not isinstance(probe, dict) or set(probe) != {"source_id", "ticker", "asset_id", "input_path"}:
        raise WorkerError("invalid probe")
    probe_key = (probe["source_id"], probe["asset_id"])
    probe_asset = {asset.key: asset for asset in assets}.get(probe_key)
    if (
        probe_asset is None
        or probe["ticker"] != probe_asset.ticker
        or probe["input_path"] != probe_asset.input_path
        or not probe_asset.required_content
    ):
        raise WorkerError("probe must reference an assigned required-content asset")
    return Job(job_id, tuple(sources), tuple(assets), probe_key)


def _zip_member_path(name: str) -> PurePosixPath:
    if "\\" in name or not name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise WorkerError(f"unsafe ZIP entry: {name!r}")
    stripped = name[:-1] if name.endswith("/") else name
    if not stripped:
        raise WorkerError("unsafe empty ZIP entry")
    normalized = safe_relative_path(stripped)
    return PurePosixPath(normalized)


def import_one_job(home_root: Path, archive_path: Path) -> dict[str, Any]:
    try:
        archive_path = archive_path.resolve(strict=True)
    except OSError as exc:
        raise WorkerError(f"job ZIP does not exist: {archive_path}") from exc
    try:
        archive_path.relative_to(home_root.resolve(strict=True))
    except ValueError as exc:
        raise WorkerError("job ZIP must be copied inside Worker Home before import") from exc
    if archive_path.suffix.casefold() != ".zip" or not archive_path.is_file():
        raise WorkerError("job import source must be a regular ZIP")

    jobs_root = home_root / "jobs"
    ensure_directory(home_root, jobs_root)
    temporary = jobs_root / f".importing.{os.getpid()}.{uuid.uuid4().hex}"
    ensure_directory(home_root, temporary)
    wrapper: str | None = None
    seen: set[str] = set()
    try:
        try:
            archive = zipfile.ZipFile(archive_path, "r")
        except (OSError, zipfile.BadZipFile) as exc:
            raise WorkerError(f"cannot open job ZIP: {exc}") from exc
        with archive:
            infos = archive.infolist()
            if not infos:
                raise WorkerError("job ZIP is empty")
            files: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
            for info in infos:
                path = _zip_member_path(info.filename)
                first = path.parts[0]
                if wrapper is None:
                    wrapper = first
                elif first.casefold() != wrapper.casefold():
                    raise WorkerError("job ZIP must contain exactly one top-level job folder")
                if info.is_dir():
                    continue
                if len(path.parts) < 2:
                    raise WorkerError("job files must be inside the top-level job folder")
                inner = PurePosixPath(*path.parts[1:])
                key = inner.as_posix().casefold()
                if key in seen:
                    raise WorkerError(f"duplicate ZIP entry: {inner.as_posix()}")
                seen.add(key)
                files.append((info, inner))
            if wrapper is None or not wrapper.startswith("OCR_JOB_") or not files:
                raise WorkerError("invalid top-level job folder")
            for info, inner in files:
                target = temporary.joinpath(*inner.parts)
                ensure_directory(home_root, target.parent)
                with archive.open(info, "r") as source, target.open("xb") as destination:
                    shutil.copyfileobj(source, destination, length=1024 * 1024)

        job = load_job(temporary)
        expected_wrapper = f"OCR_JOB_{job.job_id}"
        if wrapper != expected_wrapper:
            raise WorkerError(f"job ZIP folder must be {expected_wrapper}")
        target = jobs_root / job.job_id
        _resolve_contained(home_root, target, must_exist=False, description="job import target")
        if target.exists():
            raise WorkerError(f"job_id already exists and will not be overwritten: {job.job_id}")
        os.replace(temporary, target)
        return {
            "job_id": job.job_id,
            "assets": len(job.assets),
            "job_directory": f"jobs/{job.job_id}",
            "source_zip": archive_path.relative_to(home_root).as_posix(),
        }
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)


def import_jobs(home_root: Path, requested_zip: str | None) -> int:
    inbox = home_root / "inbox"
    ensure_directory(home_root, inbox)
    if requested_zip:
        candidate = Path(requested_zip)
        if not candidate.is_absolute():
            candidate = home_root / candidate
        archives = [candidate]
    else:
        archives = sorted(inbox.glob("OCR_JOB_*.zip"), key=lambda path: path.name.casefold())
    if not archives:
        raise WorkerError("no OCR_JOB_*.zip was found in inbox")
    if requested_zip is None and len(archives) > 1:
        names = [archive.name for archive in archives]
        raise WorkerError(f"multiple job ZIPs found in inbox; use --job-zip to choose one: {names}")
    imported: list[dict[str, Any]] = []
    for archive in archives:
        imported.append(import_one_job(home_root, archive))
    print(json.dumps({"status": "IMPORTED", "jobs": imported}, ensure_ascii=False, indent=2))
    return 0


def visible_text(markdown: str) -> str:
    text = re.sub(r"<!--.*?-->", " ", markdown, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"!\[[^]]*]\([^)]*\)", " ", text)
    text = re.sub(r"[`#*_>|~-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def repetition_detected(markdown: str) -> bool:
    lines = [re.sub(r"\s+", " ", visible_text(line)).casefold() for line in markdown.splitlines()]
    lines = [line for line in lines if len(line) >= 3]
    if len(lines) < 12:
        return False
    counts = Counter(lines)
    if max(counts.values(), default=0) >= max(8, int(len(lines) * 0.2)):
        return True
    pairs = Counter(zip(lines, lines[1:]))
    return max(pairs.values(), default=0) >= max(6, int(len(lines) * 0.15))


def validate_markdown(markdown: str, *, required_content: bool) -> None:
    if "\x00" in markdown:
        raise WorkerError("Markdown contains NUL")
    if "\ufffd" in markdown:
        raise WorkerError("Markdown contains Unicode replacement characters")
    if required_content and not visible_text(markdown):
        raise WorkerError("required-content Markdown is empty")
    lowered = markdown.casefold()
    for tag in ("table", "tr", "td", "th"):
        if lowered.count(f"<{tag}") != lowered.count(f"</{tag}>"):
            raise WorkerError(f"unbalanced HTML table tag: {tag}")
    if repetition_detected(markdown):
        raise WorkerError("excessive within-asset repetition")


def output_paths(job_root: Path, asset: Asset) -> tuple[Path, Path]:
    markdown = job_root / "output" / asset.ticker / "markdown" / f"{asset.asset_id}.md"
    result_json = job_root / "output" / asset.ticker / "json" / f"{asset.asset_id}_res.json"
    return markdown, result_json


def worker_metadata(job: Job, asset: Asset) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "required_worker_profile": REQUIRED_WORKER_PROFILE,
        "job_id": job.job_id,
        "source_id": asset.source_id,
        "ticker": asset.ticker,
        "asset_id": asset.asset_id,
        "input_path": asset.input_path,
        "input_sha256": asset.sha256,
        "config_sha256": sha256_json(EXPECTED_OCR_CONFIG),
    }
    if asset.page_number is not None:
        value["page_number"] = asset.page_number
    return value


def validate_result_payload(payload: Any, job: Job, asset: Asset) -> None:
    if not isinstance(payload, dict):
        raise WorkerError("OCR JSON root must be an object")
    if payload.get("_worker") != worker_metadata(job, asset):
        raise WorkerError("OCR JSON checkpoint metadata mismatch")


def validate_checkpoint(job_root: Path, job: Job, asset: Asset) -> bool:
    markdown_path, json_path = output_paths(job_root, asset)
    if not markdown_path.is_file() or not json_path.is_file():
        return False
    try:
        markdown_path = _resolve_contained(
            job_root, markdown_path, description="Markdown checkpoint"
        )
        json_path = _resolve_contained(job_root, json_path, description="JSON checkpoint")
        markdown = markdown_path.read_text(encoding="utf-8")
        validate_markdown(markdown, required_content=asset.required_content)
        validate_result_payload(read_json(json_path), job, asset)
    except (WorkerError, OSError, UnicodeError):
        return False
    return True


def _scrub_paths(value: Any, replacement: str) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(item, str) and (key.casefold().endswith("path") or key.casefold().endswith("_path")):
                cleaned[key] = replacement
            else:
                cleaned[key] = _scrub_paths(item, replacement)
        return cleaned
    if isinstance(value, list):
        return [_scrub_paths(item, replacement) for item in value]
    return value


def _redact(text: str, home_root: Path) -> str:
    redacted = text.replace(str(home_root), "<WORKER_HOME>")
    redacted = redacted.replace(str(Path.home()), "<HOME>")
    redacted = re.sub(r"(?i)[A-Z]:\\Users\\[^\\\s]+", r"C:\\Users\\<USER>", redacted)
    return redacted[:1000]


def _with_engine_stderr_tail(reason: str, lines: Iterable[str]) -> str:
    compact = [
        re.sub(r"\s+", " ", str(line)).strip()
        for line in lines
        if str(line).strip()
    ]
    if not compact:
        return reason
    tail = " | ".join(compact[-ENGINE_STDERR_TAIL_MAX_LINES:])
    if len(tail) > ENGINE_STDERR_TAIL_MAX_CHARS:
        marker = "..."
        tail = marker + tail[-(ENGINE_STDERR_TAIL_MAX_CHARS - len(marker)):]
    return f"{reason}; engine stderr tail: {tail}"


def emit(event: str, **details: Any) -> None:
    print(json.dumps({"event": event, "at": now_iso(), **details}, ensure_ascii=False), flush=True)


def verify_runtime(profile: str) -> dict[str, Any]:
    if profile not in {"gpu", "cpu"}:
        raise WorkerError("runtime profile must be cpu or gpu")
    paddle_distribution = "paddlepaddle-gpu" if profile == "gpu" else "paddlepaddle"
    expected = {paddle_distribution: "3.3.1", **COMMON_PACKAGES}
    try:
        actual = {name: importlib.metadata.version(name) for name in expected}
    except importlib.metadata.PackageNotFoundError as exc:
        raise WorkerError(f"runtime package missing: {exc.name}") from exc
    if actual != expected:
        raise WorkerError(f"runtime package mismatch: {actual}")
    try:
        import paddle
    except Exception as exc:
        raise WorkerError(f"cannot import Paddle runtime: {exc}") from exc
    compiled = bool(paddle.device.is_compiled_with_cuda())
    if profile == "gpu" and not compiled:
        raise WorkerError("GPU profile is not CUDA-compiled")
    if profile == "cpu" and compiled:
        raise WorkerError("CPU profile unexpectedly contains a CUDA Paddle build")
    return {
        "profile": profile,
        "packages": actual,
        "cuda_compiled": compiled,
        "device_before_engine": str(paddle.device.get_device()),
    }


def enforce_bos_only_model_source() -> dict[str, Any]:
    """Restrict pinned PaddleX 3.7.2 to the approved BOS model host.

    ``PADDLE_PDX_MODEL_SOURCE`` only changes PaddleX's preferred host.  PaddleX
    otherwise keeps the remaining hosts as automatic fallbacks, which would
    make a transient BOS failure silently change the provenance of the shared
    model cache.  The package version is locked, so fail closed if its internal
    model-manager contract is not the one verified by this Worker Home.
    """

    configured = os.environ.get("PADDLE_PDX_MODEL_SOURCE", "").strip().casefold()
    if configured != "bos":
        raise WorkerError("model source must be locked to BOS")
    try:
        module = importlib.import_module("paddlex.inference.utils.official_models")
        manager = module.official_models
        bos_hoster = module._BosModelHoster
        if not hasattr(manager, "hoster_candidates") or not hasattr(manager, "_hosters"):
            raise AttributeError("PaddleX model manager contract changed")
        manager.hoster_candidates = [bos_hoster]
        manager._hosters = None
    except Exception as exc:
        raise WorkerError(f"cannot enforce BOS-only model source: {exc}") from exc
    return {"source": "bos", "automatic_fallbacks": []}


def _run_asset(pipeline: Any, home_root: Path, job_root: Path, job: Job, asset: Asset) -> None:
    markdown_path, json_path = output_paths(job_root, asset)
    ensure_directory(home_root, markdown_path.parent)
    ensure_directory(home_root, json_path.parent)
    for stale in (markdown_path, json_path):
        if stale.exists():
            _resolve_contained(job_root, stale, description="stale checkpoint")
            stale.unlink()

    temporary_root = job_root / "state" / "_engine_tmp" / f"{asset.ticker}_{asset.asset_id}_{os.getpid()}"
    ensure_directory(home_root, temporary_root.parent)
    _resolve_contained(home_root, temporary_root, must_exist=False, description="engine temporary directory")
    if temporary_root.exists():
        shutil.rmtree(temporary_root)
    temporary_root.mkdir(parents=True)
    try:
        input_path = resolve_inside(job_root, asset.input_path)
        results = list(
            pipeline.predict_iter(
                [str(input_path)],
                max_new_tokens=EXPECTED_OCR_CONFIG["max_new_tokens"],
            )
        )
        if len(results) != 1:
            raise WorkerError(f"engine returned {len(results)} results for one asset")
        result = results[0]
        result.save_to_markdown(save_path=temporary_root, pretty=True, show_formula_number=False)
        result.save_to_json(save_path=temporary_root, ensure_ascii=False)
        markdown_candidates = list(temporary_root.glob("*.md"))
        json_candidates = list(temporary_root.glob("*.json"))
        if len(markdown_candidates) != 1 or len(json_candidates) != 1:
            raise WorkerError("engine did not create exactly one Markdown and one JSON file")
        markdown = markdown_candidates[0].read_text(encoding="utf-8")
        validate_markdown(markdown, required_content=asset.required_content)
        payload = _scrub_paths(read_json(json_candidates[0]), asset.input_path)
        if not isinstance(payload, dict):
            raise WorkerError("OCR JSON root must be an object")
        payload["_worker"] = worker_metadata(job, asset)
        validate_result_payload(payload, job, asset)

        json_temporary = json_path.with_name(f".{json_path.name}.{os.getpid()}.tmp")
        markdown_temporary = markdown_path.with_name(f".{markdown_path.name}.{os.getpid()}.tmp")
        json_temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        markdown_temporary.write_text(markdown.rstrip() + "\n", encoding="utf-8")
        os.replace(json_temporary, json_path)
        os.replace(markdown_temporary, markdown_path)
        if not validate_checkpoint(job_root, job, asset):
            raise WorkerError("new checkpoint failed read-back validation")
    finally:
        if temporary_root.exists():
            shutil.rmtree(temporary_root, ignore_errors=True)


def engine_main(home_root: Path, job_root: Path, selection_path: Path, profile: str) -> int:
    runtime = verify_runtime(profile)
    model_source = enforce_bos_only_model_source()
    job = load_job(job_root)
    raw_selection = read_json(selection_path)
    if not isinstance(raw_selection, list) or not raw_selection:
        raise WorkerError("engine selection must be a non-empty list")
    assets_by_key = job.assets_by_key
    selection: list[Asset] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_selection:
        if not isinstance(item, dict) or set(item) != {"source_id", "asset_id"}:
            raise WorkerError("invalid engine selection entry")
        key = (item["source_id"], item["asset_id"])
        asset = assets_by_key.get(key)
        if asset is None or key in seen:
            raise WorkerError("engine selection references an unknown or duplicate asset")
        selection.append(asset)
        seen.add(key)

    emit("model_loading", runtime=runtime, model_source=model_source)
    import paddle
    from paddleocr import PaddleOCRVL

    requested_device = "gpu:0" if profile == "gpu" else "cpu"
    try:
        paddle.set_device(requested_device)
    except Exception as exc:
        raise WorkerError(f"cannot activate {requested_device}: {exc}") from exc
    pipeline = PaddleOCRVL(
        device=requested_device,
        pipeline_version=EXPECTED_OCR_CONFIG["pipeline_version"],
        vl_rec_max_concurrency=EXPECTED_OCR_CONFIG["vl_rec_max_concurrency"],
        use_ocr_for_image_block=EXPECTED_OCR_CONFIG["use_ocr_for_image_block"],
        use_chart_recognition=EXPECTED_OCR_CONFIG["use_chart_recognition"],
        markdown_ignore_labels=EXPECTED_OCR_CONFIG["markdown_ignore_labels"],
    )
    emit("model_ready", device=str(paddle.device.get_device()))
    for asset in selection:
        started = time.monotonic()
        emit(
            "asset_started",
            source_id=asset.source_id,
            ticker=asset.ticker,
            asset_id=asset.asset_id,
            page_number=asset.page_number,
        )
        try:
            _run_asset(pipeline, home_root, job_root, job, asset)
        except Exception as exc:
            emit(
                "asset_failed",
                source_id=asset.source_id,
                ticker=asset.ticker,
                asset_id=asset.asset_id,
                page_number=asset.page_number,
                reason=_redact(f"{type(exc).__name__}: {exc}", home_root),
            )
            return 20
        emit(
            "asset_completed",
            source_id=asset.source_id,
            ticker=asset.ticker,
            asset_id=asset.asset_id,
            page_number=asset.page_number,
            seconds=round(time.monotonic() - started, 3),
        )
    return 0


def append_log(home_root: Path, log_path: Path, payload: dict[str, Any]) -> None:
    ensure_directory(home_root, log_path.parent)
    _resolve_contained(home_root, log_path, must_exist=False, description="run log")
    with log_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        handle.flush()


def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        process.kill()


def _stream_reader(stream: Any, channel: str, messages: queue.Queue[tuple[str, str | None]]) -> None:
    try:
        for line in stream:
            messages.put((channel, line.rstrip("\r\n")))
    finally:
        messages.put((channel, None))


def native_model_cache_environment(home_root: Path) -> dict[str, str]:
    """Build the engine environment with a Unicode-safe local model path.

    Paddle Inference 3.3.1 on Windows may fail to open PIR JSON models when
    their *absolute* path contains non-ASCII characters. The cache remains
    physically contained in Worker Home, while the native engine receives a
    path relative to an explicit Worker Home working directory.
    """

    cache_root = home_root / ".runtime" / "paddlex_cache"
    if not cache_root.is_dir():
        raise WorkerError("shared model cache directory is missing")
    _resolve_contained(home_root, cache_root, description="shared model cache")
    environment = os.environ.copy()
    environment["PADDLE_PDX_CACHE_HOME"] = ".runtime/paddlex_cache"
    return environment


def supervise_engine(
    home_root: Path,
    job_root: Path,
    job: Job,
    assets: list[Asset],
    profile: str,
) -> dict[str, Any]:
    state_root = job_root / "state"
    ensure_directory(home_root, state_root)
    selection_path = state_root / f"engine_selection_{os.getpid()}_{uuid.uuid4().hex}.json"
    atomic_write_json(
        selection_path,
        [{"source_id": asset.source_id, "asset_id": asset.asset_id} for asset in assets],
    )
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "_engine",
        "--home-root",
        str(home_root),
        "--job-root",
        str(job_root),
        "--selection",
        str(selection_path),
        "--profile",
        profile,
    ]
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=native_model_cache_environment(home_root),
        cwd=str(home_root),
        creationflags=flags,
    )
    messages: queue.Queue[tuple[str, str | None]] = queue.Queue()
    assert process.stdout is not None and process.stderr is not None
    readers: list[threading.Thread] = []
    for stream, channel in ((process.stdout, "stdout"), (process.stderr, "stderr")):
        reader = threading.Thread(target=_stream_reader, args=(stream, channel, messages), daemon=True)
        reader.start()
        readers.append(reader)

    log_path = job_root / "logs" / "run.jsonl"
    completed: set[tuple[str, str]] = set()
    current: tuple[str, str] | None = None
    failure: dict[str, Any] | None = None
    model_ready = False
    model_deadline = time.monotonic() + 3600
    asset_deadline: float | None = None
    closed: set[str] = set()
    stderr_tail: deque[str] = deque(maxlen=ENGINE_STDERR_TAIL_MAX_LINES)
    try:
        while True:
            try:
                channel, line = messages.get(timeout=0.5)
            except queue.Empty:
                channel, line = "", ""
            if line is None and channel:
                closed.add(channel)
            elif line:
                if channel == "stderr":
                    redacted_line = _redact(line, home_root)
                    stderr_tail.append(redacted_line)
                    append_log(home_root, log_path, {
                        "event": "engine_stderr",
                        "at": now_iso(),
                        "message": redacted_line,
                    })
                else:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        event = {
                            "event": "engine_stdout",
                            "at": now_iso(),
                            "message": _redact(line, home_root),
                        }
                    append_log(home_root, log_path, event)
                    print(json.dumps(event, ensure_ascii=False), flush=True)
                    name = event.get("event")
                    if name == "model_ready":
                        model_ready = True
                    elif name == "asset_started":
                        current = (event["source_id"], event["asset_id"])
                        asset_deadline = time.monotonic() + EXPECTED_OCR_CONFIG["page_timeout_seconds"]
                    elif name == "asset_completed":
                        completed.add((event["source_id"], event["asset_id"]))
                        current = None
                        asset_deadline = None
                    elif name == "asset_failed":
                        asset = job.assets_by_key[(event["source_id"], event["asset_id"])]
                        failure = failure_record(asset, event.get("reason", "engine failure"))
                        _kill_process_tree(process)

            now = time.monotonic()
            if not model_ready and now > model_deadline and failure is None:
                failure = failure_record(
                    assets[0],
                    _with_engine_stderr_tail(
                        "model initialization exceeded 60 minutes",
                        stderr_tail,
                    ),
                )
                _kill_process_tree(process)
            if current is not None and asset_deadline is not None and now > asset_deadline and failure is None:
                asset = job.assets_by_key[current]
                failure = failure_record(
                    asset,
                    f"asset timeout after {EXPECTED_OCR_CONFIG['page_timeout_seconds']} seconds",
                )
                _kill_process_tree(process)
            if process.poll() is not None and {"stdout", "stderr"}.issubset(closed) and messages.empty():
                break

        return_code = process.wait()
        expected_keys = [asset.key for asset in assets]
        missing = [key for key in expected_keys if key not in completed]
        if failure is None and (return_code != 0 or missing):
            key = missing[0] if missing else expected_keys[0]
            failure = failure_record(
                job.assets_by_key[key],
                _with_engine_stderr_tail(
                    f"engine exited {return_code} without a valid checkpoint",
                    stderr_tail,
                ),
            )
        return {"completed": completed, "failure": failure, "return_code": return_code}
    finally:
        if process.poll() is None:
            _kill_process_tree(process)
        for reader in readers:
            reader.join(timeout=1)
        process.stdout.close()
        process.stderr.close()
        selection_path.unlink(missing_ok=True)


def failure_record(asset: Asset, reason: str) -> dict[str, Any]:
    return {
        "source_id": asset.source_id,
        "ticker": asset.ticker,
        "asset_id": asset.asset_id,
        "page_number": asset.page_number,
        "reason": reason,
        "at": now_iso(),
    }


def write_failures(home_root: Path, job_root: Path, job: Job, failures: dict[tuple[str, str], dict[str, Any]]) -> None:
    ordered = sorted(failures.values(), key=lambda row: (row["ticker"], row["asset_id"].casefold()))
    state_root = job_root / "state"
    ensure_directory(home_root, state_root)
    atomic_write_json(state_root / "failed_images.json", {"job_id": job.job_id, "failures": ordered})


def load_failures(job_root: Path) -> dict[tuple[str, str], dict[str, Any]]:
    path = job_root / "state" / "failed_images.json"
    if not path.is_file():
        return {}
    try:
        path = _resolve_contained(job_root, path, description="failure state")
        payload = read_json(path)
        rows = payload.get("failures", []) if isinstance(payload, dict) else []
        return {
            (row["source_id"], row["asset_id"]): row
            for row in rows
            if isinstance(row, dict)
        }
    except (WorkerError, KeyError, TypeError, ValueError):
        return {}


def get_job_root(home_root: Path, job_id: str) -> Path:
    job_id = _safe_name(job_id, SAFE_JOB_ID, "job_id")
    jobs_root = home_root / "jobs"
    if not jobs_root.is_dir():
        raise WorkerError("jobs directory is missing or unsafe")
    _resolve_contained(home_root, jobs_root, description="jobs directory")
    job_root = jobs_root / job_id
    if not job_root.is_dir():
        raise WorkerError(f"job is not imported: {job_id}")
    return _resolve_contained(home_root, job_root, description="job directory")


def run_job(home_root: Path, job_id: str, profile: str) -> int:
    job_root = get_job_root(home_root, job_id)
    job = load_job(job_root)
    runtime = verify_runtime(profile)
    failures = load_failures(job_root)
    attempted: set[tuple[str, str]] = set()

    probe_asset = job.assets_by_key[job.probe_key]
    if not validate_checkpoint(job_root, job, probe_asset):
        result = supervise_engine(home_root, job_root, job, [probe_asset], profile)
        attempted.add(probe_asset.key)
        if result["failure"] is not None or not validate_checkpoint(job_root, job, probe_asset):
            failure = result["failure"] or failure_record(probe_asset, "probe checkpoint validation failed")
            failures[probe_asset.key] = failure
            write_failures(home_root, job_root, job, failures)
            print(json.dumps({
                "status": "PROBE_FAILED",
                "job_id": job.job_id,
                "failure": failure,
            }, ensure_ascii=False, indent=2))
            return 2
    failures.pop(probe_asset.key, None)

    remaining = [
        asset for asset in job.assets
        if asset.key != probe_asset.key and not validate_checkpoint(job_root, job, asset)
    ]
    while remaining:
        batch = [asset for asset in remaining if asset.key not in attempted]
        if not batch:
            break
        result = supervise_engine(home_root, job_root, job, batch, profile)
        for key in result["completed"]:
            attempted.add(key)
            failures.pop(key, None)
        failure = result["failure"]
        if failure is not None:
            key = (failure["source_id"], failure["asset_id"])
            attempted.add(key)
            failures[key] = failure
        remaining = [
            asset for asset in job.assets
            if asset.key != probe_asset.key
            and asset.key not in attempted
            and not validate_checkpoint(job_root, job, asset)
        ]

    for asset in job.assets:
        if validate_checkpoint(job_root, job, asset):
            failures.pop(asset.key, None)
        elif asset.key not in failures:
            failures[asset.key] = failure_record(asset, "not completed in this invocation")
    write_failures(home_root, job_root, job, failures)
    completed = sum(validate_checkpoint(job_root, job, asset) for asset in job.assets)
    summary = {
        "status": "COMPLETE" if completed == len(job.assets) else "PARTIAL",
        "job_id": job.job_id,
        "completed_assets": completed,
        "failed_assets": len(job.assets) - completed,
        "total_assets": len(job.assets),
        "runtime": runtime,
        "quality_pass_claimed": False,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def status_jobs(home_root: Path) -> int:
    jobs_root = home_root / "jobs"
    rows: list[dict[str, Any]] = []
    if jobs_root.is_dir():
        jobs_root = _resolve_contained(home_root, jobs_root, description="jobs directory")
        for job_root in sorted(jobs_root.iterdir(), key=lambda path: path.name.casefold()):
            if not job_root.is_dir() or job_root.name.startswith("."):
                continue
            try:
                job_root = _resolve_contained(home_root, job_root, description="job directory")
                job = load_job(job_root)
                completed = sum(validate_checkpoint(job_root, job, asset) for asset in job.assets)
                result_zip = home_root / "outbox" / f"RESULT_{job.job_id}.zip"
                rows.append({
                    "job_id": job.job_id,
                    "status": "COMPLETE" if completed == len(job.assets) else ("NOT_STARTED" if completed == 0 else "PARTIAL"),
                    "completed_assets": completed,
                    "pending_or_failed_assets": len(job.assets) - completed,
                    "total_assets": len(job.assets),
                    "result_zip": result_zip.relative_to(home_root).as_posix() if result_zip.is_file() else None,
                })
            except WorkerError as exc:
                rows.append({"job_id": job_root.name, "status": "INVALID", "error": _redact(str(exc), home_root)})
    print(json.dumps({
        "status": "OK",
        "active_runtime_profile": os.environ.get("OCR_WORKER_PROFILE"),
        "jobs": rows,
    }, ensure_ascii=False, indent=2))
    return 0


def machine_report(home_root: Path, profile: str) -> dict[str, Any]:
    try:
        import psutil

        ram_gib = round(psutil.virtual_memory().total / (1024 ** 3), 1)
        free_disk_gib = round(psutil.disk_usage(home_root).free / (1024 ** 3), 1)
    except Exception:
        ram_gib = None
        free_disk_gib = None
    gpus: list[dict[str, Any]] = []
    try:
        output = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version,compute_cap", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=True,
        ).stdout
        for line in output.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) >= 4:
                gpus.append({
                    "name": parts[0],
                    "memory_mib": int(parts[1]),
                    "driver": parts[2],
                    "compute_capability": parts[3],
                })
    except Exception:
        pass
    return {
        "captured_at": now_iso(),
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
        },
        "python": platform.python_version(),
        "runtime": verify_runtime(profile),
        "ram_gib": ram_gib,
        "free_disk_gib": free_disk_gib,
        "gpus": gpus,
    }


def _safe_zip_name(name: str) -> str:
    normalized = safe_relative_path(name)
    folded = normalized.casefold()
    if folded == "input" or folded.startswith(("input/", ".runtime/")):
        raise WorkerError(f"forbidden result ZIP entry: {name}")
    return normalized


def pack_job(home_root: Path, job_id: str, profile: str) -> int:
    job_root = get_job_root(home_root, job_id)
    job = load_job(job_root)
    verify_runtime(profile)
    failures = load_failures(job_root)
    completed_assets: list[Asset] = []
    output_files: list[dict[str, Any]] = []
    archive_files: list[tuple[Path, str]] = []
    for asset in job.assets:
        if not validate_checkpoint(job_root, job, asset):
            continue
        completed_assets.append(asset)
        markdown_path, json_path = output_paths(job_root, asset)
        for path, kind in ((markdown_path, "markdown"), (json_path, "json")):
            archive_name = path.relative_to(job_root).as_posix()
            path = _resolve_contained(job_root, path, description=f"{kind} pack input")
            file_entry: dict[str, Any] = {
                "source_id": asset.source_id,
                "ticker": asset.ticker,
                "asset_id": asset.asset_id,
                "kind": kind,
                "path": archive_name,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            if asset.page_number is not None:
                file_entry["page_number"] = asset.page_number
            output_files.append(file_entry)
            archive_files.append((path, archive_name))
        failures.pop(asset.key, None)
    completed_keys = {asset.key for asset in completed_assets}
    for asset in job.assets:
        if asset.key not in completed_keys and asset.key not in failures:
            failures[asset.key] = failure_record(asset, "no valid checkpoint")
    write_failures(home_root, job_root, job, failures)

    state_root = job_root / "state"
    ensure_directory(home_root, state_root)
    report_path = state_root / "machine_report.json"
    failure_path = state_root / "failed_images.json"
    manifest_path = state_root / "result_manifest.json"
    atomic_write_json(report_path, machine_report(home_root, profile))
    result_manifest = {
        "schema_version": SCHEMA_VERSION,
        "required_worker_profile": REQUIRED_WORKER_PROFILE,
        "job_id": job.job_id,
        "status": "COMPLETE" if len(completed_assets) == len(job.assets) else "PARTIAL",
        "created_at": now_iso(),
        "sources": [source.portable() for source in job.sources],
        "total_assets": len(job.assets),
        "completed_assets": len(completed_assets),
        "failed_assets": len(job.assets) - len(completed_assets),
        "config_sha256": sha256_json(EXPECTED_OCR_CONFIG),
        "ocr_config": EXPECTED_OCR_CONFIG,
        "runtime_profile": profile,
        "quality_pass_claimed": False,
        "files": output_files,
    }
    atomic_write_json(manifest_path, result_manifest)
    archive_files.extend([
        (manifest_path, "result_manifest.json"),
        (report_path, "machine_report.json"),
        (failure_path, "failed_images.json"),
    ])
    log_path = job_root / "logs" / "run.jsonl"
    if log_path.is_file():
        archive_files.append((
            _resolve_contained(job_root, log_path, description="run log"),
            "logs/run.jsonl",
        ))

    seen: set[str] = set()
    normalized: list[tuple[Path, str]] = []
    for path, name in archive_files:
        name = _safe_zip_name(name)
        if not path.is_file():
            raise WorkerError(f"unsafe or missing pack input: {name}")
        path = _resolve_contained(job_root, path, description="result pack input")
        key = name.casefold()
        if key in seen:
            raise WorkerError(f"duplicate result ZIP entry: {name}")
        seen.add(key)
        normalized.append((path, name))

    outbox = home_root / "outbox"
    ensure_directory(home_root, outbox)
    target = outbox / f"RESULT_{job.job_id}.zip"
    _resolve_contained(home_root, target, must_exist=False, description="result ZIP target")
    temporary = outbox / f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path, name in sorted(normalized, key=lambda row: row[1].casefold()):
                archive.write(path, arcname=name)
        with zipfile.ZipFile(temporary, "r") as archive:
            names = archive.namelist()
            if len(names) != len(seen) or {name.casefold() for name in names} != seen:
                raise WorkerError("result ZIP read-back allowlist mismatch")
            broken = archive.testzip()
            if broken is not None:
                raise WorkerError(f"result ZIP CRC failure: {broken}")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    print(json.dumps({
        "status": result_manifest["status"],
        "job_id": job.job_id,
        "completed_assets": len(completed_assets),
        "failed_assets": len(job.assets) - len(completed_assets),
        "result_zip": target.relative_to(home_root).as_posix(),
        "uploaded": False,
    }, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Persistent raw PaddleOCR-VL Worker Home")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("--home-root", required=True)
    import_parser.add_argument("--job-zip")

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--home-root", required=True)

    for command_name in ("run", "pack"):
        command_parser = subparsers.add_parser(command_name)
        command_parser.add_argument("--home-root", required=True)
        command_parser.add_argument("--job-id", required=True)
        command_parser.add_argument("--profile", required=True, choices=("gpu", "cpu"))

    engine_parser = subparsers.add_parser("_engine")
    engine_parser.add_argument("--home-root", required=True)
    engine_parser.add_argument("--job-root", required=True)
    engine_parser.add_argument("--selection", required=True)
    engine_parser.add_argument("--profile", required=True, choices=("gpu", "cpu"))

    args = parser.parse_args()
    home_root: Path | None = None
    try:
        home_root = Path(args.home_root).resolve(strict=True)
        if not home_root.is_dir():
            raise WorkerError("Worker Home root is unsafe")
        if args.command == "import":
            return import_jobs(home_root, args.job_zip)
        if args.command == "status":
            return status_jobs(home_root)
        if args.command == "run":
            return run_job(home_root, args.job_id, args.profile)
        if args.command == "pack":
            return pack_job(home_root, args.job_id, args.profile)
        job_root = Path(args.job_root).resolve(strict=True)
        try:
            job_root.relative_to(home_root)
        except ValueError as exc:
            raise WorkerError("engine job root escapes Worker Home") from exc
        selection_path = Path(args.selection).resolve(strict=True)
        try:
            selection_path.relative_to(job_root)
        except ValueError as exc:
            raise WorkerError("engine selection escapes job") from exc
        return engine_main(home_root, job_root, selection_path, args.profile)
    except WorkerError as exc:
        base = home_root if home_root is not None else Path.cwd()
        print(json.dumps({"status": "ERROR", "error": _redact(str(exc), base)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except Exception as exc:
        base = home_root if home_root is not None else Path.cwd()
        message = _redact(f"{type(exc).__name__}: {exc}", base)
        print(json.dumps({"status": "ERROR", "error": message}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
