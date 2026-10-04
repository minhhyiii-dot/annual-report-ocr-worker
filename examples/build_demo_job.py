"""Package the synthetic one-page fixture; does not install or invoke OCR."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import zipfile


def build_demo_job(output: Path) -> Path:
    source = Path(__file__).resolve().parent / "synthetic-job"
    manifest = json.loads((source / "job.json").read_text(encoding="utf-8"))
    asset = manifest["assets"][0]
    image = (source / asset["input_path"]).read_bytes()
    if not image.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("The demo input is not a PNG")
    asset["sha256"] = hashlib.sha256(image).hexdigest()
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    files = {"job.json": manifest_bytes, asset["input_path"]: image}
    checksums = "".join(hashlib.sha256(content).hexdigest() + "  " + name + "\n" for name, content in files.items())
    files["job_checksums.sha256"] = checksums.encode("utf-8")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    wrapper = "OCR_JOB_" + manifest["job_id"]
    # Exclusive creation prevents accidental overwriting of an existing job archive.
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            item = zipfile.ZipInfo(wrapper + "/" + name, date_time=(2025, 1, 1, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(item, content)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="New archive path, normally inside Worker Home inbox")
    args = parser.parse_args()
    print(build_demo_job(args.output))


if __name__ == "__main__":
    main()
