from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import os
import platform
import re
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


SCHEMA_VERSION = "2.1"
VALID_PROFILES = frozenset({"cpu", "gpu"})
PROFILE_PATH_CODES = {"cpu": "c", "gpu": "g"}
EXIT_CONFIGURATION = 2
EXIT_VERIFICATION = 4
_PIN = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s;]+)$"
)
_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[A-Za-z0-9.+-]*)?$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


class ProbeError(RuntimeError):
    """A probe failure with a stable JSON stage and process exit code."""

    def __init__(self, message: str, *, stage: str, exit_code: int) -> None:
        super().__init__(message)
        self.stage = stage
        self.exit_code = exit_code


class ProbeConfigurationError(ProbeError):
    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            stage="configuration",
            exit_code=EXIT_CONFIGURATION,
        )


class ProbeVerificationError(ProbeError):
    def __init__(self, message: str, *, stage: str = "dependency") -> None:
        super().__init__(
            message,
            stage=stage,
            exit_code=EXIT_VERIFICATION,
        )


class ProbePythonError(ProbeVerificationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, stage="python")


class ProbeDependencyError(ProbeVerificationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, stage="dependency")


class ProbePaddleImportError(ProbeVerificationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, stage="paddle_import")


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ProbeConfigurationError(message)


def _normalise_distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).casefold()


def _bounded_error(exc: BaseException, home_root: Path, limit: int = 500) -> str:
    text = " ".join(str(exc).splitlines()).replace(str(home_root), "<WORKER_HOME>")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _resolved_home(path: str | os.PathLike[str]) -> Path:
    candidate = Path(path).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ProbeConfigurationError(f"Worker Home is unavailable: {candidate}: {exc}") from exc
    if not resolved.is_dir():
        raise ProbeConfigurationError(f"Worker Home is not a directory: {resolved}")
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
        raise ProbeConfigurationError(f"{description} is unavailable: {candidate}: {exc}") from exc
    try:
        common = os.path.commonpath(
            [os.path.normcase(str(home_root)), os.path.normcase(str(resolved))]
        )
    except ValueError as exc:
        raise ProbeConfigurationError(f"{description} escapes Worker Home: {resolved}") from exc
    if common != os.path.normcase(str(home_root)):
        raise ProbeConfigurationError(f"{description} escapes Worker Home: {resolved}")
    return resolved


def home_relative(home_root: Path, path: Path) -> str:
    resolved = resolve_inside(
        home_root,
        path,
        description="path",
        must_exist=False,
    )
    return resolved.relative_to(home_root).as_posix()


