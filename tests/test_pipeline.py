"""End to end on the 2024 archive sample: normalize -> chunk -> embed -> bundle -> verify -> publish."""
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from usajobs_datapull import __main__ as cli
from usajobs_datapull import bundle, chunk, codelists, embed, normalize, publish, remote, store

FIXTURES = Path(__file__).parent / "fixtures"
LABEL = "archive-2024-11-12"


@pytest.fixture
def con(tmp_path):
    raw = tmp_path / "raw" / LABEL
    raw.mkdir(parents=True)
    shutil.copy(FIXTURES / "archive-2024-11-12-sample.json", raw / "batch.json")
    c = store.connect(tmp_path / "work.db")
    postings, quarantine, _, _ = normalize.normalize_dir(raw, codelists.load("hiringpaths", FIXTURES / "codelists"))
    normalize.save(c, LABEL, postings, quarantine)
    return c


def build(con, embedder, out):
    selected = store.select_postings(con, LABEL, ["2210"])
    rows, _ = chunk.chunk_postings(selected)
    chunk.save(con, LABEL, rows)
    embed.embed_chunks(con, store.chunks_for(con, LABEL, [p["id"] for p in selected]), embedder)
    return bundle.build(con, LABEL, embedder, ["2210"], out, built_at="2026-10-12T00:00:00+00:00")


def test_scope_is_series_and_open_on_snapshot_date(con):
    selected = store.select_postings(con, LABEL, ["2210"])
    assert selected and all("2210" in p["series"] for p in selected)
    assert all(p["closes_at"] is None or p["closes_at"] >= "2024-11-12" for p in selected)
    assert len(selected) < 28                       # the sample's non-2210 and closed postings are out


def test_embed_cache_skips_unchanged_text(con, fake_embedder):
    selected = store.select_postings(con, LABEL, ["2210"])
    rows, _ = chunk.chunk_postings(selected)
    chunk.save(con, LABEL, rows)
    chunks = store.chunks_for(con, LABEL, [p["id"] for p in selected])
    first = embed.embed_chunks(con, chunks, fake_embedder)
    second = embed.embed_chunks(con, chunks, fake_embedder)
    assert first["embedded"] == first["unique_texts"] > 0
    assert second["embedded"] == 0 and second["cached"] == first["unique_texts"]


def test_bundle_builds_verifies_and_matches_contract(con, fake_embedder, tmp_path):
    folder, m = build(con, fake_embedder, tmp_path / "dist")
    assert folder.name == "bundle" and not (tmp_path / "dist" / "bundle.partial").exists()
    names = {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}
    assert names == {"manifest.json", "postings.jsonl", "chunks.jsonl", "vectors.int8.bin", "scales.f32.bin",
                     "facets.json", "conformance.json", "quality.json", "README.md", "CONTRACT.md", "BUNDLE.md",
                     "schema/postings.schema.json", "schema/chunks.schema.json", "schema/manifest.schema.json"}
    assert m["bundle_id"].startswith("2024-11-12_")
    assert m["embedding"]["query_prefix"].startswith("Represent this sentence")
    assert m["source"] == {"api": "usajobs", "series": ["2210"], "snapshot": LABEL, "as_of": "2024-11-12",
                           "status": "open"}
    assert (folder / "vectors.int8.bin").stat().st_size == m["counts"]["chunks"] * 384
    assert bundle.verify(folder, fake_embedder)["bundle_id"] == m["bundle_id"]
    quality = json.loads((folder / "quality.json").read_text(encoding="utf-8"))
    assert quality["bundle"]["quantization_worst_cosine"] >= 0.99
    card = (folder / "README.md").read_text(encoding="utf-8")
    assert card.startswith("---\nlicense: other") and "config_name: postings" in card
    assert "CONTRACT.md" in m["files"] and "## Promises" in (folder / "CONTRACT.md").read_text(encoding="utf-8")


