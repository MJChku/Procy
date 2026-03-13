#!/usr/bin/env python3
"""ANN Search Benchmark — evaluator script for procy evolve.

Measures recall@10 for HNSW index on synthetic data.
The search implementation is in ann_search.py (same directory).
This script imports and evaluates it.

Usage:
    python3 eval_ann.py
"""
import sys
import time
import json
import numpy as np

# Default parameters (can be overridden by ann_search.py)
DIM = 128
N_BASE = 50000
N_QUERY = 1000
K = 10

def ground_truth_knn(base, queries, k):
    """Brute-force exact KNN for ground truth."""
    gt = np.zeros((len(queries), k), dtype=np.int64)
    for i, q in enumerate(queries):
        dists = np.sum((base - q) ** 2, axis=1)
        gt[i] = np.argpartition(dists, k)[:k]
    return gt

def recall_at_k(gt, results, k):
    """Compute recall@k."""
    assert gt.shape[0] == results.shape[0]
    hits = 0
    total = gt.shape[0] * k
    for i in range(gt.shape[0]):
        hits += len(set(gt[i].tolist()) & set(results[i].tolist()))
    return hits / total

def main():
    np.random.seed(42)
    base = np.random.randn(N_BASE, DIM).astype(np.float32)
    queries = np.random.randn(N_QUERY, DIM).astype(np.float32)

    # Compute ground truth
    gt = ground_truth_knn(base, queries, K)

    # Import the search implementation
    try:
        sys.path.insert(0, ".")
        from ann_search import build_index, search_index
    except ImportError as e:
        print(json.dumps({"error": f"Cannot import ann_search: {e}", "recall_at_10": 0.0}))
        sys.exit(1)

    # Build
    t0 = time.time()
    index = build_index(base)
    build_time = time.time() - t0

    # Search
    t0 = time.time()
    results = search_index(index, queries, K)
    search_time = time.time() - t0

    # Evaluate
    recall = recall_at_k(gt, results, K)
    qps = N_QUERY / search_time if search_time > 0 else 0

    result = {
        "recall_at_10": round(recall, 4),
        "build_time_s": round(build_time, 4),
        "search_time_s": round(search_time, 4),
        "qps": round(qps, 1),
        "n_base": N_BASE,
        "n_query": N_QUERY,
        "dim": DIM,
    }
    print(json.dumps(result))

if __name__ == "__main__":
    main()
