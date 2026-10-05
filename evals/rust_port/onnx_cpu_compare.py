"""Numerical Torch/ORT CPU comparison over a proposed deterministic corpus.

This feeds the exported graph directly because the pinned SentenceTransformers
ONNX wrapper omits Qwen3 position_ids. It is not daemon or shipped-backend parity.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import random
import subprocess
import time

PINS = {'torch': '2.13.0+cpu', 'sentence-transformers': '5.6.0',
        'transformers': '4.57.6', 'tokenizers': '0.22.2', 'optimum': '2.1.0',
        'optimum-onnx': '0.1.0', 'onnx': '1.22.0', 'onnxruntime': '1.27.0'}
PREFIX = 'Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:'
REVISION = '97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3'


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def git(source, *args):
    return subprocess.check_output(['git', *args], cwd=source, text=True).strip()


def definitions(source, relative, names):
    raw = subprocess.check_output(['git', 'show', f'HEAD:{relative}'], cwd=source)
    tree = ast.parse(raw)
    selected = [node for node in tree.body if
                (isinstance(node, ast.FunctionDef) and node.name in names) or
                (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets))]
    scope = {'random': random, 'math': math}
    exec(compile(ast.Module(body=selected, type_ignores=[]), relative, 'exec'), scope)
    return scope, hashlib.sha256(raw).hexdigest()


def materialize(source):
    corpus, corpus_hash = definitions(source, 'evals/rust_baseline/daemon.py', {'SEED', 'TOPICS', 'corpus'})
    vectors, vector_hash = definitions(source, 'evals/rust_baseline/scaling.py', {'DIMENSION', 'vector'})
    queries = [f'Project {corpus["TOPICS"][i % len(corpus["TOPICS"])]} calibration probe {i:05d}' for i in range(25)]
    manifest = {'status': 'PROPOSED deterministic 25-query corpus; maintainer decision pending',
                'seed': corpus['SEED'], 'documents': [entry['text'] for entry in corpus['corpus'](1000)],
                'queries': queries, 'query_prefix': PREFIX,
                'corpus_generator_sha256': corpus_hash, 'bank_generator_sha256': vector_hash,
                'bank_definition': 'vector(SEED+i), format .9g then pgvector float32, IDs 1..2000; no bank re-embedding'}
    return manifest, vectors['vector']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--materialize-only', action='store_true')
    parser.add_argument('--smoke', action='store_true')
    args = parser.parse_args()
    os.environ.update(CUDA_VISIBLE_DEVICES='-1', OMP_NUM_THREADS='4', MKL_NUM_THREADS='4',
                      OPENBLAS_NUM_THREADS='4', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                      TOKENIZERS_PARALLELISM='false')
    manifest, vector = materialize(args.source)
    encoded = json.dumps(manifest, indent=2, ensure_ascii=False) + '\n'
    args.manifest.write_text(encoded, encoding='utf-8')
    if args.materialize_only:
        assert len(manifest['documents']) == 1000 and len(manifest['queries']) == 25
        assert len(set(manifest['queries'])) == 25
        print('materialized', hashlib.sha256(encoded.encode()).hexdigest(), flush=True)
        return
    # The full comparison requires its exact instrument to be committed first.
    script = Path(__file__).resolve()
    relative = script.relative_to(args.source.resolve()).as_posix()
    committed = subprocess.check_output(['git', 'show', f'HEAD:{relative}'], cwd=args.source)
    if script.read_text(encoding='utf-8').replace('\r\n', '\n') != committed.decode().replace('\r\n', '\n'):
        raise RuntimeError('comparison instrument differs from committed HEAD')
    versions = {name: importlib.metadata.version(name) for name in PINS}
    if versions != PINS:
        raise RuntimeError(f'pin mismatch: {versions}')
    print('comparison-start', 'smoke' if args.smoke else 'full', time.time(), flush=True)
    import numpy as np
    import torch
    import onnxruntime as ort
    import onnx
    from sentence_transformers import SentenceTransformer
    torch.set_num_threads(4)
    torch.set_num_interop_threads(1)
    assert torch.version.cuda is None
    graph_path = args.model / 'onnx' / 'model.onnx'
    graph = onnx.load(str(graph_path), load_external_data=False)
    floating_types = {tensor.data_type for tensor in graph.graph.initializer if tensor.data_type in (1, 10, 11, 16)}
    if floating_types != {onnx.TensorProto.FLOAT}:
        raise RuntimeError(f'graph floating initializer types: {floating_types}')
    docs = manifest['documents'][:1] if args.smoke else manifest['documents']
    queries = manifest['queries'][:1] if args.smoke else manifest['queries']
    texts = docs + [PREFIX + query for query in queries]
    started = time.perf_counter()
    model = SentenceTransformer(str(args.model), backend='torch', device='cpu',
                                model_kwargs={'torch_dtype': torch.float32, 'local_files_only': True})
    model.max_seq_length = min(model.max_seq_length, 512)
    sequence_cap = model.max_seq_length
    dtypes = sorted({str(p.dtype) for p in model.parameters()})
    assert dtypes == ['torch.float32']
    pooling, normalization = model[1], model[2]
    features_list, identities, torch_rows = [], [], []
    unchanged_delta = None
    with torch.inference_mode():
        for i, text in enumerate(texts):
            features = model.tokenize([text])
            ids = features['input_ids']
            features['position_ids'] = torch.arange(ids.shape[1]).unsqueeze(0).expand_as(ids)
            out = model.forward(dict(features))['sentence_embedding']
            out = torch.nn.functional.normalize(out, p=2, dim=1).cpu().numpy()[0]
            if i in (0, len(docs)):
                ordinary = model.encode([text], batch_size=1, normalize_embeddings=True, show_progress_bar=False)[0]
                delta = float(np.max(np.abs(out - ordinary)))
                unchanged_delta = max(unchanged_delta or 0.0, delta)
                assert np.array_equal(out, ordinary)
            tensor_features = {key: value for key, value in features.items() if isinstance(value, torch.Tensor)}
            features_list.append(tensor_features)
            identities.append({key: {'shape': list(value.shape), 'dtype': str(value.dtype),
                                     'sha256': hashlib.sha256(value.numpy().tobytes()).hexdigest()}
                               for key, value in tensor_features.items()})
            torch_rows.append(out)
            if (i + 1) % 50 == 0:
                print('torch-rows', i + 1, 'seconds', time.perf_counter() - started, flush=True)
    torch_end = time.perf_counter()
    del model
    import gc
    gc.collect()
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(str(graph_path), sess_options=options, providers=['CPUExecutionProvider'])
    assert session.get_providers() == ['CPUExecutionProvider']
    names = [value.name for value in session.get_inputs()]
    outputs = [value.name for value in session.get_outputs()]
    assert outputs == ['last_hidden_state']
    ort_rows = []
    for i, features in enumerate(features_list):
        feed = {name: features[name].numpy() for name in names}
        hidden = session.run(outputs, feed)[0]
        pooled = pooling({'token_embeddings': torch.from_numpy(hidden), 'attention_mask': features['attention_mask']})
        normalized = normalization(pooled)['sentence_embedding']
        ort_rows.append(torch.nn.functional.normalize(normalized, p=2, dim=1).numpy()[0])
        if (i + 1) % 50 == 0:
            print('ort-rows', i + 1, 'seconds', time.perf_counter() - torch_end, flush=True)
    ort_end = time.perf_counter()
    a, b = np.stack(torch_rows), np.stack(ort_rows)
    assert a.shape == b.shape == (len(texts), 1024) and a.dtype == b.dtype == np.float32
    assert np.isfinite(a).all() and np.isfinite(b).all()
    cosines = np.sum(a.astype(np.float64) * b.astype(np.float64), axis=1) / (
        np.linalg.norm(a.astype(np.float64), axis=1) * np.linalg.norm(b.astype(np.float64), axis=1))
    bank = np.array([[float(format(v, '.9g')) for v in vector(61003 + i)] for i in range(2000)], dtype=np.float32)
    ranks = []
    for index, query in enumerate(queries):
        row = len(docs) + index
        score_a = bank @ a[row] / (np.linalg.norm(bank, axis=1) * np.linalg.norm(a[row]))
        score_b = bank @ b[row] / (np.linalg.norm(bank, axis=1) * np.linalg.norm(b[row]))
        ids = np.arange(1, 2001)
        oa, ob = np.lexsort((ids, -score_a)), np.lexsort((ids, -score_b))
        disagreements = [{'rank': k + 1, 'torch_id': int(oa[k] + 1), 'ort_id': int(ob[k] + 1),
                          'torch_adjacent_margin': float(score_a[oa[k-1]] - score_a[oa[k]]) if k else None,
                          'ort_adjacent_margin': float(score_b[ob[k-1]] - score_b[ob[k]]) if k else None}
                         for k in range(2000) if oa[k] != ob[k]]
        ranks.append({'query': query, 'exact_equal': np.array_equal(oa, ob),
                      'top8_equal': np.array_equal(oa[:8], ob[:8]), 'disagreements': disagreements,
                      'torch_ordered_ids': (oa + 1).tolist(), 'ort_ordered_ids': (ob + 1).tolist(),
                      'torch_ordered_scores': score_a[oa].tolist(), 'ort_ordered_scores': score_b[ob].tolist(),
                      'max_score_difference': float(np.max(np.abs(score_a - score_b))),
                      'torch_min_adjacent_margin': float(np.min(score_a[oa][:-1] - score_a[oa][1:])),
                      'ort_min_adjacent_margin': float(np.min(score_b[ob][:-1] - score_b[ob][1:]))})
    result = {'status': 'direct-ORT-CPU-numerical-comparison', 'smoke': args.smoke,
              'query_status': manifest['status'], 'source_commit': git(args.source, 'rev-parse', 'HEAD'),
              'instrument_sha256': sha256(script), 'committed_instrument_sha256': hashlib.sha256(committed).hexdigest(),
              'manifest_sha256': hashlib.sha256(encoded.encode()).hexdigest(), 'seed': 61003,
              'versions': versions, 'model_revision': REVISION, 'shape': list(a.shape),
              'max_absolute_difference': float(np.max(np.abs(a - b))), 'minimum_cosine': float(np.min(cosines)),
              'per_row_cosine': cosines.tolist(), 'per_row_max_absolute_difference': np.max(np.abs(a-b), axis=1).tolist(),
              'query_rankings': ranks, 'identical_full_rankings': sum(bool(r['exact_equal']) for r in ranks),
              'identical_top8_rankings': sum(bool(r['top8_equal']) for r in ranks),
              'bank_float32_le_sha256': hashlib.sha256(bank.astype('<f4', copy=False).tobytes()).hexdigest(),
              'input_identities': identities, 'torch_default_explicit_position_delta': unchanged_delta,
              'provider': session.get_providers(), 'torch_parameter_dtypes': dtypes,
              'torch_threads': 4, 'ort_intra_threads': 4, 'ort_inter_threads': 1, 'max_seq_length': sequence_cap,
              'graph_input_names': names, 'graph_output_names': outputs,
              'model_files': [{'file': p.relative_to(args.model).as_posix(), 'bytes': p.stat().st_size, 'sha256': sha256(p)}
                              for p in sorted(args.model.rglob('*')) if p.is_file()],
              'elapsed_seconds': {'torch_load_encode': torch_end-started, 'ort_load_encode': ort_end-torch_end},
              'limitations': ['stock pinned ST ONNX wrapper omits required position_ids; this uses direct ORT',
                              'exporter hidden-state warning: max difference 6.67572021484375e-5 exceeded 1e-5',
                              'proposed query corpus is not the historical 25-case corpus',
                              'stored-vector ranking in memory is not daemon search parity', 'ONNX prerequisite remains deferred']}
    args.output.write_text(json.dumps(result, indent=2, default=lambda v: bool(v) if isinstance(v, np.bool_) else str(v)) + '\n', encoding='utf-8')
    print('comparison-complete', result['shape'], result['max_absolute_difference'], result['minimum_cosine'],
          'full-order', result['identical_full_rankings'], 'top8', result['identical_top8_rankings'], flush=True)


if __name__ == '__main__':
    main()
