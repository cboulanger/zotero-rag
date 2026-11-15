# Evaluation reports

Dated reports of evaluation runs, named `YYYY-MM-DD-<topic>.md`.

| Report | What it measures |
|---|---|
| [2026-11-14-reranking-offline.md](2026-11-14-reranking-offline.md) | Cross-encoder re-ranking, retrieval only: 178 questions and 26 gold facts on two gold indexes, bge-reranker-v2-m3 (GPU) vs mMiniLM-L12 (CPU), timings |
| [2026-11-14-reranking-judged-on.md](2026-11-14-reranking-judged-on.md) | Judged end-to-end run of the `runpod` preset with re-ranking |
| [2026-11-14-reranking-judged-off.md](2026-11-14-reranking-judged-off.md) | The same run without re-ranking (`--no-rerank`) |
