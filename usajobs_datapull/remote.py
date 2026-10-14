"""`remote` -- what is published, and who can read it.

Read-only. With HF_TOKEN it reports the dataset's visibility, the latest
bundle_id and the bundle tags. Then it tries to download manifest.json with
NO token, the way a stranger would: for a private dataset that must fail
(401/403/404). Nothing is changed and no secret is printed.
"""
from __future__ import annotations

import sys

from . import config
from .publish import is_private, remote_bundle_id

ANON_TIMEOUT = (10, 30)


def manifest_url(repo_id: str) -> str:
    return f"https://huggingface.co/datasets/{repo_id}/resolve/main/manifest.json"


def anonymous_status(repo_id: str, get=None) -> int:
    """HTTP status a stranger (no token) gets for manifest.json."""
    if get is None:
        import requests
        get = requests.get
    resp = get(manifest_url(repo_id), timeout=ANON_TIMEOUT, allow_redirects=True)
    return resp.status_code


def status(api, repo_id: str, get=None) -> dict:
    private = is_private(api, repo_id)
    refs = api.list_repo_refs(repo_id=repo_id, repo_type="dataset")
    anon = anonymous_status(repo_id, get)
    return {
        "repo": repo_id,
        "private": private,
        "bundle_id": remote_bundle_id(api, repo_id),
        "tags": sorted(t.name for t in getattr(refs, "tags", []) or []),
        "anonymous_status": anon,
        "anonymous_blocked": anon in (401, 403, 404),
    }


def run() -> None:
    config.require("HF_TOKEN", "HF_DATASET_REPO")
    from huggingface_hub import HfApi
    try:
        s = status(HfApi(token=config.HF_TOKEN), config.HF_DATASET_REPO)
    except RuntimeError as e:
        print(f"remote: {e}", file=sys.stderr)
        raise SystemExit(1)
    print(f"remote {s['repo']}")
    print(f"  visibility         {'private' if s['private'] else 'PUBLIC'}")
    print(f"  latest bundle      {s['bundle_id'] or '(none published)'}")
    print(f"  tags               {', '.join(s['tags']) or '(none)'}")
    verdict = "blocked (good)" if s["anonymous_blocked"] else "ALLOWED -- anyone can download it"
    print(f"  anonymous download {verdict} [HTTP {s['anonymous_status']}]")
    if config.HF_DATASET_PRIVATE and not (s["private"] and s["anonymous_blocked"]):
        print("remote: the dataset should be private (HF_DATASET_PRIVATE=true) but is readable without a token",
              file=sys.stderr)
        raise SystemExit(1)
