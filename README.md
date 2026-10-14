# usajobs-datapull

Data pipeline for [cresswell.io/2210StaticSite](https://cresswell.io/2210StaticSite/):
open federal IT Specialist (2210) jobs from the USAJOBS Search API, cleaned,
chunked, embedded and published as a versioned bundle to a private Hugging Face dataset.

Not affiliated with USAJOBS or OPM. MIT licensed, no support.

```
fetch → normalize → chunk → embed → bundle → publish
```

| Stage | |
|---|---|
| `fetch` | API → raw pages, saved untouched |
| `normalize` | one clean record per posting; dedupe, quality flags, HR contacts removed |
| `chunk` | postings → passages, boilerplate dropped |
| `embed` | ONNX model (`Xenova/bge-small-en-v1.5`), cached by text hash |
| `bundle` | write + verify (schemas, checksums, vectors, search conformance) |
| `publish` | upload one tagged commit; no-op if unchanged |
| `probe` | measure the API's limits |

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env    # fill in keys
python -m usajobs_datapull check
python -m usajobs_datapull all
python -m pytest
```

Bundle format: [CONTRACT.md](CONTRACT.md), [docs/BUNDLE.md](docs/BUNDLE.md).

Job posting content is U.S. government data from USAJOBS.
