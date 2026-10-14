# usajobs-datapull

Data pipeline for [cresswell.io/2210StaticSite](https://cresswell.io/2210StaticSite/):
open federal IT Specialist (2210) jobs from the USAJOBS Search API, cleaned,
chunked, embedded and published as a versioned bundle to a private Hugging Face dataset.

```
fetch → normalize → chunk → embed → bundle → publish
```

Bundle format: [CONTRACT.md](CONTRACT.md), [docs/BUNDLE.md](docs/BUNDLE.md).

Not affiliated with USAJOBS or OPM. Job posting content is U.S. government data
from USAJOBS. MIT licensed, provided as-is, no support.
