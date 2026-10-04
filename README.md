# Annual Report OCR Worker

A portable Windows batch worker for converting prepared annual-report page images into raw Markdown and JSON with PaddleOCR-VL. This is the OCR processing stage of a local document-data workflow, not an HTTP backend or a financial-analysis model.

The public repository contains the small worker starter, pinned runtime/dependency definitions, and a **fully synthetic one-page job**. Real company PDFs, large job archives, model weights, installed runtimes, and historical OCR outputs are not included.

## What it does

1. Accepts a job ZIP containing a manifest and already separated PNG page images.
2. Validates schema 2.0, file paths, the exact input-file set, and SHA-256 checksums before importing.
3. Runs a designated probe image before processing the remaining assigned assets.
4. Uses one page per batch and saves a Markdown file plus a JSON result for each image.
5. Uses validated output pairs as checkpoints so an interrupted job can resume.
6. Packages result metadata and output files into `RESULT_<job_id>.zip`.

PDF rendering/page splitting happen **upstream, outside this worker**. It does not crop images, merge a complete report, manually correct OCR, score companies, or upload results.

## Repository layout

```text
OCR_WORKER_HOME/            Original portable worker, preserved byte-for-byte
  worker/                  Python core, PowerShell entry point, setup/probe scripts
  worker_home_checksums.sha256
  START_HERE.md            Original Vietnamese operating guide
examples/
  synthetic-job/           Schema 2.0 manifest, checksums, and one invented PNG
  build_demo_job.py         Dependency-free synthetic job ZIP builder
tests/test_core.py          Dependency-free integrity and core import/status checks
```

The original checksum manifest covers **14 files**. Together with the manifest, the starter contains 15 files. Added documentation, examples, tests, and Git metadata stay outside `OCR_WORKER_HOME` because its `doctor` rejects unexpected static files. `.gitattributes` disables text conversion for every original starter file and checksummed synthetic input, preserving hashes on Windows and Linux clones.

The original agent prompt files are retained as historical project artifacts. They describe the worker's operating procedure; they are not required for the synthetic builder or dependency-free tests. No installation or model download occurs just by cloning, building the sample ZIP, or running the tests below.

## Input and output contract

A valid archive has one top-level directory named `OCR_JOB_<job_id>` and contains:

- `job.json`: schema `2.0`, required profile, sources, assets, probe selection, and expected suffixes.
- `job_checksums.sha256`: SHA-256 entries for every immutable file, including the manifest.
- `input/<TICKER>/<asset_id>.png`: each image's digest also appears in its asset record.

Runtime state uses version `2.1`; input job schema remains `2.0`. The required job profile is `paddleocr-vl-v1.6-raw-maxtext-r1`.

After import, the worker stores jobs under `OCR_WORKER_HOME/jobs/<job_id>/`. OCR output pairs are `output/<TICKER>/markdown/<asset_id>.md` and `output/<TICKER>/json/<asset_id>_res.json`. A checkpoint counts as complete only when both outputs validate. Result ZIPs exclude input images, Python, installed packages, and models.

## Synthetic example

The PNG contains an invented one-page financial table for **DEMO Company**, labeled as synthetic. It has no source PDF and represents no real company. `VNMidcap` in its manifest is a schema-required enum value, not a claim that the fictional company belongs to an index. The included expected text is visible in the PNG; no OCR output is fabricated.

With an existing Python interpreter, run from the repository root:

```powershell
python -B .\examples\build_demo_job.py --output .\OCR_WORKER_HOME\inbox\OCR_JOB_synthetic-demo.zip
```

The builder uses only the Python standard library, recomputes input and manifest checksums, and writes the expected wrapper folder. It refuses to overwrite an existing archive. It does **not** install packages, import a job into the real worker, or invoke inference. The PNG and source manifest remain unchanged.

## Worker operation on Windows

The original portable workflow is Windows x64 and PowerShell based. Its locks specify CPython 3.12.10, Paddle 3.3.1, PaddleOCR 3.7.0, and PaddleX 3.7.2. CPU and GPU environments have separate local profiles; model cache is shared within the worker home. These are the source's pinned versions, not a promise of compatibility with every machine.

Begin with the read-only preflight from inside `OCR_WORKER_HOME`:

```powershell
Set-Location .\OCR_WORKER_HOME
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\worker\worker.ps1 doctor
```

`READY` means the static worker integrity check passed. It can still report `setup_required: true` and advisory failures for runtime, network, GPU, memory, or disk checks. It is not an inference-readiness guarantee.

Read `START_HERE.md` and the preflight download/storage estimates before choosing a CPU/GPU setup. Setup downloads a local Python environment and packages; the first inference can download several GiB of model weights. Runtime/model estimates in the lock are planning estimates, with the CPU figures specifically marked unmeasured. Setup and inference were intentionally not run when preparing this public repository.

After independently completing the chosen profile setup, use the official commands:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\worker\worker.ps1 import -JobZip .\inbox\OCR_JOB_synthetic-demo.zip
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\worker\worker.ps1 status
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\worker\worker.ps1 run -JobId synthetic-demo
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\worker\worker.ps1 pack -JobId synthetic-demo
```

Even official `import` and `status` require the managed Python recorded by setup; they were not tested as runtime wrappers here. `run` and `pack` also require a valid active Paddle profile. Keep original protected files unchanged and use `inbox/`, `jobs/`, `logs/`, `outbox/`, and `.runtime/` for mutable data. These directories are excluded from Git.

## Verification and limits

Run the lightweight public checks with an existing Python 3.12 interpreter:

```powershell
python -B -m unittest discover -s tests -v
```

Five tests verify original SHA-256 hashes, Python syntax, the synthetic manifest/image contract, isolated **core Python** import plus `NOT_STARTED` status, duplicate job rejection, and corrupt-checksum rejection with no imported job left behind. All job mutations use disposable temporary directories. These tests exercise the unchanged Python core directly; they do not claim to exercise the official PowerShell runtime wrappers.

During publication preparation, the five tests passed, PowerShell syntax parsed, and `doctor -Profile cpu` returned `READY` for the unchanged 14-file starter. No runtime was installed; network and some system-information probes were unavailable in the restricted test environment and remained advisory.

**Not verified in this release:** clean CPU/GPU setup, model download, OCR accuracy, inference timeout/OOM behavior, resume after actual inference, or packaging a `COMPLETE` inference result. A real annual report can also exceed the configured token limit or need downstream QA. Raw OCR outputs should be reviewed before extracting financial values or using them in analysis.
