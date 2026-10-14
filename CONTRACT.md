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
