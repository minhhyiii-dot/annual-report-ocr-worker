"""Dependency-free core checks; never install or run PaddleOCR inference."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
WORKER_HOME = ROOT / "OCR_WORKER_HOME"
WORKER = WORKER_HOME / "worker" / "ocr_worker.py"
FIXTURE = ROOT / "examples" / "synthetic-job"


def load_builder():
    spec = importlib.util.spec_from_file_location("demo_builder", ROOT / "examples" / "build_demo_job.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def core_command(home: Path, command: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-B", "-I", str(WORKER), command, "--home-root", str(home)],
        capture_output=True, text=True, encoding="utf-8", timeout=20, env=environment,
    )


class CoreTests(unittest.TestCase):
    def test_original_checksums_and_python_syntax(self):
        entries = (WORKER_HOME / "worker_home_checksums.sha256").read_text().splitlines()
        self.assertEqual(len(entries), 14)
        for entry in entries:
            digest, relative = entry.split("  ", 1)
            self.assertEqual(hashlib.sha256((WORKER_HOME / relative).read_bytes()).hexdigest(), digest, relative)
        for path in ROOT.rglob("*.py"):
            ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))

    def test_fixture_checksums_and_contract(self):
        for row in (FIXTURE / "job_checksums.sha256").read_text().splitlines():
            digest, relative = row.split("  ", 1)
            self.assertEqual(hashlib.sha256((FIXTURE / relative).read_bytes()).hexdigest(), digest)
        manifest = json.loads((FIXTURE / "job.json").read_text())
        self.assertEqual(manifest["schema_version"], "2.0")
        self.assertEqual(len(manifest["assets"]), 1)
        asset = manifest["assets"][0]
        image = (FIXTURE / asset["input_path"]).read_bytes()
        self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(hashlib.sha256(image).hexdigest(), asset["sha256"])

    def test_import_and_not_started_status(self):
        with tempfile.TemporaryDirectory(prefix="ocr-core-test-") as directory:
            home = Path(directory)
            load_builder().build_demo_job(home / "inbox" / "OCR_JOB_synthetic-demo.zip")
            result = core_command(home, "import")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["jobs"][0]["assets"], 1)
            state = core_command(home, "status")
            self.assertEqual(state.returncode, 0, state.stderr)
            row = json.loads(state.stdout)["jobs"][0]
            self.assertEqual(row["status"], "NOT_STARTED")
            self.assertEqual(row["completed_assets"], 0)
            self.assertEqual(row["pending_or_failed_assets"], 1)
            self.assertIsNone(row["result_zip"])

    def test_duplicate_import_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="ocr-core-test-") as directory:
            home = Path(directory)
            load_builder().build_demo_job(home / "inbox" / "OCR_JOB_synthetic-demo.zip")
            self.assertEqual(core_command(home, "import").returncode, 0)
            result = core_command(home, "import")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("job_id already exists", result.stderr)

    def test_corrupt_checksum_is_rejected_without_job(self):
        with tempfile.TemporaryDirectory(prefix="ocr-core-test-") as directory:
            home = Path(directory)
            original = load_builder().build_demo_job(home / "original.zip")
            bad = home / "inbox" / "OCR_JOB_synthetic-demo.zip"
            bad.parent.mkdir()
            with zipfile.ZipFile(original) as source, zipfile.ZipFile(bad, "w", compression=zipfile.ZIP_DEFLATED) as target:
                for item in source.infolist():
                    content = source.read(item)
                    if item.filename.endswith(".png"):
                        content += b"synthetic-corruption"
                    target.writestr(item, content)
            result = core_command(home, "import")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("job checksum mismatch", result.stderr)
            self.assertFalse((home / "jobs" / "synthetic-demo").exists())
            self.assertEqual(list((home / "jobs").iterdir()), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
