"""A disposable ONNX graph for offline CI, not a replacement model artifact.

The shipped Python ONNX pipeline runs this graph to record exact goldens for
tokenization, pooling, batching and cache behavior. Real-model parity remains
the separate embedding row and is never inferred from this fixture.
"""
from __future__ import annotations

import json
from pathlib import Path


def create(root: Path):
    import numpy as np
    import onnx
    from onnx import TensorProto, helper, numpy_helper
    from tokenizers import Tokenizer, models, pre_tokenizers
    root.mkdir(parents=True)
    (root / "onnx").mkdir()
    (root / "1_Pooling").mkdir()
    vocab = {"[PAD]": 0, "[UNK]": 1, "memory": 2, "cursor": 3, "cache": 4,
             "query": 5, "document": 6, "normalize": 7, "partition": 8}
    tok = Tokenizer(models.WordLevel(vocab, unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    tok.save(str(root / "tokenizer.json"))
    (root / "tokenizer_config.json").write_text(json.dumps({
        "tokenizer_class": "PreTrainedTokenizerFast", "pad_token": "[PAD]",
        "unk_token": "[UNK]", "model_max_length": 128}), encoding="utf-8")
    (root / "config.json").write_text(json.dumps({"model_type": "bert", "hidden_size": 4,
        "num_hidden_layers": 1, "num_attention_heads": 1, "intermediate_size": 4,
        "max_position_embeddings": 128, "vocab_size": len(vocab)}), encoding="utf-8")
    (root / "sentence_bert_config.json").write_text(json.dumps({"max_seq_length": 128,
        "do_lower_case": False}), encoding="utf-8")
    (root / "modules.json").write_text(json.dumps([
        {"idx": 0, "name": "0", "path": "", "type": "sentence_transformers.models.Transformer"},
        {"idx": 1, "name": "1", "path": "1_Pooling", "type": "sentence_transformers.models.Pooling"},
    ]), encoding="utf-8")
    (root / "1_Pooling/config.json").write_text(json.dumps({"word_embedding_dimension": 4,
        "pooling_mode_mean_tokens": True, "pooling_mode_cls_token": False}), encoding="utf-8")
    weights = np.zeros((len(vocab), 4), dtype="f4")
    weights[:, 0] = np.arange(1, len(vocab) + 1)
    graph = helper.make_graph([helper.make_node("Gather", ["weights", "input_ids"], ["last_hidden_state"], axis=0)],
        "offline-embedding-fixture", [helper.make_tensor_value_info("input_ids", TensorProto.INT64, ["batch", "tokens"])],
        [helper.make_tensor_value_info("last_hidden_state", TensorProto.FLOAT, ["batch", "tokens", 4])],
        [numpy_helper.from_array(weights, "weights")])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 9
    onnx.checker.check_model(model)
    onnx.save(model, root / "onnx/model.onnx")


if __name__ == "__main__":
    import sys
    create(Path(sys.argv[1]))
