# Re-ranking: offline comparison on the gold library (2026-11-14)

Retrieval-only comparison for the re-ranking step (PR #107). The dense baseline is the plain
`multilingual-e5-large-instruct` vector order (no BM25 blend); each re-ranked variant `rrN` rescores
the top N candidates with a cross-encoder and keeps the rest in vector order.

- **Indexes:** `d800` is the Docling snapshot (4527 chunks); `k800` is the Kreuzberg snapshot of the
  extractor comparison (4497 chunks), both from the Hugging Face store `cmboulanger/zotero-rag-gold`.
- **Questions:** 178 retrieval questions on the gold library (P@1, P@10 and MRR over the first 200
  ranks), plus the 26 gold facts of the 16-question gold standard (rank of the first chunk matching
  the fact's source pattern).
- **Query embeddings:** computed locally with the same e5 model (cosine >= 0.9998 to KISSKI's).
- **Models:** `BAAI/bge-reranker-v2-m3` on a RunPod serverless endpoint (RTX A5000, vLLM), and
  `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1` on 4 CPU cores.

## Timing

| Model | Where | Time |
|---|---|---|
| mMiniLM-L12 | 4 vCPU | about 3.2 s per 100 pairs |
| bge-reranker-v2-m3 | 4 vCPU, fp32 | about 37 s per 100 pairs (too slow for the 15 s budget beyond a pool of about 30) |
| bge-reranker-v2-m3 | RunPod A5000, warm | about 1.5 s per 200 pairs |
| Qwen3-Reranker-0.6B | 4 vCPU | over 25 minutes for 3 questions (ruled out) |

int8 dynamic quantization of bge on the CPU failed (`KeyError: 'ne'`).

## Results

### d800_bge-runpod  (BAAI/bge-reranker-v2-m3, pool 200, 178 questions, median 1.45s)

| Variant | P@1 | P@10 | MRR | Gold facts in top 10 | Gold facts in top 30 | P@10 vs dense (won/lost, McNemar p) |
|---|---|---|---|---|---|---|
| dense | 0.388 | 0.742 | 0.515 | 12/26 | 18/26 | +0 -0 p=1.000 |
| rr20 | 0.646 | 0.826 | 0.717 | 13/26 | 18/26 | +15 -0 p=0.000 |
| rr30 | 0.646 | 0.843 | 0.721 | 14/26 | 18/26 | +18 -0 p=0.000 |
| rr50 | 0.663 | 0.888 | 0.745 | 12/26 | 18/26 | +26 -0 p=0.000 |
| rr100 | 0.691 | 0.927 | 0.776 | 12/26 | 18/26 | +33 -0 p=0.000 |
| rr200 | 0.708 | 0.949 | 0.795 | 13/26 | 19/26 | +37 -0 p=0.000 |

### d800_mminilm  (cross-encoder/mmarco-mMiniLMv2-L12-H384-v1, pool 100, 178 questions, median 3.20s)

| Variant | P@1 | P@10 | MRR | Gold facts in top 10 | Gold facts in top 30 | P@10 vs dense (won/lost, McNemar p) |
|---|---|---|---|---|---|---|
| dense | 0.388 | 0.742 | 0.515 | 12/26 | 18/26 | +0 -0 p=1.000 |
| rr20 | 0.618 | 0.803 | 0.687 | 12/26 | 18/26 | +12 -1 p=0.003 |
| rr30 | 0.629 | 0.815 | 0.696 | 11/26 | 18/26 | +14 -1 p=0.001 |
| rr50 | 0.640 | 0.843 | 0.710 | 8/26 | 18/26 | +19 -1 p=0.000 |
| rr100 | 0.663 | 0.888 | 0.739 | 7/26 | 18/26 | +27 -1 p=0.000 |

### k800_bge-runpod  (BAAI/bge-reranker-v2-m3, pool 200, 178 questions, median 1.45s)

| Variant | P@1 | P@10 | MRR | Gold facts in top 10 | Gold facts in top 30 | P@10 vs dense (won/lost, McNemar p) |
|---|---|---|---|---|---|---|
| dense | 0.444 | 0.758 | 0.547 | 14/26 | 19/26 | +0 -0 p=1.000 |
| rr20 | 0.669 | 0.820 | 0.738 | 13/26 | 19/26 | +11 -0 p=0.001 |
| rr30 | 0.680 | 0.843 | 0.751 | 15/26 | 19/26 | +15 -0 p=0.000 |
| rr50 | 0.691 | 0.876 | 0.768 | 15/26 | 20/26 | +21 -0 p=0.000 |
| rr100 | 0.708 | 0.910 | 0.791 | 15/26 | 17/26 | +27 -0 p=0.000 |
| rr200 | 0.719 | 0.933 | 0.805 | 15/26 | 17/26 | +31 -0 p=0.000 |

### k800_mminilm  (cross-encoder/mmarco-mMiniLMv2-L12-H384-v1, pool 100, 178 questions, median 3.06s)

| Variant | P@1 | P@10 | MRR | Gold facts in top 10 | Gold facts in top 30 | P@10 vs dense (won/lost, McNemar p) |
|---|---|---|---|---|---|---|
| dense | 0.444 | 0.758 | 0.547 | 14/26 | 19/26 | +0 -0 p=1.000 |
| rr20 | 0.640 | 0.815 | 0.710 | 12/26 | 19/26 | +11 -1 p=0.006 |
| rr30 | 0.652 | 0.831 | 0.721 | 13/26 | 19/26 | +14 -1 p=0.001 |
| rr50 | 0.652 | 0.854 | 0.726 | 12/26 | 16/26 | +18 -1 p=0.000 |
| rr100 | 0.663 | 0.882 | 0.745 | 9/26 | 15/26 | +24 -2 p=0.000 |

## By language (bge-reranker-v2-m3, P@10)

| Index | Language | Questions | Dense | rr50 | rr100 |
|---|---|---|---|---|---|
| d800 | de | 27 | 0.70 | 0.81 | 0.81 |
| d800 | en | 130 | 0.75 | 0.91 | 0.95 |
| d800 | es | 14 | 0.57 | 0.79 | 0.86 |
| d800 | fr | 7 | 1.00 | 1.00 | 1.00 |
| k800 | de | 27 | 0.67 | 0.81 | 0.85 |
| k800 | en | 130 | 0.78 | 0.90 | 0.93 |
| k800 | es | 14 | 0.71 | 0.79 | 0.79 |
| k800 | fr | 7 | 0.86 | 0.86 | 1.00 |

## Conclusions

- bge-reranker-v2-m3 is a clear win on both indexes: P@10 rises from about 0.75 to 0.91-0.93 at a
  pool of 100, P@1 from about 0.4 to 0.7, and no question gets worse. Gold facts in the top 10 stay
  the same or improve. It needs a GPU, so the bundled `runpod` and `remote-mpcdf` presets use it with
  a pool of 100.
- mMiniLM-L12 on the CPU also improves P@10, but pushes gold facts out of the top 10 at larger pools
  (d800: 12 to 7 at a pool of 100). The bundled local presets use it with a pool of 30.
- The judged end-to-end runs of the same day are in `2026-11-14-reranking-judged-on.md` and
  `2026-11-14-reranking-judged-off.md` (runpod preset, Qwen2.5-7B, re-ranking on and off): facts
  missing from the context fell from 15 to 8, but judged recall stayed at 52% because the 7B model
  lost more facts in generation (4 to 9).
