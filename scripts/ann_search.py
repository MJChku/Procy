"""ANN Search Implementation — to be improved by evolve iterations.

This is the baseline (naive) version. Each evolve iteration should
modify this file to improve recall@10 while keeping search fast.
"""
import numpy as np
import hnswlib

# Baseline: low-quality HNSW with poor parameters
def build_index(base_vectors):
    dim = base_vectors.shape[1]
    n = base_vectors.shape[0]
    index = hnswlib.Index(space='l2', dim=dim)
    # Deliberately bad parameters — low M, low ef_construction
    index.init_index(max_elements=n, ef_construction=20, M=4)
    index.add_items(base_vectors, np.arange(n))
    index.set_ef(10)  # low search ef
    return index

def search_index(index, queries, k):
    labels, distances = index.knn_query(queries, k=k)
    return labels
