# resume_store — local artifact store for tailoring

This directory lets you run the tailoring pipeline locally **without S3**.

## Quick start

```bash
# 1. Point the pipeline at this directory
export RESUME_STORE_DIR=$(pwd)/resume_store

# 2. Replace the placeholder templates with your real résumés
#    (one per resume type; see base/ below)

# 3. Run tailoring as usual
python -m jobmaxxing.tailor <job_id>

# Artifacts land under resume_store/tailored/<job_id>/
#   tailored.tex  — final LaTeX
#   tailored.pdf  — compiled PDF  (requires pdflatex — see below)
#   review.json   — scores + critique
#   diff.txt      — unified diff vs. base
```

## Directory layout

```
resume_store/
  base/
    swe/main.tex          # ← replace with your real SWE résumé
    mle/main.tex
    quant-trader/main.tex
    quant-dev/main.tex
    fdse/main.tex
    ai/main.tex
    robotics/main.tex
    av/main.tex
  tailored/               # created automatically on first run
    <job_id>/
      tailored.tex
      tailored.pdf
      review.json
      diff.txt
```

## Store selection (env vars)

| Env var | Effect |
|---|---|
| `RESUME_STORE_DIR` | Use this directory (local filesystem, no AWS) |
| `S3_BUCKET` | Use S3 (production) |
| neither | Error — set one |

`RESUME_STORE_DIR` takes priority over `S3_BUCKET` when both are set.

## Replacing the placeholder templates

Each `base/{type}/main.tex` is a minimal, compilable LaTeX résumé scaffold —
**not** your real résumé. Replace it:

```bash
cp ~/my-swe-resume.tex resume_store/base/swe/main.tex
```

Keep the filename `main.tex`; the store key is always `base/{type}/main.tex`.

## pdflatex requirement

Compiling the tailored PDF requires `pdflatex` (part of TeX Live / MacTeX):

```bash
brew install basictex          # minimal install (~100 MB)
# or
brew install --cask mactex     # full install (~4 GB)
```

`pdflatex` must be on `$PATH`. If it is not installed, the tailoring run will
fail at the compile step; the `.tex` artifact is still written and usable.
