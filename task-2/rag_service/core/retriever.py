import numpy as np
import torch
from typing import List
import logging
import triton
import triton.language as tl
import numpy as np
import time
import json
from test import testdata_kmeans, testdata_knn, testdata_ann
import csv
import os
import math

if not torch.cuda.is_available():
    print("CUDA not available, exiting.")
    exit()
device = torch.device("cuda:0")
print(f"Using device: {device}")

@triton.jit
def dot_kernel_pairwise(
    X_ptr, A_ptr, Out_ptr,
    Q, N, D,
    stride_xq, stride_xd,
    stride_an, stride_ad,
    stride_outq, stride_outn,
    BLOCK_SIZE_D: tl.constexpr,
    ):
        """Calculates pairwise dot product: dot(X[q], A[n])"""
        pid_q = tl.program_id(axis=0)
        pid_n = tl.program_id(axis=1)

        dot_prod = tl.zeros((), dtype=tl.float64)
        for d_start in range(0, D, BLOCK_SIZE_D):
            d_end = tl.minimum(d_start + BLOCK_SIZE_D, D)
            offs_d = d_start + tl.arange(0, BLOCK_SIZE_D)
            mask_d = offs_d < d_end

            x_ptrs = X_ptr + pid_q * stride_xq + offs_d * stride_xd
            x_vals = tl.load(x_ptrs, mask=mask_d, other=0.0)

            a_ptrs = A_ptr + pid_n * stride_an + offs_d * stride_ad
            a_vals = tl.load(a_ptrs, mask=mask_d, other=0.0)

            dot_prod += tl.sum(x_vals * a_vals, axis=0)

        out_offset = pid_q * stride_outq + pid_n * stride_outn
        tl.store(Out_ptr + out_offset, dot_prod)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class SimpleRetriever:
    """
    A basic retriever using dense embeddings and dot-product similarity
    """
    def __init__(self, doc_embeddings: np.ndarray, documents: List[str]):
        """
        Initializes the retriever with document embeddings and text.

        Args:
            doc_embeddings: A NumPy array of shape (num_docs, embedding_dim)
                            containing the precomputed document embeddings.
            documents: A list of strings, where each string is the text
                       content of a document, corresponding to the embeddings.
        """

        self.doc_embeddings = doc_embeddings
        self.documents = documents
        logging.info(f"Initialized SimpleRetriever with {len(self.documents)} documents.")

    def retrieve(self, query_emb: np.ndarray, k: int = 2) -> List[str]:
        """
        Retrieve top-k documents for a single query embedding using the
        specified dot-product logic.

        Args:
            query_emb: A NumPy array representing the query embedding
                             (shape: embedding_dim or potentially 1, embedding_dim).
            k: The number of top documents to retrieve.

        Returns:
            A list containing the text of the top-k documents. Returns empty list on error.
        """
        try:
            query_emb_flat = query_emb.flatten()
            sims = self.doc_embeddings @ query_emb_flat
            sims = sims.ravel()
            top_k_indices = np.argsort(sims)[::-1][:k]

            retrieved_docs = [self.documents[i] for i in top_k_indices]
            return retrieved_docs

        except Exception as e:
            logging.error(f"Unexpected error during retrieve method: {e}", exc_info=True)
            return []


    def batch_retrieve(self, query_embeddings: np.ndarray, ks: List[int]) -> List[List[str]]:
        """
        Retrieve top-k documents for a batch of query embeddings by calling
        the single 'retrieve' method iteratively.

        Args:
            query_embeddings: A NumPy array of query embeddings
                              (shape: batch_size, embedding_dim).
            ks: A list of integers, where each element is the 'k' value
                for the corresponding query in the batch.

        Returns:
            A list of lists, where each inner list contains the text of the
            top-k documents for the corresponding query.
        """
        if query_embeddings.shape[0] != len(ks):
            logging.error(f"Batch retrieve size mismatch: {query_embeddings.shape[0]} embeddings vs {len(ks)} k values.")
            return [[] for _ in range(len(ks))]

        batch_results = []
        for i, query_emb in enumerate(query_embeddings):
            k = ks[i]
            top_docs = self.retrieve(query_emb, k)
            batch_results.append(top_docs)

        return batch_results