def test_bundle_is_deterministic(con, fake_embedder, tmp_path):
    _, a = build(con, fake_embedder, tmp_path / "one")
    _, b = build(con, fake_embedder, tmp_path / "two")
    assert a["bundle_id"] == b["bundle_id"]
    for name in ("postings.jsonl", "chunks.jsonl", "vectors.int8.bin", "conformance.json"):
        assert (tmp_path / "one/bundle" / name).read_bytes() == (tmp_path / "two/bundle" / name).read_bytes()
    assert b"\r\n" not in (tmp_path / "one/bundle/postings.jsonl").read_bytes()


def test_verify_catches_tampering(con, fake_embedder, tmp_path):
    folder, _ = build(con, fake_embedder, tmp_path / "dist")
    path = folder / "postings.jsonl"
    path.write_bytes(path.read_bytes().replace(b'"title":"', b'"title":"X', 1))
    with pytest.raises(AssertionError, match="checksum"):
        bundle.verify(folder)


class FakeApi:
    def __init__(self, remote_manifest=None, private=True, tags=()):
        self.remote, self.private, self.tag_names = remote_manifest, private, list(tags)
        self.uploads, self.tags = [], []

    def repo_info(self, repo_id, repo_type):
        return SimpleNamespace(private=self.private)

    def list_repo_refs(self, repo_id, repo_type):
        return SimpleNamespace(tags=[SimpleNamespace(name=n) for n in self.tag_names])

    def hf_hub_download(self, repo_id, filename, repo_type, force_download):
        if self.remote is None:
            from huggingface_hub.utils import EntryNotFoundError
            raise EntryNotFoundError("no manifest yet")
        return self.remote

    def upload_folder(self, **kw):
        self.uploads.append(kw)
        return type("Commit", (), {"commit_url": "https://hf.co/commit/abc"})()

    def create_tag(self, **kw):
        self.tags.append(kw)


def test_publish_uploads_once_then_is_a_no_op(con, fake_embedder, tmp_path):
    folder, m = build(con, fake_embedder, tmp_path / "dist")
    api = FakeApi()
    first = publish.publish(folder, "cccc864/usajobs-postings", api)
    assert first["status"] == "published" and first["replaced"] is None
    assert api.uploads[0]["repo_type"] == "dataset" and api.uploads[0]["delete_patterns"]
    assert api.tags[0]["tag"] == m["bundle_id"]

    api_same = FakeApi(remote_manifest=str(folder / "manifest.json"))
    again = publish.publish(folder, "cccc864/usajobs-postings", api_same)
    assert again["status"] == "unchanged" and api_same.uploads == []


def test_publish_refuses_a_public_dataset(con, fake_embedder, tmp_path):
    folder, _ = build(con, fake_embedder, tmp_path / "dist")
    api = FakeApi(private=False)
    with pytest.raises(RuntimeError, match="PUBLIC"):
        publish.publish(folder, "cccc864/usajobs-postings", api)
    assert api.uploads == [] and api.tags == []
    assert publish.publish(folder, "cccc864/usajobs-postings", api, require_private=False)["status"] == "published"


def _get(status_code):
    return lambda url, **kw: SimpleNamespace(status_code=status_code)


def test_remote_status_private_dataset_blocks_strangers(con, fake_embedder, tmp_path):
    folder, m = build(con, fake_embedder, tmp_path / "dist")
    api = FakeApi(remote_manifest=str(folder / "manifest.json"), tags=[m["bundle_id"]])
    s = remote.status(api, "cccc864/usajobs-postings", get=_get(401))
    assert s["private"] and s["anonymous_blocked"] and s["bundle_id"] == m["bundle_id"]
    assert s["tags"] == [m["bundle_id"]]


def test_remote_status_flags_a_readable_dataset():
    s = remote.status(FakeApi(private=False), "cccc864/usajobs-postings", get=_get(200))
    assert not s["private"] and not s["anonymous_blocked"] and s["bundle_id"] is None


def test_check_shows_boolean_settings(capsys):
    cli.check()
    line = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("HF_DATASET_PRIVATE"))
    assert line.split()[-1] in ("true", "false")
