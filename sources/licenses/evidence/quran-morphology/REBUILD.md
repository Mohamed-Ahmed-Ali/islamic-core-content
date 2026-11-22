# Quran Morphology Corresponding Source Rebuild Instructions

This archive contains the corresponding source for `quran-morphology.db` under the GNU General Public License v3.0.

## 1. Reference Environment Bootstrap (§5)

Build the pinned reference container image from the archive root:

```sh
docker build --platform linux/amd64 -t content-reference:latest -f pipelines/toolchain/Dockerfile .
```

## 2. Runnable Rebuild Execution

From the archive root, verify locked inputs and build the morphology pack inside the reference container:

```sh
mkdir -p dist/morphology
docker run --rm --platform linux/amd64 --network none --read-only \
  --tmpfs /tmp:rw,nosuid,nodev -e PYTHONDONTWRITEBYTECODE=1 -e HOME=/tmp -e TMPDIR=/tmp \
  --mount "type=bind,src=$(pwd),dst=/repo,readonly" \
  --mount "type=bind,src=$(pwd)/dist,dst=/repo/dist" \
  -w /repo content-reference:latest \
  bash -lc 'set -euo pipefail; python -m pipelines.common.lock_inputs --registry sources/registry.json --raw-root raw-inputs --output sources/manifest.lock.json --verify --pack quran-morphology && python -m pipelines.builders.build_quran_morphology --sources-root . --output-dir dist/morphology'
```

## 3. Individual Step Reference

### Lock Verification

```sh
python -m pipelines.common.lock_inputs \
  --registry sources/registry.json \
  --raw-root raw-inputs \
  --output sources/manifest.lock.json \
  --verify \
  --pack quran-morphology
```

### Pack Build

```sh
python -m pipelines.builders.build_quran_morphology \
  --sources-root . \
  --output-dir dist/morphology
```

No inputs for other packs are needed.