class TritonKnnRetriever:
    """
    A basic retriever using dense embeddings and dot-product similarity
    """
    def __init__(self, doc_embeddings: np.ndarray, documents: List[str]):
        """
        Initializes the retriever with document embeddings and text.

        Args:
            doc_embeddings: A NumPy array of shape (num_docs, embedding_dim)
                            containing the precomputed document embeddings.
            documents: A list of strings, where each string is the text
                       content of a document, corresponding to the embeddings.
        """

        self.doc_embeddings = doc_embeddings
        self.documents = documents
        logging.info(f"Initialized SimpleRetriever with {len(self.documents)} documents.")
        self.DEFAULT_BLOCK_Q = 32
        self.DEFAULT_BLOCK_N = 64
        self.DEFAULT_BLOCK_D = 512
        self.DEFAULT_BLOCK_K = 16
        if self.device.type == 'cuda':
            logging.info("Warming up Triton kernel...")
            try:
                # Create small dummy tensors on the GPU
                warmup_Q = 10
                warmup_N = min(16, self.N_A) # Use a small N, but not more than available
                warmup_D = self.D

                # Use float32, consistent with _prepare_tensors
                warmup_X = torch.randn(warmup_Q, warmup_D, dtype=torch.float32, device=self.device)
                # Use a slice of actual embeddings or create dummy A
                # Using dummy A avoids issues if N_A is very small
                warmup_A = torch.randn(warmup_N, warmup_D, dtype=torch.float32, device=self.device)
                # Alternatively use: warmup_A = self.doc_embeddings_torch[:warmup_N]

                # Call the method that uses the kernel
                # We need warmup_A_prep, warmup_X_prep if distance_dot expects prepared tensors
                # Let's assume distance_dot calls _prepare_tensors internally
                _ = self.distance_dot(warmup_X, warmup_A) # Discard result

                # --- Crucial: Synchronize GPU ---
                torch.cuda.synchronize()
                logging.info("Triton kernel warm-up complete.")

            except Exception as e:
                # Log warning but don't necessarily crash initialization
                logging.warning(f"Triton kernel warm-up failed: {e}. First call might be slow.")
        else:
             logging.info("Skipping Triton kernel warm-up on CPU.")
    
    def _prepare_tensors(self, *tensors, target_device =device):
        """Ensure tensors are float32, contiguous, and on the correct device."""
        prepared = []
        for t in tensors:
            if not isinstance(t, torch.Tensor):
                t = torch.tensor(t, dtype=torch.float32, device=target_device)
            if t.device != target_device:
                t = t.to(target_device)
            if t.dtype != torch.float32:
                t = t.to(dtype=torch.float32)
            prepared.append(t.contiguous())
        return prepared
    def distance_dot(self, X, A):
        """Computes pairwise dot product using Triton kernel."""
        X_prep, A_prep = self._prepare_tensors(X, A)
        Q, D = X_prep.shape
        N, D_A = A_prep.shape
        assert D == D_A, f"Dimension mismatch: X({D}) vs A({D_A})"

        Out = torch.empty((Q, N), dtype=torch.float64, device=device)
        grid = (Q, N)
        self.dot_kernel_pairwise[grid](
        X_prep, A_prep, Out,
        Q, N, D,
        X_prep.stride(0), X_prep.stride(1),
        A_prep.stride(0), A_prep.stride(1),
        Out.stride(0), Out.stride(1),
        BLOCK_SIZE_D=self.DEFAULT_BLOCK_D
        )
        return Out
    def retrieve(self, N_A, D, X, K):
        """
         Finds the K nearest neighbors in A for each query vector in X using
         brute-force pairwise L2 distance calculation.

         Args:
         N_A (int): Number of database points (should match A.shape[0]).
         D (int): Dimensionality (should match A.shape[1] and X.shape[1]).
         A (torch.Tensor): Database vectors (N_A, D) on GPU.
         X (torch.Tensor): Query vectors (Q, D) on GPU.
         K (int): Number of neighbors to find.

         Returns:
         tuple[torch.Tensor, torch.Tensor]:
            - topk_indices (torch.Tensor): Indices of the K nearest neighbors (Q, K).
            - topk_distances (torch.Tensor): Squared L2 distances of the K nearest neighbors (Q, K).
        """
        input_was_1d = False
        if X.ndim == 1:
        # Input is a single vector (D,)
            input_was_1d = True
            X = X.unsqueeze(0)
        A = self.doc_embeddings
        A_prep, X_prep = self._prepare_tensors(A, X)
        Q = X_prep.shape[0]
        assert A_prep.shape[0] == N_A, "N_A doesn't match A.shape[0]"
        assert A_prep.shape[1] == D, "D doesn't match A.shape[1]"
        assert X_prep.shape[1] == D, "D doesn't match X.shape[1]"
        assert K > 0, "K must be positive"
        assert K <= N_A, "K cannot be larger than the number of database points"


        print(f"Running k-NN: Q={Q}, N={N_A}, D={D}, K={K}")
        start_time = time.time()

    # 1. Calculate all pairwise squared L2 distances
    #    distance_l2 returns squared L2 distances
        all_distances = self.distance_dot(X_prep, A_prep) # Shape (Q, N_A)

    # 2. Find the top K smallest distances for each query
    #    largest=False gives smallest distances (nearest neighbors)
        topk_distances, topk_indices = torch.topk(all_distances, k=K, dim=1, largest=False)
        indices_np = topk_indices.cpu().numpy()

        end_time = time.time()
        if isinstance(self.documents, np.ndarray):
            retrieved_docs = self.documents[indices_np]
        else:
            retrieved_docs = [
            [self.documents[int(idx)] for idx in row]
            for row in indices_np
            ]

        print(f"k-NN computation time: {end_time - start_time:.6f} seconds")
        if input_was_1d:
            if isinstance(retrieved_docs, np.ndarray):
                return retrieved_docs.squeeze(0)
            elif isinstance(retrieved_docs, list) and len(retrieved_docs) == 1:
                 return retrieved_docs[0]
            else:
                return retrieved_docs
        else: 
            return retrieved_docs


    