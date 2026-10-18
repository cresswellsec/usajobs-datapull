# Bundle spec — v1

Promises and limits: [CONTRACT.md](../CONTRACT.md). Anything not written here
can change. JSON Schemas ship in the bundle (`schema/`).

## Files

| File | Format | Contents |
|---|---|---|
| `manifest.json` | JSON | What this bundle is and how to read it. Read it first. |
| `postings.jsonl` | JSON Lines, UTF-8, `\n` | One posting per line, sorted by `id` |
| `index.jsonl` | JSON Lines, UTF-8, `\n` | A compact subset of each posting, same order as `postings.jsonl` |
| `chunks.jsonl` | JSON Lines, UTF-8, `\n` | One searchable chunk per line. **Line order = vector row order.** |
| `vectors.int8.bin` | raw binary | `N × D` int8, row-major. Row *i* = chunk *i*. |
| `scales.f32.bin` | raw binary | `N` float32 little-endian, one scale per row |
| `posting_vectors.int8.bin` | raw binary | `P × D` int8, row-major. Row *k* = posting *k* of `postings.jsonl`. |
| `posting_scales.f32.bin` | raw binary | `P` float32 little-endian, one scale per row |
| `facets.json` | JSON | Filter values and counts |
| `conformance.json` | JSON | Fixed queries and expected results, for checking a consumer searches correctly |
| `quality.json` | JSON | Data-quality summary of this build (counts, flags, completeness) |
| `schema/*.schema.json` | JSON Schema 2020-12 | `postings`, `index`, `chunks`, `manifest` |
| `CONTRACT.md`, `BUNDLE.md` | Markdown | The data contract and this spec |
| `examples.json` | JSON | *Optional, not yet produced.* Example queries with pre-generated answers |
| `README.md` | Markdown | Hugging Face dataset card (human-facing, not part of the machine contract) |

## manifest.json

```json
{
  "schema_version": "1.3.0",
  "bundle_id": "2026-10-12_a1b2c3d4",
  "built_at": "2026-10-12T06:00:00+00:00",
  "source": {
    "api": "usajobs",
    "series": ["2210"],
    "snapshot": "2026-10-12",
    "as_of": "2026-10-12",
    "status": "open"
  },
  "counts": { "postings": 333, "chunks": 1400 },
  "embedding": {
    "model": "Xenova/bge-small-en-v1.5",
    "runtime": "onnx",
    "onnx_file": "onnx/model.onnx",
    "dim": 384,
    "pooling": "cls",
    "max_tokens": 512,
    "normalized": true,
    "dtype": "int8",
    "quantization": "per_row_symmetric",
    "query_prefix": "Represent this sentence for searching relevant passages: ",
    "doc_prefix": ""
  },
  "posting_embedding": {
    "method": "mean",
    "sections": ["duties", "qualifications"],
    "fallback": "all_chunks",
    "normalized": true,
    "dtype": "int8",
    "quantization": "per_row_symmetric"
  },
  "chunking": {
    "strategy": "per_section",
    "sections": ["summary", "duties", "qualifications"],
    "max_words": 180,
    "header_fields": ["title", "agency", "location", "grade"],
    "boilerplate_removed": true
  },
  "lexical": { "tokenizer": "v1", "bm25": { "k1": 1.2, "b": 0.75 } },
  "files": {
    "postings.jsonl": { "sha256": "…", "bytes": 0 }
  }
}
```

Counts and model are illustrative; **consumers read them from the manifest and
never hard-code them.**

- `bundle_id`: `as_of` date + first 8 hex chars of a SHA-256 over every file's
  checksum except `manifest.json` and `README.md`. Same content ⇒ same id;
  `built_at` is the only field that changes between identical builds.
- `built_at`: UTC time the current content was built. Unchanged checks don't
  publish, so it only moves when the data (or `as_of`) changes.
- `source.snapshot`: `YYYY-MM-DD` (fetched that day, US Eastern) or `archive-YYYY-MM-DD`.
- `source.as_of`: the date the data reflects (US Eastern).
- `files`: every file in the bundle except `manifest.json` itself.

## Which postings are in a bundle

A posting is included when **both** hold:

1. it is tagged with at least one series in `source.series`, and
2. it is open on `source.as_of`: `closes_at` is null or `closes_at >= as_of`.

Broad notices (postings listing many series) are included and flagged.

## postings.jsonl

