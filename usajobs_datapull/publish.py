"""dist/bundle/ -> Hugging Face dataset (HF_DATASET_REPO).

Re-verifies the bundle, refuses a public dataset while HF_DATASET_PRIVATE is
true, skips if the remote bundle holds the same data (same bundle_id, or only
vector rounding differs), uploads as one commit (removing stale files) and tags
it with the bundle_id. `api` is injectable for tests.
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
# Files that change when the same text is embedded on a different CPU (int8
# rounding flips), or that only report on the build. They don't decide whether
# the data changed.
NOT_DATA = {"vectors.int8.bin", "scales.f32.bin", "posting_vectors.int8.bin", "posting_scales.f32.bin",
            "conformance.json", "quality.json"}


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


def remote_manifest(api, repo_id: str) -> dict | None:
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
    return json.loads(Path(path).read_text(encoding="utf-8"))


def remote_first_seen(api, repo_id: str) -> dict[str, str]:
    """{posting id: first_seen_at} from the published bundle; {} if there is none yet."""
    try:
        path = api.hf_hub_download(repo_id=repo_id, filename="postings.jsonl", repo_type="dataset",
                                   force_download=True)
    except Exception as e:
        name = type(e).__name__
        if name in ("EntryNotFoundError", "RemoteEntryNotFoundError", "RevisionNotFoundError"):
            return {}
        if name == "RepositoryNotFoundError":
            raise _not_found(repo_id, e) from e
        raise
    rows = (json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l)
    return {r["id"]: r["first_seen_at"] for r in rows if r.get("first_seen_at")}


def remote_bundle_id(api, repo_id: str) -> str | None:
    m = remote_manifest(api, repo_id)
    return m.get("bundle_id") if m else None


def same_data(local: dict, remote: dict | None) -> bool:
    """True if the two bundles hold the same data: same date, schema and model,
    and identical files apart from NOT_DATA."""
    if not remote:
        return False
    for key in ("schema_version", "source", "embedding"):
        if local.get(key) != remote.get(key):
            return False
    lf, rf = local.get("files", {}), remote.get("files", {})
    if set(lf) != set(rf):
        return False
    return all(lf[k].get("sha256") == rf[k].get("sha256") for k in lf if k not in NOT_DATA)


def publish(bundle_dir: Path, repo_id: str, api, require_private: bool = True) -> dict:
    from .bundle import verify
    summary = verify(bundle_dir)                   # structural checks; no model needed
    bundle_id = summary["bundle_id"]
    if require_private and not is_private(api, repo_id):
        raise RuntimeError(f"dataset {repo_id} is PUBLIC; nothing uploaded. Make it private "
                           f"(huggingface.co/datasets/{repo_id}/settings -> Change dataset visibility), "
                           f"or set HF_DATASET_PRIVATE=false in .env to publish publicly on purpose")
    remote = remote_manifest(api, repo_id)
    current = remote.get("bundle_id") if remote else None
    if current == bundle_id:
        return {"status": "unchanged", "bundle_id": bundle_id}
    local = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    if same_data(local, remote):
        log.info("only vector rounding differs from %s -- not republishing", current)
        return {"status": "unchanged", "bundle_id": current}
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
