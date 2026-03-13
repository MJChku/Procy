import numpy as np
import hnswlib

def build_index(base_vectors):
    p = hnswlib.Index(space='l2', dim=base_vectors.shape[1])
    p.init_index(max_elements=len(base_vectors), ef_construction=350, M=64)
    p.add_items(base_vectors)
    return p

def search_index(index, queries, k):
    index.set_ef(350)
    labels, distances = index.knn_query(queries, k=k)
    return labels

# Example usage:
# base_vectors = np.random.rand(10000, 128)  # Replace with actual data
# queries = np.random.rand(100, 128)         # Replace with actual data
# index = build_index(base_vectors)
# results = search_index(index, queries, 10)
# print(results)