| Field | Type | Notes |
|---|---|---|
| `id` | string | USAJOBS control number. **Primary key.** |
| `source` | string | Always `"usajobs"` |
| `position_id` | string \| null | Agency announcement number. Not unique; not a key. |
| `title` | string | Never empty |
| `agency` | string \| null | Organization name |
| `department` | string \| null | |
| `series` | string[] | 4-digit series codes, e.g. `["2210"]` |
| `pay_plan` | string \| null | e.g. `"GS"` |
| `grade_low`, `grade_high` | string \| null | No leading zeros (`"7"`, not `"07"`); letters upper-cased |
| `salary_min`, `salary_max` | number \| null | As posted, in `pay_interval` units. `min ≤ max`. |
| `pay_interval` | string \| null | e.g. `"Per Year"`, `"Per Hour"` |
| `salary_annual_min`, `salary_annual_max` | integer \| null | Annualized: hourly × 2087, daily × 260, bi-weekly × 26, monthly × 12. Null for piece work, fee basis, stipends, unpaid. |
| `locations` | object[] | `{ "name", "city", "state", "country" }`, duplicates removed. Broad notices can list hundreds. |
| `location_display` | string \| null | USAJOBS's own summary, e.g. `"Multiple Locations"` |
| `remote` | boolean \| null | The API's RemoteIndicator |
| `telework` | boolean \| null | |
| `clearance` | string \| null | As the API gives it, e.g. `"Secret"`, `"Not Required"`, `"Other"` |
| `clearance_required` | boolean \| null | `false` only for `"Not Required"` |
| `position_sensitivity` | string \| null | As the API gives it, e.g. `"Noncritical-Sensitive (NCS)/Moderate Risk"` |
| `promotion_potential` | string \| null | Highest grade the position can reach; same format as `grade_high` |
| `openings` | string \| null | As the API gives it: a number or a word such as `"Many"` |
| `hiring_paths` | string[] | Labels from the USAJOBS code list (unknown codes kept as-is) |
| `opens_at`, `closes_at` | string (`YYYY-MM-DD`) \| null | `opens_at` is the publication date |
| `closing_type` | string \| null | `closing_date`, `open_continuous`, `applicant_cutoff` or `closing_time` (USAJOBS closing-type codes 01–04) |
| `application_cap` | integer \| null | Applications after which the announcement closes. Set only when `closing_type` is `applicant_cutoff` and the API gives a number. |
| `url` | string \| null | The posting on usajobs.gov |
| `apply_url` | string \| null | |
| `questionnaire_url` | string \| null | Preview of the assessment questionnaire, when the agency provides one |
| `summary` | string \| null | Plain text |
| `duties` | string \| null | Plain text (the API's MajorDuties list, joined as sentences) |
| `qualifications` | string \| null | Plain text |
| `key_requirements` | string[] | e.g. `"U.S. Citizenship is required."` |
| `education` | string \| null | Plain text |
| `evaluations` | string \| null | Plain text: how applicants are evaluated |
| `broad_notice` | boolean | Lists 10+ series or 50+ locations |
| `fingerprint` | string (12 hex) | Same position across announcements and reposts. Built from title, agency, department, series, pay plan, grades and location names; dates, `id` and `position_id` are left out. |
| `group_id` | string (12 hex) | Postings sharing it are the same job posted as separate announcements (e.g. public vs current employees). Apps may collapse them. |
| `first_seen_at` | string (UTC, `YYYY-MM-DDTHH:MM:SS+00:00`) | When the posting first appeared in this dataset. See below. |
| `quality_flags` | string[] | See below. Flagged postings are still valid records. |

Unknown values are `null` (or `[]`), never `""`, `"N/A"` or `"Unknown"`.
HR contact details are deliberately excluded. The agency contact fields
(email, phone, website) are never copied. In the free text (`summary`,
`duties`, `qualifications`, `key_requirements`, `education`, `evaluations`), any sentence that names a
contact (an email address, a phone number, "Ms. Smith", "contact Jane Doe",
"POC: ...") is dropped whole, together with a short lead-in such as
"Questions?". The posting's `url` is the way to reach the hiring office.

### Identity over time

- `id` identifies one announcement. It never changes.
- `group_id`: announcements of the same job open over the same dates.
- `fingerprint`: the same position, including later reposts with new dates and
  new ids. Postings in one group share a fingerprint, but a fingerprint can
  span several groups.
- `first_seen_at` is the start time of the fetch that first returned the
  posting. It is carried over unchanged from bundle to bundle while the posting
  stays in the bundle. A posting that drops out and comes back gets a new value.
  Postings already in the dataset when the field was added (1.2.0) carry the
  time of that first 1.2.0 build. It is not the agency's posting date (`opens_at`).
- To list postings added since an earlier read, keep the largest
  `first_seen_at` from that read and select postings with a later value.

### Quality flags

| Flag | Meaning |
|---|---|
| `duplicate_announcement` | Shares `group_id` with another posting |
| `broad_notice` | 10+ series or 50+ locations |
| `salary_missing` | No salary given |
| `salary_min_gt_max` | Posted min > max; swapped |
| `salary_not_annualizable` | Piece work, fee basis, stipend or unpaid |
| `salary_outlier` | Annualized salary below $10k or above $1M |
| `grade_low_gt_high` | Reversed grade range; swapped |
| `closes_before_opens` | Closing date earlier than opening date |
| `missing_dates` | No opening or closing date |
| `remote_flag_mismatch` | A location says "remote job" but RemoteIndicator is false |
| `unmapped_hiring_path` | A hiring-path code not in the code list |
| `unmapped_closing_type` | A closing-type code outside 01–04; `closing_type` is null |
| `contact_info_redacted` | One or more HR-contact sentences were removed from the text |
| `missing_summary`, `missing_duties`, `missing_qualifications` | That section is empty |

## index.jsonl

One line per posting, in the same order as `postings.jsonl`, with these fields copied unchanged:
`id`, `fingerprint`, `group_id`, `first_seen_at`, `title`, `agency`, `department`, `series`, `pay_plan`,
`grade_low`, `grade_high`, `location_display`, `remote`, `hiring_paths`, `opens_at`, `closes_at`,
`closing_type`, `application_cap`, `url`. About 7% of the size of `postings.jsonl`, for consumers that
only need to list or compare postings.

## chunks.jsonl

| Field | Type | Notes |
|---|---|---|
| `chunk_id` | string | `"<posting id>:<section>:<n>"` |
| `posting_id` | string | Foreign key to `postings.id`. Every posting has at least one chunk. |
| `section` | string | One of `manifest.chunking.sections` |
| `text` | string | Exactly the text that was embedded (`doc_prefix` excluded) |
| `tokens` | integer | Token count of `text` under the embedding model's tokenizer |

Chunk text is `"<header>. <Section>: <body>"`. The header is
`Title: … | Agency: … | Location: … | Grade: …`, with absent parts left out.
Agency boilerplate (sentences repeated across many postings) is removed from
chunk text but kept in the posting record.

Chunks are ordered by `posting_id`, then section (`summary`, `duties`,
`qualifications`), then `n`.

## Vectors

- `vectors.int8.bin`: `counts.chunks × embedding.dim` signed int8, row-major, no header.
- `scales.f32.bin`: `counts.chunks` float32, little-endian, no header.
- Vectors are L2-normalized **before** quantization.
- Quantization (per-row symmetric): `scale = max(|x|) / 127`, `q = round(x / scale)`.
  The build checks every dequantized row stays within cosine 0.99 of the original.
- Dequantize: `x ≈ q × scale`.
- Similarity: dot product of the query vector with the dequantized row.
- Queries: embed `query_prefix + query_text` with `embedding.model`
  (`embedding.onnx_file`), pool with `embedding.pooling` (`cls` = first token,
  `mean` = attention-masked mean), truncate at `embedding.max_tokens`, L2-normalize.
  In Transformers.js: `pipeline('feature-extraction', model, { pooling, normalize: true })`.

## Posting vectors

- One vector per posting: the mean of that posting's chunk vectors in `posting_embedding.sections`
  (`duties`, `qualifications`), or of all its chunks if it has none of those sections. Computed from the
  float vectors, L2-normalized, then quantized the same way as chunk vectors.
- `posting_vectors.int8.bin`: `counts.postings × embedding.dim` int8; `posting_scales.f32.bin`: one float32
  per row. Row *k* is posting *k* of `postings.jsonl` and `index.jsonl`.
- Similarity between two postings: dot product of their dequantized rows. The build checks every row stays
  within cosine 0.99 of the float vector.

## Keyword search: tokenizer v1 and BM25

Consumers build their own BM25 index over `chunks.jsonl` `text`, exactly like this:

**Tokenizer v1**
1. Unicode NFKC, then lowercase.
2. Tokens = every match of `[a-z0-9]+`.
3. Additionally, for every match of `[a-z0-9]+(?:[-/][a-z0-9]+)+` (e.g. `gs-13`,
   `ts/sci`), also emit the joined form without separators (`gs13`, `tssci`).
4. No stop words, no stemming.

**BM25** (`manifest.lexical.bm25`)
- `idf(t) = ln(1 + (N − df(t) + 0.5) / (df(t) + 0.5))`
- `score(d) = Σ idf(t) · tf · (k1 + 1) / (tf + k1 · (1 − b + b · |d| / avgdl))`
  over the query's **distinct** terms; `|d|` = token count of the chunk.
- Only chunks with score > 0 are results.

**Ties**, in both lexical and dense search: the lower row index ranks first.

A future change gets a new name (`v2`), and `manifest.lexical.tokenizer` says which applies.

## facets.json

```json
{
  "agency":      [{ "value": "Department of the Air Force", "count": 41 }],
  "department":  [{ "value": "Department of Defense", "count": 120 }],
  "grade":       [{ "value": "GS-13", "count": 88 }],
  "state":       [{ "value": "Virginia", "count": 52 }],
  "clearance":   [{ "value": "Secret", "count": 120 }],
  "remote":      [{ "value": true, "count": 17 }],
  "telework":    [{ "value": true, "count": 140 }],
  "hiring_path": [{ "value": "The public", "count": 200 }],
  "series":      [{ "value": "2210", "count": 333 }],
  "broad_notice":[{ "value": false, "count": 300 }]
}
```

Sorted by count (descending), then value. `grade` uses `pay_plan-grade_low`.

## conformance.json

```json
{
  "k": 5,
  "cases": [
    { "query": "cybersecurity analyst incident response", "mode": "dense",   "expected_top": ["<chunk_id>", "…"] },
    { "query": "GS-13 TS/SCI",                            "mode": "lexical", "expected_top": ["<chunk_id>", "…"] }
  ]
}
```

Generated by the pipeline's reference retriever (`usajobs_datapull/retrieval.py`)
from the written int8 files. A consumer **passes** if, for every case, its
top-`k` chunk ids match `expected_top` exactly for `lexical`, and share at
least `k − 1` ids for `dense` (a different ONNX runtime or a quantized model
in the browser can swap near-ties).

## quality.json

Counts from each stage of the build (`normalize`, `chunk`, `embed`) and, for the
bundled postings, flag counts, field completeness, and the worst quantization
cosine. Informational; its shape may grow in minor versions.

## Versioning and publishing

- `schema_version` is semver. **Major** = breaking (consumers must update);
  **minor** = additive (new optional fields or files); **patch** = docs.
- Consumers check the major version and refuse bundles they don't support.

| Version | Change |
|---|---|
| 1.3.0 | Added `index.jsonl` (compact posting subset), `posting_vectors.int8.bin` + `posting_scales.f32.bin` (one vector per posting), `schema/index.schema.json`, manifest `posting_embedding` |
| 1.2.0 | Postings: added `source`, `position_sensitivity`, `promotion_potential`, `openings`, `closing_type`, `application_cap`, `questionnaire_url`, `education`, `evaluations`, `fingerprint`, `first_seen_at`. Flag `unmapped_closing_type`. |
| 1.1.4 | Update cadence, change detection and display rules documented |
- Each publish is one Hugging Face commit tagged with the `bundle_id`.
  `main` is always the latest. Publishing an identical bundle is a no-op.
- The pipeline checks every 15 minutes and publishes only on a change (see
  CONTRACT.md → Updates).
- The dataset is private (see CONTRACT.md → Access). `publish` refuses a
  public dataset while `HF_DATASET_PRIVATE=true` (the default).

## Consumer rules

0. Authenticate with your own read-only token, server-side only.
1. Read `manifest.json` first. Check the schema major version.
2. Verify each file's `sha256` against the manifest.
3. Take the embedding model, ONNX file, pooling, prefixes, dimension and tokenizer
   **from the manifest**. A model mismatch is a hard error: the vectors would be meaningless.
4. Hide postings whose `closes_at` has passed (the bundle may be hours old).
5. Pass `conformance.json` in CI.
6. Detect updates by comparing `manifest.bundle_id`, never `built_at`.
7. Follow CONTRACT.md → Display (source, link, retrieval date, non-affiliation).
