# Data contract

Open USAJOBS postings for the configured series (default 2210), cleaned,
de-duplicated, chunked and embedded. File spec: BUNDLE.md.

Dataset: [`cccc864/usajobs-postings`](https://huggingface.co/datasets/cccc864/usajobs-postings)
(private). `main` is the latest; each bundle is also tagged with its `bundle_id`.

```
https://huggingface.co/datasets/cccc864/usajobs-postings/resolve/main/<file>
https://huggingface.co/datasets/cccc864/usajobs-postings/resolve/<bundle_id>/<file>
```

## Access

- Fine-grained, read-only Hugging Face token for this dataset only, sent as
  `Authorization: Bearer <token>`. Keep it server-side or in build secrets.
- Browser apps copy the bundle in at build time; that copy is public.

## Updates

- USAJOBS is checked every 15 minutes (best effort; a run can be late or skipped).
  A new bundle is published only when the data changed, and always on the first
  run after midnight US Eastern, when `source.as_of` rolls over.
- To detect a new bundle, read `main/manifest.json` and compare `bundle_id` with
  the one you last built from. Rebuild only when it differs. Don't check more
  often than every 15 minutes.
- `built_at` (UTC) is when the current data was built, not when it was last
  checked: an unchanged check publishes nothing. An old `built_at` alone does not
  mean the pipeline stopped. `source.as_of` older than today (US Eastern) after
  about 01:00 does.

## Display

From the USAJOBS API terms. Apps showing this data should:

- name USAJOBS as the source and link each job to its `url`, the official announcement;
- show when the data was retrieved (`built_at`);
- say the app is not operated, sponsored or endorsed by OPM or USAJOBS, and that
  the data is reformatted: the official announcement is authoritative;
- send applicants to USAJOBS (`apply_url` or `url`) and never take applications.

## Promises

Every published bundle passed all of these:

1. Every posting and chunk matches its JSON Schema.
2. Every file's SHA-256 and size match the manifest.
3. `postings.id` is unique, every chunk's `posting_id` exists, every posting has a chunk.
4. One vector per chunk, same order, manifest dimension, int8 within cosine 0.99.
5. `conformance.json` is reproduced from the written files.
6. Only postings in `source.series` and open on `source.as_of`.
7. One record per posting. Separate announcements of the same job share a `group_id`.
8. Unknown is `null` or `[]`, never `""` or `"N/A"`. Plain text, no HTML.
   Grades have no leading zeros, `salary_min ≤ salary_max`, dates are `YYYY-MM-DD`.
9. Suspicious data is kept and flagged in `quality_flags`.
10. No HR contacts: contact fields are not copied, and sentences naming an
    email, phone or contact person are removed (`contact_info_redacted`).
11. Same data, same files, same `bundle_id`.
12. `schema_version` is semver; old bundles stay available by tag.

## Limits

- One series, open jobs only, one snapshot date. Hide postings past `closes_at`.
- Summary, duties, qualifications and key requirements only; link to `url` for the rest.
- City/state/country only, no coordinates.
- First salary range only. Annualized as hourly × 2087, daily × 260,
  bi-weekly × 26, monthly × 12.
- Boilerplate is removed from chunks, not from postings.
- Document vectors only: embed queries yourself with the manifest's model.
- A few hundred postings; brute-force search is fine.