def load_runtime_lock(home_root: Path, runtime_lock: str | os.PathLike[str]) -> dict[str, Any]:
    lock_path = resolve_inside(
        home_root,
        runtime_lock,
        description="runtime lock",
    )
    if not lock_path.is_file():
        raise ProbeConfigurationError(f"runtime lock is not a file: {lock_path}")
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProbeConfigurationError(f"runtime lock is invalid: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ProbeConfigurationError(f"runtime lock schema must be {SCHEMA_VERSION}")
    uv_lock = payload.get("uv")
    if (
        not isinstance(uv_lock, dict)
        or not isinstance(uv_lock.get("version"), str)
        or _VERSION.fullmatch(uv_lock["version"]) is None
        or not isinstance(uv_lock.get("installer_url"), str)
        or not uv_lock["installer_url"].startswith("https://")
        or not isinstance(uv_lock.get("installer_sha256"), str)
        or _SHA256.fullmatch(uv_lock["installer_sha256"]) is None
    ):
        raise ProbeConfigurationError("runtime lock uv contract is invalid")
    python_lock = payload.get("python")
    if (
        not isinstance(python_lock, dict)
        or python_lock.get("implementation") != "CPython"
        or not isinstance(python_lock.get("version"), str)
        or _VERSION.fullmatch(python_lock["version"]) is None
        or python_lock.get("architecture") != "64bit"
    ):
        raise ProbeConfigurationError("runtime lock Python contract is invalid")
    return payload


def profile_install_groups(lock: Mapping[str, Any], profile: str) -> list[dict[str, Any]]:
    if profile not in VALID_PROFILES:
        raise ProbeConfigurationError(f"unsupported profile: {profile}")
    profiles = lock.get("profiles")
    groups = lock.get("install_groups")
    if not isinstance(profiles, dict) or not isinstance(groups, dict):
        raise ProbeConfigurationError("runtime lock install_groups/profiles are invalid")
    profile_lock = profiles.get(profile)
    if not isinstance(profile_lock, dict):
        raise ProbeConfigurationError(f"runtime lock has no {profile} profile")
    group_names = profile_lock.get("install_groups")
    if not isinstance(group_names, list) or not group_names:
        raise ProbeConfigurationError(f"runtime lock {profile} install_groups are invalid")
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_name in group_names:
        if not isinstance(raw_name, str) or raw_name in seen:
            raise ProbeConfigurationError(f"runtime lock {profile} has duplicate/invalid install group")
        seen.add(raw_name)
        raw_group = groups.get(raw_name)
        if not isinstance(raw_group, dict):
            raise ProbeConfigurationError(f"runtime lock install group is missing: {raw_name}")
        requirements_file = raw_group.get("requirements_file")
        index_url = raw_group.get("index_url")
        if (
            not isinstance(requirements_file, str)
            or not requirements_file
            or not isinstance(index_url, str)
            or not index_url.startswith("https://")
            or raw_group.get("no_deps") is not True
            or "extra_index_url" in raw_group
            or "extra_index_urls" in raw_group
        ):
            raise ProbeConfigurationError(f"runtime lock install group is not source-exclusive: {raw_name}")
        selected.append(
            {
                "name": raw_name,
                "requirements_file": requirements_file,
                "index_url": index_url,
                "no_deps": True,
            }
        )
    return selected


def parse_requirement_locks(
    home_root: Path,
    runtime_lock_path: str | os.PathLike[str],
    groups: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, tuple[str, str]], list[str]]:
    lock_path = resolve_inside(
        home_root,
        runtime_lock_path,
        description="runtime lock",
    )
    worker_root = lock_path.parent
    locked: dict[str, tuple[str, str]] = {}
    files: list[str] = []
    for group in groups:
        relative = group["requirements_file"]
        if Path(relative).name != relative:
            raise ProbeConfigurationError(f"requirements file must be a basename: {relative}")
        requirement_path = resolve_inside(
            home_root,
            worker_root / relative,
            description=f"requirements file {relative}",
        )
        if not requirement_path.is_file():
            raise ProbeConfigurationError(f"requirements file is not a file: {relative}")
        files.append(home_relative(home_root, requirement_path))
        try:
            requirement_text = requirement_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ProbeConfigurationError(
                f"requirements lock is unreadable: {relative}: {exc}"
            ) from exc
        for number, raw_line in enumerate(requirement_text.splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            match = _PIN.fullmatch(line)
            if match is None:
                raise ProbeConfigurationError(
                    f"requirements lock must contain exact name==version pins: "
                    f"{relative}:{number}"
                )
            display_name = match.group("name")
            version = match.group("version")
            normalised = _normalise_distribution(display_name)
            previous = locked.get(normalised)
            if previous is not None and previous[1] != version:
                raise ProbeConfigurationError(
                    f"conflicting locked versions for {display_name}: "
                    f"{previous[1]} and {version}"
                )
            locked[normalised] = (display_name, version)
    if not locked:
        raise ProbeConfigurationError("selected requirements locks are empty")
    return locked, files


def _expected_profile_python(home_root: Path, profile: str) -> Path:
    relative = (
        Path(".runtime") / "v" / PROFILE_PATH_CODES[profile] / "Scripts" / "python.exe"
        if os.name == "nt"
        else Path(".runtime") / "v" / PROFILE_PATH_CODES[profile] / "bin" / "python"
    )
    return resolve_inside(
        home_root,
        home_root / relative,
        description="profile Python",
    )


def probe_runtime(
    home_root: str | os.PathLike[str],
    profile: str,
    runtime_lock: str | os.PathLike[str],
    *,
    python_executable: str | os.PathLike[str] | None = None,
    python_version: str | None = None,
    architecture: str | None = None,
    deep: bool = False,
    version_getter: Callable[[str], str] = importlib.metadata.version,
    module_importer: Callable[[str], Any] = importlib.import_module,
) -> dict[str, Any]:
    home = _resolved_home(home_root)
    lock = load_runtime_lock(home, runtime_lock)
    groups = profile_install_groups(lock, profile)
    locked, lock_files = parse_requirement_locks(home, runtime_lock, groups)

    executable = resolve_inside(
        home,
        python_executable or sys.executable,
        description="profile Python",
    )
    expected_executable = _expected_profile_python(home, profile)
    if os.path.normcase(str(executable)) != os.path.normcase(str(expected_executable)):
        raise ProbePythonError(
            f"runtime probe must run with the {profile} profile Python: {executable}"
        )

    expected_python = lock.get("python")
    if not isinstance(expected_python, dict):
        raise ProbeConfigurationError("runtime lock Python contract is invalid")
    actual_python_version = python_version or platform.python_version()
    actual_architecture = architecture or platform.architecture()[0]
    if actual_python_version != expected_python.get("version"):
        raise ProbePythonError(
            f"Python version mismatch: expected {expected_python.get('version')}, "
            f"got {actual_python_version}"
        )
    if actual_architecture != expected_python.get("architecture"):
        raise ProbePythonError(
            f"Python architecture mismatch: expected {expected_python.get('architecture')}, "
            f"got {actual_architecture}"
        )

    installed: dict[str, str] = {}
    mismatches: list[str] = []
    for normalised, (display_name, expected_version) in sorted(locked.items()):
        try:
            actual_version = version_getter(display_name)
        except importlib.metadata.PackageNotFoundError:
            mismatches.append(f"{display_name}: missing (expected {expected_version})")
            continue
        except Exception as exc:
            raise ProbeDependencyError(
                f"package metadata verification failed for {display_name}: "
                f"{_bounded_error(exc, home)}"
            ) from exc
        installed[normalised] = actual_version
        if actual_version != expected_version:
            mismatches.append(
                f"{display_name}: expected {expected_version}, got {actual_version}"
            )
    if mismatches:
        raise ProbeDependencyError("locked package verification failed: " + "; ".join(mismatches))

    profile_lock = lock["profiles"][profile]
    distribution = profile_lock.get("paddle_distribution")
    if not isinstance(distribution, str) or not distribution:
        raise ProbeConfigurationError(f"runtime lock {profile} Paddle distribution is invalid")
    distribution_key = _normalise_distribution(distribution)
    expected_paddle_version = profile_lock.get("paddle_version")
    locked_paddle = locked.get(distribution_key)
    if (
        not isinstance(expected_paddle_version, str)
        or locked_paddle is None
        or locked_paddle[1] != expected_paddle_version
    ):
        raise ProbeConfigurationError(f"runtime lock {profile} Paddle pin is inconsistent")
    paddle_report: dict[str, Any] = {
        "distribution": distribution,
        "version": installed[distribution_key],
    }
    if deep:
        try:
            paddle = module_importer("paddle")
            compiled = bool(paddle.device.is_compiled_with_cuda())
            dataset_path = Path(paddle.dataset.common.DATA_HOME)
        except Exception as exc:
            raise ProbePaddleImportError(
                f"Paddle import/build verification failed: {_bounded_error(exc, home)}"
            ) from exc
        expected_compiled = profile == "gpu"
        if compiled != expected_compiled:
            raise ProbePaddleImportError(
                f"Paddle CUDA build mismatch for {profile}: "
                f"expected {expected_compiled}, got {compiled}"
            )
        try:
            dataset_home = resolve_inside(
                home,
                dataset_path,
                description="Paddle dataset cache",
                must_exist=False,
            )
        except ProbeConfigurationError as exc:
            raise ProbePaddleImportError(str(exc)) from exc
        requested_device = "gpu:0" if profile == "gpu" else "cpu"
        device_smoke: dict[str, Any] = {
            "status": "WARNING",
            "stage": "device_warning",
            "requested_device": requested_device,
            "device": None,
            "error": None,
        }
        try:
            paddle.set_device(requested_device)
            selected_device = str(paddle.device.get_device())
            if profile == "gpu" and not re.fullmatch(r"gpu:\d+", selected_device):
                raise RuntimeError(f"Paddle selected {selected_device}, not a CUDA device")
            if profile == "cpu" and selected_device != "cpu":
                raise RuntimeError(f"Paddle selected {selected_device}, not cpu")
            # This is intentionally tiny and does not initialise any OCR/model.
            tensor = paddle.to_tensor([1.0], dtype="float32")
            observed = float((tensor + 1.0).numpy()[0])
            if observed != 2.0:
                raise RuntimeError(f"tiny tensor smoke returned {observed!r}")
            device_smoke.update(
                {
                    "status": "PASS",
                    "device": selected_device,
                    "observed": observed,
                }
            )
            device_smoke.pop("stage", None)
        except Exception as exc:
            device_smoke["error"] = _bounded_error(exc, home)
        paddle_report.update(
            {
                "cuda_compiled": compiled,
                "dataset_cache": home_relative(home, dataset_home),
                "device_smoke": device_smoke,
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "READY",
        "profile": profile,
        "mode": "deep" if deep else "metadata",
        "python": {
            "executable": home_relative(home, executable),
            "version": actual_python_version,
            "architecture": actual_architecture,
        },
        "packages": installed,
        "package_count": len(installed),
        "lock_files": lock_files,
        "paddle": paddle_report,
    }


def _parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(description="Verify one Worker Home runtime profile")
    parser.add_argument("--home-root", required=True)
    parser.add_argument("--profile", choices=sorted(VALID_PROFILES), required=True)
    parser.add_argument("--runtime-lock", required=True)
    parser.add_argument(
        "--deep",
        action="store_true",
        help="Import Paddle and verify device/cache; setup and pre-run only, never doctor",
    )
    return parser


def _emit(payload: Mapping[str, Any]) -> None:
    # Keep redirected stdout ASCII-safe under Windows ``python -I``. JSON
    # consumers recover the original Unicode text from the escape sequences.
    sys.stdout.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    profile: str | None = None
    try:
        args = _parser().parse_args(argv)
        profile = args.profile
        payload = probe_runtime(
            args.home_root,
            args.profile,
            args.runtime_lock,
            deep=args.deep,
        )
    except ProbeError as exc:
        print(f"runtime probe failed: {exc}", file=sys.stderr)
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
        print(f"runtime probe failed unexpectedly: {exc}", file=sys.stderr)
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
