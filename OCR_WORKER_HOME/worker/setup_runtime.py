from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Sequence


SCHEMA_VERSION = "2.1"
VALID_PROFILES = frozenset({"cpu", "gpu"})
PROFILE_PATH_CODES = {"cpu": "c", "gpu": "g"}
EXIT_CONFIGURATION = 2
EXIT_INSTALL = 3
EXIT_VERIFICATION = 4
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_PIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==[^\s;]+$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[A-Za-z0-9.+-]*)?$")
_PASSTHROUGH_ENVIRONMENT = frozenset(
    {
        "ALL_PROXY",
        "COMSPEC",
        "CURL_CA_BUNDLE",
        "CUDA_VISIBLE_DEVICES",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LC_ALL",
        "NO_PROXY",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_IDENTIFIER",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "WINDIR",
        "UV_NATIVE_TLS",
        "UV_SYSTEM_CERTS",
    }
)


class SetupError(RuntimeError):
    """A setup failure with a stable stage and process exit code."""

    def __init__(self, stage: str, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.stage = stage
        self.exit_code = exit_code


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise SetupError("configuration", message, EXIT_CONFIGURATION)


def _resolved_home(path: str | os.PathLike[str]) -> Path:
    candidate = Path(path).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise SetupError(
            "configuration",
            f"Worker Home is unavailable: {candidate}: {exc}",
            EXIT_CONFIGURATION,
        ) from exc
    if not resolved.is_dir():
        raise SetupError(
            "configuration",
            f"Worker Home is not a directory: {resolved}",
            EXIT_CONFIGURATION,
        )
    return resolved


def resolve_inside(
    home_root: Path,
    path: str | os.PathLike[str],
    *,
    description: str,
    must_exist: bool = True,
) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = home_root / candidate
    try:
        resolved = candidate.resolve(strict=must_exist)
    except OSError as exc:
        raise SetupError(
            "configuration",
            f"{description} is unavailable: {candidate}: {exc}",
            EXIT_CONFIGURATION,
        ) from exc
    try:
        common = os.path.commonpath(
            [os.path.normcase(str(home_root)), os.path.normcase(str(resolved))]
        )
    except ValueError as exc:
        raise SetupError(
            "configuration",
            f"{description} escapes Worker Home: {resolved}",
            EXIT_CONFIGURATION,
        ) from exc
    if common != os.path.normcase(str(home_root)):
        raise SetupError(
            "configuration",
            f"{description} escapes Worker Home: {resolved}",
            EXIT_CONFIGURATION,
        )
    return resolved


def home_relative(home_root: Path, path: Path) -> str:
    resolved = resolve_inside(
        home_root,
        path,
        description="path",
        must_exist=False,
    )
    return resolved.relative_to(home_root).as_posix()


def _load_json(path: Path, *, description: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SetupError(
            "configuration",
            f"{description} is invalid: {exc}",
            EXIT_CONFIGURATION,
        ) from exc
    if not isinstance(payload, dict):
        raise SetupError(
            "configuration",
            f"{description} must contain a JSON object",
            EXIT_CONFIGURATION,
        )
    return payload


def _validate_runtime_lock(
    home_root: Path,
    runtime_lock_path: Path,
    profile: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    lock = _load_json(runtime_lock_path, description="runtime lock")
    if lock.get("schema_version") != SCHEMA_VERSION:
        raise SetupError(
            "configuration",
            f"runtime lock schema must be {SCHEMA_VERSION}",
            EXIT_CONFIGURATION,
        )
    uv_lock = lock.get("uv")
    if (
        not isinstance(uv_lock, dict)
        or not isinstance(uv_lock.get("version"), str)
        or _VERSION.fullmatch(uv_lock["version"]) is None
        or not isinstance(uv_lock.get("installer_url"), str)
        or not uv_lock["installer_url"].startswith("https://")
        or not isinstance(uv_lock.get("installer_sha256"), str)
        or _SHA256.fullmatch(uv_lock["installer_sha256"]) is None
    ):
        raise SetupError(
            "configuration",
            "runtime lock uv contract is invalid",
            EXIT_CONFIGURATION,
        )
    python_lock = lock.get("python")
    if (
        not isinstance(python_lock, dict)
        or python_lock.get("implementation") != "CPython"
        or not isinstance(python_lock.get("version"), str)
        or _VERSION.fullmatch(python_lock["version"]) is None
        or python_lock.get("architecture") != "64bit"
    ):
        raise SetupError(
            "configuration",
            "runtime lock Python contract is invalid",
            EXIT_CONFIGURATION,
        )
    profiles = lock.get("profiles")
    raw_groups = lock.get("install_groups")
    if not isinstance(profiles, dict) or not isinstance(raw_groups, dict):
        raise SetupError(
            "configuration",
            "runtime lock profiles/install_groups are invalid",
            EXIT_CONFIGURATION,
        )
    profile_lock = profiles.get(profile)
    if not isinstance(profile_lock, dict):
        raise SetupError(
            "configuration",
            f"runtime lock has no {profile} profile",
            EXIT_CONFIGURATION,
        )
    group_names = profile_lock.get("install_groups")
    if not isinstance(group_names, list) or not group_names:
        raise SetupError(
            "configuration",
            f"runtime lock {profile} install_groups are invalid",
            EXIT_CONFIGURATION,
        )
    groups: list[dict[str, Any]] = []
    seen: set[str] = set()
    worker_root = runtime_lock_path.parent
    locked_names: dict[str, str] = {}
    for raw_name in group_names:
        if not isinstance(raw_name, str) or raw_name in seen:
            raise SetupError(
                "configuration",
                f"runtime lock {profile} has duplicate/invalid install group",
                EXIT_CONFIGURATION,
            )
        seen.add(raw_name)
        raw_group = raw_groups.get(raw_name)
        if not isinstance(raw_group, dict):
            raise SetupError(
                "configuration",
                f"runtime lock install group is missing: {raw_name}",
                EXIT_CONFIGURATION,
            )
        requirements_file = raw_group.get("requirements_file")
        index_url = raw_group.get("index_url")
        if (
            not isinstance(requirements_file, str)
            or Path(requirements_file).name != requirements_file
            or not isinstance(index_url, str)
            or not index_url.startswith("https://")
            or raw_group.get("no_deps") is not True
            or "extra_index_url" in raw_group
            or "extra_index_urls" in raw_group
        ):
            raise SetupError(
                "configuration",
                f"runtime lock install group is not source-exclusive: {raw_name}",
                EXIT_CONFIGURATION,
            )
        requirements_path = resolve_inside(
            home_root,
            worker_root / requirements_file,
            description=f"requirements file {requirements_file}",
        )
        if not requirements_path.is_file():
            raise SetupError(
                "configuration",
                f"requirements file is not a file: {requirements_file}",
                EXIT_CONFIGURATION,
            )
        pins = 0
        try:
            requirement_text = requirements_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise SetupError(
                "configuration",
                f"requirements lock is unreadable: {requirements_file}: {exc}",
                EXIT_CONFIGURATION,
            ) from exc
        for number, raw_line in enumerate(requirement_text.splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if _PIN.fullmatch(line) is None:
                raise SetupError(
                    "configuration",
                    f"requirements lock must contain exact name==version pins: "
                    f"{requirements_file}:{number}",
                    EXIT_CONFIGURATION,
                )
            distribution, version = line.split("==", 1)
            normalised = re.sub(r"[-_.]+", "-", distribution).casefold()
            previous = locked_names.get(normalised)
            if previous is not None and previous != version:
                raise SetupError(
                    "configuration",
                    f"conflicting locked versions for {distribution}",
                    EXIT_CONFIGURATION,
                )
            locked_names[normalised] = version
            pins += 1
        if pins == 0:
            raise SetupError(
                "configuration",
                f"requirements lock is empty: {requirements_file}",
                EXIT_CONFIGURATION,
            )
        groups.append(
            {
                "name": raw_name,
                "requirements_file": requirements_file,
                "requirements_path": requirements_path,
                "index_url": index_url,
                "no_deps": True,
            }
        )
    distribution = profile_lock.get("paddle_distribution")
    paddle_version = profile_lock.get("paddle_version")
    if not isinstance(distribution, str) or not isinstance(paddle_version, str):
        raise SetupError(
            "configuration",
            f"runtime lock {profile} Paddle contract is invalid",
            EXIT_CONFIGURATION,
        )
    normalised_distribution = re.sub(r"[-_.]+", "-", distribution).casefold()
    if locked_names.get(normalised_distribution) != paddle_version:
        raise SetupError(
            "configuration",
            f"runtime lock {profile} Paddle pin is inconsistent",
            EXIT_CONFIGURATION,
        )
    return lock, groups


def _runtime_paths(home_root: Path, profile: str) -> dict[str, Path]:
    runtime_root = resolve_inside(
        home_root,
        home_root / ".runtime",
        description="runtime root",
        must_exist=False,
    )
    profile_root = resolve_inside(
        home_root,
        runtime_root / "v" / PROFILE_PATH_CODES[profile],
        description="profile root",
        must_exist=False,
    )
    venv_root = profile_root
    venv_python = resolve_inside(
        home_root,
        venv_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python"),
        description="profile Python",
        must_exist=False,
    )
    return {
        "runtime": runtime_root,
        "profile": profile_root,
        "venv": venv_root,
        "python": venv_python,
        "state": runtime_root / "setup_state.json",
    }


def _ensure_local_directories(home_root: Path, runtime_root: Path) -> None:
    for relative in (
        "home",
        "home/AppData/Local",
        "home/AppData/Roaming",
        "home/.cache",
        "tools",
        "python",
        "python-bin",
        "cache/uv",
        "cache/pip",
        "cache/huggingface",
        "cache/modelscope",
        "cache/paddle",
        "cache/cuda",
        "cache/xdg",
        "downloads",
        "tmp",
        "paddlex_cache",
        "v",
    ):
        directory = resolve_inside(
            home_root,
            runtime_root / relative,
            description=f"runtime directory {relative}",
            must_exist=False,
        )
        directory.mkdir(parents=True, exist_ok=True)


def sanitized_child_environment(
    home_root: Path,
    runtime_root: Path,
    lock: Mapping[str, Any],
    *,
    base_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = os.environ if base_environment is None else base_environment
    environment = {
        key: value
        for key, value in source.items()
        if key.upper() in _PASSTHROUGH_ENVIRONMENT
    }
    local_home = runtime_root / "home"
    environment.update(
        {
            "HOME": str(local_home),
            "USERPROFILE": str(local_home),
            "LOCALAPPDATA": str(local_home / "AppData" / "Local"),
            "APPDATA": str(local_home / "AppData" / "Roaming"),
            "TEMP": str(runtime_root / "tmp"),
            "TMP": str(runtime_root / "tmp"),
            "PYTHONNOUSERSITE": "1",
            "PIP_CACHE_DIR": str(runtime_root / "cache" / "pip"),
            "UV_CACHE_DIR": str(runtime_root / "cache" / "uv"),
            "UV_NO_CONFIG": "1",
            "UV_NO_MODIFY_PATH": "1",
            "UV_NO_PROGRESS": "1",
            "UV_PYTHON_INSTALL_DIR": str(runtime_root / "python"),
            "UV_PYTHON_BIN_DIR": str(runtime_root / "python-bin"),
            "UV_PYTHON_INSTALL_BIN": "0",
            "UV_PYTHON_NO_REGISTRY": "1",
            "UV_PYTHON_PREFERENCE": "only-managed",
            "HF_HOME": str(runtime_root / "cache" / "huggingface"),
            "HUGGINGFACE_HUB_CACHE": str(runtime_root / "cache" / "huggingface"),
            "MODELSCOPE_CACHE": str(runtime_root / "cache" / "modelscope"),
            "PADDLE_HOME": str(runtime_root / "cache" / "paddle"),
            "CUDA_CACHE_PATH": str(runtime_root / "cache" / "cuda"),
            "XDG_CACHE_HOME": str(runtime_root / "cache" / "xdg"),
            "PADDLE_PDX_CACHE_HOME": str(runtime_root / "paddlex_cache"),
            "OCR_WORKER_HOME_ROOT": str(home_root),
        }
    )
    cache_environment = lock.get("cache_environment")
    if isinstance(cache_environment, dict):
        for key in (
            "PADDLE_PDX_MODEL_SOURCE",
            "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK",
        ):
            value = cache_environment.get(key)
            if isinstance(value, str):
                environment[key] = value
    return environment


def _write_child_diagnostics(completed: subprocess.CompletedProcess[str]) -> None:
    for payload in (completed.stdout, completed.stderr):
        if payload:
            sys.stderr.write(payload)
            if not payload.endswith("\n"):
                sys.stderr.write("\n")


def _failure_exit_code(stage: str) -> int:
    return (
        EXIT_INSTALL
        if stage in {"network", "installer", "python", "index"}
        else EXIT_VERIFICATION
    )


def _classify_uv_failure(stage: str, completed: subprocess.CompletedProcess[str]) -> str:
    if stage != "index":
        return stage
    diagnostic = f"{completed.stdout}\n{completed.stderr}".casefold()
    network_markers = (
        "network error",
        "network failure",
        "connection",
        "timed out",
        "timeout",
        "temporary failure",
        "name resolution",
        "resolve host",
        "dns",
        "proxy",
        "tls",
        "ssl certificate",
        "unreachable",
        "unable to connect",
        "failed to connect",
        "error sending request",
    )
    return "network" if any(marker in diagnostic for marker in network_markers) else stage


def _execute(
    arguments: Sequence[str],
    *,
    environment: Mapping[str, str],
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(arguments),
        cwd=str(cwd),
        env=dict(environment),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _run_uv(
    uv_path: Path,
    arguments: Sequence[str],
    *,
    environment: Mapping[str, str],
    home_root: Path,
    stage: str,
) -> None:
    command = [str(uv_path), *map(str, arguments)]
    print("+ " + " ".join(command), file=sys.stderr)
    try:
        completed = _execute(command, environment=environment, cwd=home_root)
    except OSError as exc:
        raise SetupError(
            "installer",
            f"uv command could not start: {exc}",
            EXIT_INSTALL,
        ) from exc
    _write_child_diagnostics(completed)
    if completed.returncode != 0:
        failure_stage = _classify_uv_failure(stage, completed)
        raise SetupError(
            failure_stage,
            f"uv command failed with exit code {completed.returncode}",
            _failure_exit_code(failure_stage),
        )


def _run_probe(
    profile_python: Path,
    probe_path: Path,
    home_root: Path,
    profile: str,
    runtime_lock_path: Path,
    *,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    command = [
        str(profile_python),
        "-B",
        "-I",
        str(probe_path),
        "--home-root",
        str(home_root),
        "--profile",
        profile,
        "--runtime-lock",
        str(runtime_lock_path),
        "--deep",
    ]
    try:
        completed = _execute(command, environment=environment, cwd=home_root)
    except OSError as exc:
        raise SetupError(
            "python",
            f"runtime probe could not start: {exc}",
            EXIT_VERIFICATION,
        ) from exc
    if completed.stderr:
        sys.stderr.write(completed.stderr)
        if not completed.stderr.endswith("\n"):
            sys.stderr.write("\n")
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SetupError(
            "paddle_import",
            "runtime probe did not return exactly one JSON object",
            EXIT_VERIFICATION,
        ) from exc
    if (
        completed.returncode != 0
        or not isinstance(payload, dict)
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("status") != "READY"
        or payload.get("profile") != profile
        or payload.get("mode") != "deep"
    ):
        error = payload.get("error") if isinstance(payload, dict) else None
        reported_stage = payload.get("stage") if isinstance(payload, dict) else None
        if reported_stage not in {
            "configuration",
            "python",
            "dependency",
            "paddle_import",
        }:
            reported_stage = "paddle_import"
        exit_code = (
            EXIT_CONFIGURATION
            if completed.returncode == EXIT_CONFIGURATION
            and reported_stage == "configuration"
            else EXIT_VERIFICATION
        )
        raise SetupError(
            str(reported_stage),
            f"runtime probe failed with exit code {completed.returncode}: {error or 'invalid report'}",
            exit_code,
        )
    paddle = payload.get("paddle")
    if (
        not isinstance(payload.get("packages"), dict)
        or not isinstance(payload.get("python"), dict)
        or not isinstance(paddle, dict)
        or not isinstance(paddle.get("cuda_compiled"), bool)
        or not isinstance(paddle.get("dataset_cache"), str)
        or not isinstance(paddle.get("device_smoke"), dict)
    ):
        raise SetupError(
            "paddle_import",
            "runtime deep probe omitted required verification fields",
            EXIT_VERIFICATION,
        )
    return payload


def _run_uv_version(
    uv_path: Path,
    expected_version: str,
    *,
    environment: Mapping[str, str],
    home_root: Path,
) -> str:
    command = [str(uv_path), "--version"]
    try:
        completed = _execute(command, environment=environment, cwd=home_root)
    except OSError as exc:
        raise SetupError(
            "installer",
            f"uv version check could not start: {exc}",
            EXIT_VERIFICATION,
        ) from exc
    _write_child_diagnostics(completed)
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if completed.returncode != 0 or len(lines) != 1:
        raise SetupError(
            "installer",
            f"uv version check failed with exit code {completed.returncode}",
            EXIT_VERIFICATION,
        )
    match = re.fullmatch(r"uv\s+([^\s]+)(?:\s+.*)?", lines[0])
    actual = match.group(1) if match else None
    if actual != expected_version:
        raise SetupError(
            "installer",
            f"uv version mismatch: expected {expected_version}, got {actual or lines[0]}",
            EXIT_VERIFICATION,
        )
    return actual


def _profile_python_is_usable(
    profile_python: Path,
    lock: Mapping[str, Any],
    *,
    environment: Mapping[str, str],
    home_root: Path,
) -> bool:
    if not profile_python.is_file():
        return False
    venv_root = profile_python.parent.parent
    pyvenv_config = venv_root / "pyvenv.cfg"
    try:
        resolve_inside(
            home_root,
            pyvenv_config,
            description="profile pyvenv.cfg",
        )
    except SetupError:
        return False
    if not pyvenv_config.is_file():
        return False
    code = (
        "import json,os,platform,struct,sys;"
        "print(json.dumps({'version':platform.python_version(),"
        "'architecture':str(struct.calcsize('P')*8)+'bit',"
        "'executable':os.path.abspath(sys.executable),"
        "'prefix':os.path.abspath(sys.prefix),"
        "'base_prefix':os.path.abspath(sys.base_prefix)}))"
    )
    command = [str(profile_python), "-B", "-I", "-c", code]
    try:
        completed = _execute(command, environment=environment, cwd=home_root)
    except OSError as exc:
        print(f"profile Python usability check could not start: {exc}", file=sys.stderr)
        return False
    if completed.returncode != 0:
        _write_child_diagnostics(completed)
        return False
    try:
        report = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError):
        return False
    python_lock = lock.get("python")
    if not (
        isinstance(python_lock, dict)
        and isinstance(report, dict)
        and report.get("version") == python_lock.get("version")
        and report.get("architecture") == python_lock.get("architecture")
        and isinstance(report.get("executable"), str)
        and isinstance(report.get("prefix"), str)
        and isinstance(report.get("base_prefix"), str)
    ):
        return False

    normalize = lambda value: os.path.normcase(os.path.normpath(os.path.abspath(value)))
    if normalize(report["executable"]) != normalize(profile_python):
        return False
    if normalize(report["prefix"]) != normalize(venv_root):
        return False
    if normalize(report["base_prefix"]) == normalize(venv_root):
        return False
    try:
        resolve_inside(
            home_root,
            Path(report["base_prefix"]),
            description="profile base Python",
        )
    except SetupError:
        return False
    return True


def _load_existing_state(state_path: Path) -> dict[str, Any]:
    if not state_path.is_file():
        return {}
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SetupError(
            "configuration",
            "existing setup state is invalid; use a clean Worker Home extraction",
            EXIT_CONFIGURATION,
        ) from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise SetupError(
            "configuration",
            "legacy setup state is not supported; use a clean Worker Home extraction",
            EXIT_CONFIGURATION,
        )
    return payload


def _remove_profile_root(home_root: Path, profile_root: Path) -> None:
    if not profile_root.exists() and not profile_root.is_symlink():
        return
    resolved = resolve_inside(
        home_root,
        profile_root,
        description="partial profile root",
    )
    expected = home_root / ".runtime" / "v" / profile_root.name
    expected = resolve_inside(
        home_root,
        expected,
        description="expected profile root",
        must_exist=False,
    )
    if os.path.normcase(str(resolved)) != os.path.normcase(str(expected)):
        raise SetupError(
            "configuration",
            f"refusing to remove unexpected profile root: {resolved}",
            EXIT_CONFIGURATION,
        )
    is_junction = getattr(profile_root, "is_junction", lambda: False)
    try:
        if profile_root.is_symlink():
            profile_root.unlink()
        elif is_junction():
            os.rmdir(profile_root)
        elif profile_root.is_dir():
            shutil.rmtree(profile_root)
        else:
            profile_root.unlink()
    except OSError as exc:
        raise SetupError(
            "python",
            f"partial profile could not be removed: {exc}",
            EXIT_INSTALL,
        ) from exc


def _preserved_profiles(
    state: Mapping[str, Any],
    home_root: Path,
    *,
    replacing: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    profiles = state.get("profiles")
    if not isinstance(profiles, dict):
        return result
    for profile in sorted(VALID_PROFILES - {replacing}):
        record = profiles.get(profile)
        if not isinstance(record, dict):
            continue
        expected_venv = f".runtime/v/{PROFILE_PATH_CODES[profile]}"
        expected_python = (
            f"{expected_venv}/Scripts/python.exe"
            if os.name == "nt"
            else f"{expected_venv}/bin/python"
        )
        if record.get("venv") != expected_venv or record.get("python") != expected_python:
            continue
        python_path = resolve_inside(
            home_root,
            home_root / Path(expected_python),
            description=f"preserved {profile} profile Python",
            must_exist=False,
        )
        if python_path.is_file():
            result[profile] = copy.deepcopy(record)
    return result


def _atomic_write_state(state_path: Path, state: Mapping[str, Any]) -> None:
    temporary = state_path.with_name(
        f".{state_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, state_path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _commit_state(state_path: Path, state: Mapping[str, Any]) -> None:
    try:
        _atomic_write_state(state_path, state)
    except OSError as exc:
        raise SetupError(
            "installer",
            f"setup state could not be committed: {exc}",
            EXIT_VERIFICATION,
        ) from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise SetupError(
            "dependency",
            f"requirements lock could not be hashed: {path.name}: {exc}",
            EXIT_VERIFICATION,
        ) from exc
    return digest.hexdigest()


def _profile_evidence(
    home_root: Path,
    profile: str,
    profile_python: Path,
    managed_python: Path,
    uv_path: Path,
    uv_version: str,
    groups: Sequence[Mapping[str, Any]],
    verification: Mapping[str, Any],
    completed_at: str,
) -> dict[str, Any]:
    python_report = verification["python"]
    paddle_report = verification["paddle"]
    requirements = [
        {
            "group": group["name"],
            "file": home_relative(home_root, group["requirements_path"]),
            "sha256": _sha256_file(group["requirements_path"]),
            "index_url": group["index_url"],
        }
        for group in groups
    ]
    return {
        "venv": home_relative(home_root, profile_python.parent.parent),
        "python": home_relative(home_root, profile_python),
        "python_version": python_report["version"],
        "python_architecture": python_report["architecture"],
        "managed_python": home_relative(home_root, managed_python),
        "managed_python_version": python_report["version"],
        "managed_python_architecture": python_report["architecture"],
        "uv_path": home_relative(home_root, uv_path),
        "uv_version": uv_version,
        "install_groups": [group["name"] for group in groups],
        "requirements_locks": requirements,
        "packages": copy.deepcopy(verification["packages"]),
        "dependency_check": {"status": "PASS", "command": "uv pip check"},
        "paddle_build": {
            "distribution": paddle_report["distribution"],
            "version": paddle_report["version"],
            "cuda_compiled": paddle_report["cuda_compiled"],
        },
        "device_smoke": copy.deepcopy(paddle_report["device_smoke"]),
        "completed_at": completed_at,
        "verification": copy.deepcopy(verification),
    }


def _state_payload(
    previous_state: Mapping[str, Any],
    home_root: Path,
    profile: str,
    managed_python: Path,
    completed_at: str,
    *,
    profile_record: Mapping[str, Any] | None,
) -> dict[str, Any]:
    profiles = _preserved_profiles(previous_state, home_root, replacing=profile)
    if profile_record is not None:
        profiles[profile] = copy.deepcopy(profile_record)
    previous_active = previous_state.get("active_profile")
    active_profile = (
        profile
        if profile_record is not None
        else previous_active
        if previous_active in profiles
        else sorted(profiles)[0]
        if profiles
        else None
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "setup_completed": completed_at,
        "managed_python": home_relative(home_root, managed_python),
        "active_profile": active_profile,
        "profiles": profiles,
        "global_path_changed": False,
        "admin_used": False,
        "docker_used": False,
        "driver_changed": False,
    }


def _success_report(
    state: Mapping[str, Any],
    profile: str,
    profile_python: Path,
    home_root: Path,
    verification: Mapping[str, Any],
) -> dict[str, Any]:
    profiles = state.get("profiles")
    installed = sorted(profiles) if isinstance(profiles, dict) else [profile]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "READY",
        "profile": profile,
        "active_profile": profile,
        "installed_profiles": installed,
        "managed_python": state["managed_python"],
        "profile_python": home_relative(home_root, profile_python),
        "verification": dict(verification),
    }


def setup_runtime(
    home_root: str | os.PathLike[str],
    profile: str,
    uv_path: str | os.PathLike[str],
    runtime_lock: str | os.PathLike[str],
    *,
    managed_python: str | os.PathLike[str] | None = None,
    base_environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if profile not in VALID_PROFILES:
        raise SetupError(
            "configuration",
            f"unsupported profile: {profile}",
            EXIT_CONFIGURATION,
        )
    home = _resolved_home(home_root)
    lock_path = resolve_inside(home, runtime_lock, description="runtime lock")
    if not lock_path.is_file():
        raise SetupError(
            "configuration",
            f"runtime lock is not a file: {lock_path}",
            EXIT_CONFIGURATION,
        )
    uv = resolve_inside(home, uv_path, description="uv executable")
    if not uv.is_file():
        raise SetupError(
            "configuration",
            f"uv executable is not a file: {uv}",
            EXIT_CONFIGURATION,
        )
    managed = resolve_inside(
        home,
        managed_python or sys.executable,
        description="managed Python",
    )
    if not managed.is_file():
        raise SetupError(
            "configuration",
            f"managed Python is not a file: {managed}",
            EXIT_CONFIGURATION,
        )
    probe_path = resolve_inside(
        home,
        lock_path.parent / "runtime_probe.py",
        description="runtime probe",
    )
    if not probe_path.is_file():
        raise SetupError(
            "configuration",
            f"runtime probe is not a file: {probe_path}",
            EXIT_CONFIGURATION,
        )

    lock, groups = _validate_runtime_lock(home, lock_path, profile)
    paths = _runtime_paths(home, profile)
    try:
        _ensure_local_directories(home, paths["runtime"])
    except OSError as exc:
        raise SetupError(
            "installer",
            f"local runtime directories could not be created: {exc}",
            EXIT_INSTALL,
        ) from exc
    environment = sanitized_child_environment(
        home,
        paths["runtime"],
        lock,
        base_environment=base_environment,
    )
    uv_version = _run_uv_version(
        uv,
        str(lock["uv"]["version"]),
        environment=environment,
        home_root=home,
    )
    previous_state = _load_existing_state(paths["state"])

    profile_record = (
        previous_state.get("profiles", {}).get(profile)
        if isinstance(previous_state.get("profiles"), dict)
        else None
    )
    venv_usable = _profile_python_is_usable(
        paths["python"],
        lock,
        environment=environment,
        home_root=home,
    )
    if isinstance(profile_record, dict) and venv_usable:
        try:
            _run_uv(
                uv,
                ["pip", "check", "--python", str(paths["python"])],
                environment=environment,
                home_root=home,
                stage="dependency",
            )
            verification = _run_probe(
                paths["python"],
                probe_path,
                home,
                profile,
                lock_path,
                environment=environment,
            )
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            evidence = _profile_evidence(
                home,
                profile,
                paths["python"],
                managed,
                uv,
                uv_version,
                groups,
                verification,
                str(profile_record.get("completed_at") or now),
            )
            verified_state = _state_payload(
                previous_state,
                home,
                profile,
                managed,
                now,
                profile_record=evidence,
            )
            _commit_state(paths["state"], verified_state)
            return _success_report(
                verified_state,
                profile,
                paths["python"],
                home,
                verification,
            )
        except SetupError as exc:
            print(
                f"existing {profile} profile is invalid and will be repaired: {exc}",
                file=sys.stderr,
            )
    elif isinstance(profile_record, dict):
        print(
            f"existing {profile} profile Python is unusable and will be rebuilt",
            file=sys.stderr,
        )

    if isinstance(profile_record, dict):
        # Never leave a profile marked installed while repairing/rebuilding it.
        cleaned_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        previous_state = _state_payload(
            previous_state,
            home,
            profile,
            managed,
            cleaned_at,
            profile_record=None,
        )
        _commit_state(paths["state"], previous_state)

    if not venv_usable:
        _remove_profile_root(home, paths["profile"])
        _run_uv(
            uv,
            ["venv", "--python", str(managed), str(paths["venv"])],
            environment=environment,
            home_root=home,
            stage="python",
        )
        if not _profile_python_is_usable(
            paths["python"],
            lock,
            environment=environment,
            home_root=home,
        ):
            try:
                _remove_profile_root(home, paths["profile"])
            except Exception as cleanup_error:
                print(f"partial profile cleanup failed: {cleanup_error}", file=sys.stderr)
            raise SetupError(
                "python",
                "uv did not create a usable profile Python",
                EXIT_INSTALL,
            )
        venv_usable = True

    try:
        profile_python = resolve_inside(
            home,
            paths["python"],
            description="profile Python",
        )
        if not profile_python.is_file():
            raise SetupError(
                "python",
                "uv did not create the profile Python",
                EXIT_INSTALL,
            )
        for group in groups:
            _run_uv(
                uv,
                [
                    "pip",
                    "install",
                    "--python",
                    str(profile_python),
                    "--index-url",
                    group["index_url"],
                    "--no-deps",
                    "--requirement",
                    str(group["requirements_path"]),
                ],
                environment=environment,
                home_root=home,
                stage="index",
            )
        _run_uv(
            uv,
            ["pip", "check", "--python", str(profile_python)],
            environment=environment,
            home_root=home,
            stage="dependency",
        )
        verification = _run_probe(
            profile_python,
            probe_path,
            home,
            profile,
            lock_path,
            environment=environment,
        )

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        evidence = _profile_evidence(
            home,
            profile,
            profile_python,
            managed,
            uv,
            uv_version,
            groups,
            verification,
            now,
        )
        state = _state_payload(
            previous_state,
            home,
            profile,
            managed,
            now,
            profile_record=evidence,
        )
        _commit_state(paths["state"], state)
        return _success_report(state, profile, profile_python, home, verification)
    except Exception:
        # Keep a usable partial venv and all download caches. The next approved
        # setup reruns every source-exclusive group idempotently in the same venv.
        if not venv_usable:
            try:
                _remove_profile_root(home, paths["profile"])
            except Exception as cleanup_error:
                print(f"partial profile cleanup failed: {cleanup_error}", file=sys.stderr)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(description="Install one Worker Home runtime profile")
    parser.add_argument("--home-root", required=True)
    parser.add_argument("--profile", choices=sorted(VALID_PROFILES), required=True)
    parser.add_argument("--uv-path", required=True)
    parser.add_argument("--runtime-lock", required=True)
    return parser


def _emit(payload: Mapping[str, Any]) -> None:
    # PowerShell captures this process through the Windows legacy code page.
    # ASCII escapes keep the exactly-one-JSON contract valid under ``python -I``
    # even when a Worker Home or error contains Vietnamese/Unicode characters.
    sys.stdout.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    profile: str | None = None
    try:
        args = _parser().parse_args(argv)
        profile = args.profile
        payload = setup_runtime(
            args.home_root,
            args.profile,
            args.uv_path,
            args.runtime_lock,
        )
    except SetupError as exc:
        print(f"runtime setup failed at {exc.stage}: {exc}", file=sys.stderr)
        _emit(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "BLOCKED",
                "profile": profile,
                "stage": exc.stage,
                "error": str(exc),
            }
        )
        return exc.exit_code
    except Exception as exc:  # pragma: no cover - last-resort CLI contract
        print(f"runtime setup failed unexpectedly: {exc}", file=sys.stderr)
        _emit(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "BLOCKED",
                "profile": profile,
                "stage": "dependency",
                "error": str(exc),
            }
        )
        return EXIT_VERIFICATION
    _emit(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
