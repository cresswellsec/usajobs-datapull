"""dist/bundle/ -> Hugging Face dataset (HF_DATASET_REPO).

Re-verifies the bundle, refuses a public dataset while HF_DATASET_PRIVATE is
true, skips if the remote bundle_id matches, uploads as one commit (removing
stale files) and tags it with the bundle_id. `api` is injectable for tests.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from . import config

log = logging.getLogger("usajobs_datapull.publish")

# Only bundle files are ever deleted from the repo (never .gitattributes etc.).
BUNDLE_PATTERNS = ["*.json", "*.jsonl", "*.bin", "*.md", "schema/*"]


def _not_found(repo_id: str, e: Exception) -> RuntimeError:
    return RuntimeError(f"dataset {repo_id} not found, or HF_TOKEN can't see it "
                        f"(a private dataset needs a token with access to it)")


def is_private(api, repo_id: str) -> bool:
    try:
        return bool(api.repo_info(repo_id=repo_id, repo_type="dataset").private)
    except Exception as e:
        if type(e).__name__ == "RepositoryNotFoundError":
            raise _not_found(repo_id, e) from e
        raise


def remote_bundle_id(api, repo_id: str) -> str | None:
    try:
        path = api.hf_hub_download(repo_id=repo_id, filename="manifest.json", repo_type="dataset",
                                   force_download=True)
    except Exception as e:                         # missing file, empty repo, ...
        name = type(e).__name__
        if name in ("EntryNotFoundError", "RemoteEntryNotFoundError", "RevisionNotFoundError"):
            return None
        if name == "RepositoryNotFoundError":
            raise _not_found(repo_id, e) from e
        raise
    return json.loads(Path(path).read_text(encoding="utf-8")).get("bundle_id")


def publish(bundle_dir: Path, repo_id: str, api, require_private: bool = True) -> dict:
    from .bundle import verify
    summary = verify(bundle_dir)                   # structural checks; no model needed
    bundle_id = summary["bundle_id"]
    if require_private and not is_private(api, repo_id):
        raise RuntimeError(f"dataset {repo_id} is PUBLIC; nothing uploaded. Make it private "
                           f"(huggingface.co/datasets/{repo_id}/settings -> Change dataset visibility), "
                           f"or set HF_DATASET_PRIVATE=false in .env to publish publicly on purpose")
    current = remote_bundle_id(api, repo_id)
    if current == bundle_id:
        return {"status": "unchanged", "bundle_id": bundle_id}
    commit = api.upload_folder(
        repo_id=repo_id, repo_type="dataset", folder_path=str(bundle_dir),
        commit_message=f"Bundle {bundle_id}",
        commit_description=f"{summary['postings']} postings, {summary['chunks']} chunks. Replaces {current or 'nothing'}.",
        delete_patterns=BUNDLE_PATTERNS,
    )
    api.create_tag(repo_id=repo_id, repo_type="dataset", tag=bundle_id, exist_ok=True)
    return {"status": "published", "bundle_id": bundle_id, "replaced": current,
            "commit": getattr(commit, "commit_url", str(commit))}


def write_result(result: dict, path: Path | None = None) -> None:
    """Record what this publish did, for automation (CI summary, the site rebuild trigger)."""
    path = path or config.PUBLISH_RESULT
    path.parent.mkdir(parents=True, exist_ok=True)
    keep = {k: result.get(k) for k in ("status", "bundle_id", "replaced")}
    path.write_text(json.dumps(keep, sort_keys=True) + "\n", encoding="utf-8")


def run() -> None:
    config.require("HF_TOKEN", "HF_DATASET_REPO")
    config.PUBLISH_RESULT.unlink(missing_ok=True)   # never leave a stale result behind a failed run
    from huggingface_hub import HfApi
    from . import log as logsetup
    logsetup.setup("publish")
    folder = config.BUNDLE_DIR
    if not (folder / "manifest.json").exists():
        print(f"no bundle at {folder} -- run `bundle` first", file=sys.stderr)
        raise SystemExit(1)
    try:
        result = publish(folder, config.HF_DATASET_REPO, HfApi(token=config.HF_TOKEN),
                         require_private=config.HF_DATASET_PRIVATE)
    except (AssertionError, RuntimeError) as e:
        print(f"publish failed: {e}", file=sys.stderr)
        raise SystemExit(1)
    write_result(result)
    if result["status"] == "unchanged":
        print(f"publish: {config.HF_DATASET_REPO} already has bundle {result['bundle_id']} -- nothing to do")
    else:
        print(f"publish: {result['bundle_id']} -> https://huggingface.co/datasets/{config.HF_DATASET_REPO}")
        print(f"  replaced {result['replaced'] or '(first publish)'}; tagged {result['bundle_id']}")
