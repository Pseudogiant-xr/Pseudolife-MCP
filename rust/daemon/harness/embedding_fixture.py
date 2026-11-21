"""A disposable ONNX graph for offline CI, not a replacement model artifact.

The shipped Python ONNX pipeline runs this graph to record exact goldens for
tokenization, pooling, batching and cache behavior. Real-model parity remains
the separate embedding row and is never inferred from this fixture.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path


VOCAB = {"[PAD]": 0, "[UNK]": 1, "memory": 2, "cursor": 3, "cache": 4,
         "query": 5, "document": 6, "normalize": 7, "partition": 8}
MAX_TOKENS = 128


def mean_unit_vector(text, dimension=1024):
    """The unit vector a daemon embeds ``text`` as under the mean-pooled
    fixture, from this file's definition alone: whitespace word pieces
    (``tokenizers``' ``Whitespace``), unknown pieces as ``[UNK]``, at most
    ``MAX_TOKENS`` of them (the graph's own cap; a config can lower it), the
    mean of their ``fixture_row`` rows, then L2 normalization. Every row
    is (4, +-3, 0, ...), so only the share of even and odd token ids moves the
    direction."""
    pieces = re.findall(r"\w+|[^\w\s]+", text)[:MAX_TOKENS]
    if not pieces:
        raise ValueError("the fixture embeds no empty text")
    rows = [fixture_row(VOCAB.get(piece, VOCAB["[UNK]"]), dimension) for piece in pieces]
    mean = [sum(row[column] for row in rows) / len(rows) for column in range(dimension)]
    norm = math.sqrt(sum(value * value for value in mean))
    return [value / norm for value in mean]


def fixture_row(token_id, dimension=4):
    """Token ``token_id``'s row of the graph's weights, without numpy (the
    hosted Rust lane installs none). Two active columns keep the exact fp32
    norm arithmetic simple while retaining direction changes, mixed signs and
    nonzero padding values."""
    return [4.0, -3.0 if token_id % 2 else 3.0] + [0.0] * (dimension - 2)


def fixture_weights(size, dimension=4):
    import numpy as np
    return np.array([fixture_row(i, dimension) for i in range(size)], dtype="f4")


def create(root: Path, *, last_token=False, dimension=4):
    import numpy as np
    import onnx
    from onnx import TensorProto, helper, numpy_helper
    from tokenizers import Tokenizer, models, pre_tokenizers
    root.mkdir(parents=True)
    (root / "onnx").mkdir()
    (root / "1_Pooling").mkdir()
    vocab = dict(VOCAB)
    tok = Tokenizer(models.WordLevel(vocab, unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    tok.save(str(root / "tokenizer.json"))
    (root / "tokenizer_config.json").write_text(json.dumps({
        "tokenizer_class": "PreTrainedTokenizerFast", "pad_token": "[PAD]",
        "unk_token": "[UNK]", "model_max_length": 128,
        "padding_side": "left" if last_token else "right"}), encoding="utf-8")
    (root / "config.json").write_text(json.dumps({"model_type": "bert", "hidden_size": dimension,
        "num_hidden_layers": 1, "num_attention_heads": 1, "intermediate_size": 4,
        "max_position_embeddings": 128, "vocab_size": len(vocab)}), encoding="utf-8")
    (root / "sentence_bert_config.json").write_text(json.dumps({"max_seq_length": 128,
        "do_lower_case": False}), encoding="utf-8")
    modules = [
        {"idx": 0, "name": "0", "path": "", "type": "sentence_transformers.models.Transformer"},
        {"idx": 1, "name": "1", "path": "1_Pooling", "type": "sentence_transformers.models.Pooling"},
    ]
    if last_token:
        modules.append({"idx": 2, "name": "2", "path": "2_Normalize", "type": "sentence_transformers.models.Normalize"})
    (root / "modules.json").write_text(json.dumps(modules), encoding="utf-8")
    (root / "1_Pooling/config.json").write_text(json.dumps({"word_embedding_dimension": dimension,
        "pooling_mode_mean_tokens": not last_token, "pooling_mode_lasttoken": last_token,
        "pooling_mode_cls_token": False}), encoding="utf-8")
    weights = fixture_weights(len(vocab), dimension)
    inputs = [helper.make_tensor_value_info("input_ids", TensorProto.INT64, ["batch", "tokens"])]
    nodes = []
    constants = [numpy_helper.from_array(weights, "weights")]
    ids = "input_ids"
    if last_token:
        # All extra inputs affect the lookup, so their omission or wrong
        # values cannot be hidden by an optimizer or a constant zero branch.
        for i, name in enumerate(["attention_mask", "token_type_ids", "position_ids"]):
            inputs.append(helper.make_tensor_value_info(name, TensorProto.INT64, ["batch", "tokens"]))
            result = f"ids_{i}"
            nodes.append(helper.make_node("Add", [ids, name], [result]))
            ids = result
        constants.append(numpy_helper.from_array(np.array(len(vocab), dtype="int64"), "vocab_size"))
        nodes.append(helper.make_node("Mod", [ids, "vocab_size"], ["lookup_ids"]))
        ids = "lookup_ids"
    nodes.append(helper.make_node("Gather", ["weights", ids], ["last_hidden_state"], axis=0))
    graph = helper.make_graph(nodes,
        "offline-embedding-fixture", inputs,
        [helper.make_tensor_value_info("last_hidden_state", TensorProto.FLOAT, ["batch", "tokens", dimension])],
        constants)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 9
    onnx.checker.check_model(model)
    onnx.save(model, root / "onnx/model.onnx")


if __name__ == "__main__":
    import sys
    create(Path(sys.argv[1]))
