"""The real OnnxEmbedder code path, against a tiny locally built model.

A real model can't be downloaded in CI or the build sandbox, so this builds a
toy BERT-shaped ONNX graph (embedding lookup -> last_hidden_state) and a toy
tokenizer, and points the downloader at them. It exercises the actual loading,
tokenizing, padding, feeding, pooling, normalizing and batching code.
"""
import numpy as np
import pytest

onnx = pytest.importorskip("onnx")
from onnx import TensorProto, helper, numpy_helper   # noqa: E402
from tokenizers import Tokenizer, models, pre_tokenizers   # noqa: E402

from usajobs_datapull import embedders   # noqa: E402

VOCAB = ["[PAD]", "[UNK]", "[CLS]", "cat", "kitten", "mat", "rug", "sits", "rests", "congress", "budget", "bill"]
DIM = 384


def build_model(tmp_path):
    rng = np.random.default_rng(0)
    table = rng.normal(size=(len(VOCAB), DIM)).astype(np.float32)
    table[VOCAB.index("kitten")] = table[VOCAB.index("cat")] + 0.1 * rng.normal(size=DIM)
    table[VOCAB.index("rug")] = table[VOCAB.index("mat")] + 0.1 * rng.normal(size=DIM)
    table[VOCAB.index("rests")] = table[VOCAB.index("sits")] + 0.1 * rng.normal(size=DIM)
    graph = helper.make_graph(
        [helper.make_node("Gather", ["table", "input_ids"], ["last_hidden_state"], axis=0)],
        "toy",
        [helper.make_tensor_value_info("input_ids", TensorProto.INT64, ["b", "s"]),
         helper.make_tensor_value_info("attention_mask", TensorProto.INT64, ["b", "s"])],
        [helper.make_tensor_value_info("last_hidden_state", TensorProto.FLOAT, ["b", "s", DIM])],
        [numpy_helper.from_array(table, "table")],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    onnx_path = tmp_path / "model.onnx"
    onnx.save(model, onnx_path)

    tok = Tokenizer(models.WordLevel({w: i for i, w in enumerate(VOCAB)}, unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    tok_path = tmp_path / "tokenizer.json"
    tok.save(str(tok_path))
    return {"tokenizer.json": str(tok_path), "onnx/model.onnx": str(onnx_path)}


@pytest.fixture
def toy(tmp_path, monkeypatch):
    files = build_model(tmp_path)
    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", lambda repo, name, cache_dir=None: files[name])
    monkeypatch.setitem(embedders.PROFILES, "toy/mean", {
        "dim": DIM, "pooling": "mean", "max_tokens": 8, "onnx_file": "onnx/model.onnx",
        "query_prefix": "", "doc_prefix": ""})
    return embedders.OnnxEmbedder("toy/mean", cache_dir=tmp_path, batch_size=2)


def test_embeds_normalized_vectors_in_batches(toy):
    v = toy.embed(["cat sits mat", "congress budget", "kitten rests rug", "bill"])   # 2 batches of 2
    assert v.shape == (4, DIM) and v.dtype == np.float32
    assert np.allclose(np.linalg.norm(v, axis=1), 1.0, atol=1e-5)


def test_padding_does_not_change_mean_pooling(toy):
    alone = toy.embed(["cat"])[0]
    padded = toy.embed(["cat", "cat sits on the mat today"])[0]   # batched with a longer text -> padded
    assert np.allclose(alone, padded, atol=1e-5)


def test_self_check_passes_on_a_sane_model(toy):
    pairs = toy.embed(["cat sits mat", "kitten rests rug", "congress budget bill"])
    assert pairs[0] @ pairs[1] > pairs[0] @ pairs[2]
    toy_embeds = toy.embed
    toy.embed = lambda texts: toy_embeds(["cat sits mat", "kitten rests rug", "congress budget bill"])
    embedders.self_check(toy)                      # no exception


def test_self_check_fails_on_a_broken_model(toy):
    toy.embed = lambda texts: embedders.normalize_rows(np.ones((len(texts), DIM), np.float32))
    with pytest.raises(RuntimeError, match="self-check failed"):
        embedders.self_check(toy)


def test_token_counts_ignore_truncation(toy):
    text = " ".join(["cat"] * 20)
    assert toy.count_tokens(text) == 20           # max_tokens is 8, but the count is honest


def test_unknown_model_is_rejected():
    with pytest.raises(ValueError, match="not supported"):
        embedders.profile("someone/unknown-model")
