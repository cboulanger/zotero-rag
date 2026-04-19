"""
Compare embedding quality and speed between two presets.

Usage:
    # Built-in hand-crafted pairs (quick smoke test):
    uv run python scripts/eval_embeddings.py --preset-a remote-kisski --preset-b cloud-server-kisski

    # Custom JSONL pairs  {"query": "...", "passage": "..."}:
    uv run python scripts/eval_embeddings.py --preset-a remote-kisski --preset-b cloud-server-kisski --data pairs.jsonl

    # Published IR benchmark (downloads dataset automatically):
    uv run python scripts/eval_embeddings.py --preset-a remote-kisski --preset-b cloud-server-kisski --mteb-task scifact

Supported --mteb-task values:
    scifact    Scientific claim verification, English (~5 K corpus, ~300 queries)
    nfcorpus   Biomedical retrieval, English (~3.6 K corpus, ~323 queries)
    fiqa       Financial QA, English (~57 K corpus, ~648 queries)

Metrics (IR mode): nDCG@10 (primary MTEB metric), MRR@10, Recall@{1,5,10}
Metrics (pair mode): MRR@10, Recall@{1,5,10}

IR mode requires: uv sync --extra eval
"""

import argparse
import asyncio
import json
import random
import sys
import time
from math import log2
from pathlib import Path
from typing import Optional

import numpy as np

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(_PROJECT_ROOT / ".env.local", override=True)
    load_dotenv(_PROJECT_ROOT / ".env", override=True)
except ImportError:
    pass

from backend.config.presets import PRESETS, get_preset
from backend.services.embeddings import create_embedding_service

# ---------------------------------------------------------------------------
# Supported IR tasks
# ---------------------------------------------------------------------------

SUPPORTED_IR_TASKS: dict[str, dict] = {
    "scifact": {
        "ir_name": "beir/scifact/test",
        "desc": "Scientific fact-checking, English (~5 K corpus, ~300 queries)",
        "default_max_corpus": 500,
    },
    "nfcorpus": {
        "ir_name": "beir/nfcorpus/test",
        "desc": "Biomedical retrieval, English (~3.6 K corpus, ~323 queries)",
        "default_max_corpus": 500,
    },
    "fiqa": {
        "ir_name": "beir/fiqa/test",
        "desc": "Financial QA, English (~57 K corpus, ~648 queries)",
        "default_max_corpus": 500,
    },
}

# ---------------------------------------------------------------------------
# Built-in pairs (quick mode, no download needed)
# ---------------------------------------------------------------------------

BUILTIN_PAIRS: list[dict[str, str]] = [
    {
        "query": "How do transformer models handle long-range dependencies in text?",
        "passage": "Self-attention mechanisms in transformer architectures allow each token to "
                   "directly attend to every other token in the sequence, enabling effective "
                   "modelling of long-range dependencies without the vanishing gradient problem "
                   "that affects recurrent networks.",
    },
    {
        "query": "What is retrieval-augmented generation?",
        "passage": "RAG combines a dense retrieval step with a generative language model. "
                   "Given a query, relevant passages are fetched from a vector store and "
                   "prepended to the prompt, grounding the model's answer in factual documents.",
    },
    {
        "query": "Which factors influence citation counts of academic papers?",
        "passage": "Empirical studies show that citation impact is correlated with journal "
                   "prestige, author h-index, open-access availability, and whether the paper "
                   "introduces a new dataset or benchmark.",
    },
    {
        "query": "How is text chunked before embedding for semantic search?",
        "passage": "Documents are split into overlapping windows of fixed character or token "
                   "length. Overlap between consecutive chunks ensures that sentences spanning "
                   "a boundary are represented in at least one chunk, preserving semantic context.",
    },
    {
        "query": "What distinguishes vector databases from traditional relational databases?",
        "passage": "Vector databases index high-dimensional embedding vectors using approximate "
                   "nearest-neighbour structures such as HNSW graphs, enabling sub-linear "
                   "similarity search that relational B-tree indices cannot support.",
    },
    {
        "query": "What is the role of temperature in language model sampling?",
        "passage": "Temperature scales the logits before the softmax step. A low temperature "
                   "makes the distribution peakier, producing more deterministic output; a high "
                   "temperature flattens it, increasing diversity and creativity at the cost of "
                   "coherence.",
    },
    {
        "query": "How does quantization reduce memory usage of neural networks?",
        "passage": "Post-training quantization maps 32-bit float weights to lower-precision "
                   "representations (8-bit integers or 4-bit formats), shrinking model size "
                   "two-to-eight-fold while incurring only a small accuracy penalty on most tasks.",
    },
    {
        "query": "What is cosine similarity and when is it used?",
        "passage": "Cosine similarity measures the angle between two vectors, returning 1 for "
                   "identical direction and 0 for orthogonality. It is the standard metric for "
                   "comparing sentence embeddings because it is invariant to vector magnitude.",
    },
    {
        "query": "How did the printing press change the spread of information in Europe?",
        "passage": "Gutenberg's movable-type press enabled mass reproduction of texts, reducing "
                   "book prices by orders of magnitude and accelerating the diffusion of "
                   "Renaissance humanism, Reformation theology, and scientific knowledge across "
                   "European scholarly networks.",
    },
    {
        "query": "What are the main criticisms of bibliometric evaluation in academia?",
        "passage": "Critics argue that journal impact factors and h-indices incentivise quantity "
                   "over quality, disadvantage researchers in non-English fields, and create "
                   "perverse incentives such as salami-slicing publications and citation rings.",
    },
    {
        "query": "Wie funktionieren neuronale Netze beim Erkennen von Bildern?",
        "passage": "Faltungsneuronale Netze (CNNs) lernen hierarchische Merkmale: frühe Schichten "
                   "erkennen Kanten und Texturen, mittlere Schichten Formen und Teile, und "
                   "tiefere Schichten semantische Konzepte wie Gesichter oder Fahrzeuge.",
    },
    {
        "query": "Was versteht man unter semantischer Suche?",
        "passage": "Semantische Suche nutzt Einbettungsvektoren, um die inhaltliche Bedeutung "
                   "von Anfragen und Dokumenten zu repräsentieren und Treffer anhand von "
                   "Vektorähnlichkeit statt exakter Schlüsselwortübereinstimmung zu finden.",
    },
    {
        "query": "Welche Vorteile bieten lokale Sprachmodelle gegenüber Cloud-Diensten?",
        "passage": "Lokal betriebene Modelle senden keine Daten an externe Server, was "
                   "Datenschutzanforderungen erfüllt und Latenzen durch Netzwerkübertragung "
                   "vermeidet. Sie funktionieren zudem ohne Internetverbindung.",
    },
    {
        "query": "Wie wird Plagiat in wissenschaftlichen Texten automatisch erkannt?",
        "passage": "Plagiatssoftware vergleicht Einreichungen mit großen Textkorpora durch "
                   "n-Gramm-Fingerprinting und semantische Ähnlichkeitsmaße. Neuere Systeme "
                   "erkennen auch paraphrasierte Übernahmen mithilfe von Sprachmodell-Embeddings.",
    },
    {
        "query": "Welchen Einfluss hatte die Aufklärung auf die europäische Wissenschaft?",
        "passage": "Die Aufklärung förderte empirische Methoden, kritisches Denken und die "
                   "Institutionalisierung von Wissen in Akademien und Universitäten. Sie legte "
                   "damit den Grundstein für die moderne Naturwissenschaft und Philosophie.",
    },
    {
        "query": "Wie veraenderte das Internet die wissenschaftliche Kommunikation?",
        "passage": "Digitale Plattformen ermöglichten Preprint-Server, Open-Access-Journale und "
                   "kollaborative Werkzeuge, die den Publikationszyklus beschleunigten und "
                   "geografische Barrieren im globalen wissenschaftlichen Austausch abbauten.",
    },
    {
        "query": "What role do peer reviews play in scientific quality assurance?",
        "passage": "Das Peer-Review-Verfahren lässt eingereichte Manuskripte von unabhängigen "
                   "Fachleuten beurteilen, bevor sie publiziert werden. Es dient der "
                   "Qualitätssicherung, kann jedoch langsam sein und Bestätigungsverzerrungen "
                   "begünstigen.",
    },
    {
        "query": "Welche Herausforderungen stellt maschinelles Lernen an Datenschutz?",
        "passage": "Machine learning models trained on personal data can memorise and leak "
                   "sensitive records. Differential privacy and federated learning are two "
                   "techniques designed to mitigate this risk without sacrificing model utility.",
    },
    {
        "query": "How is precision different from recall in information retrieval?",
        "passage": "Precision measures the fraction of retrieved documents that are relevant, "
                   "while recall measures the fraction of all relevant documents that were "
                   "retrieved. Improving one often degrades the other, making F1 a useful "
                   "harmonic balance.",
    },
    {
        "query": "What is mean reciprocal rank and how is it calculated?",
        "passage": "MRR averages the reciprocal of the rank at which the first relevant result "
                   "appears across a set of queries. If the relevant item is at rank 3, it "
                   "contributes 1/3 to the MRR. Higher MRR indicates better ranking quality.",
    },
    {
        "query": "What does it mean for an embedding model to be multilingual?",
        "passage": "A multilingual embedding model maps semantically equivalent sentences in "
                   "different languages close together in vector space. This enables cross-lingual "
                   "retrieval where a query in one language matches relevant passages in another.",
    },
    {
        "query": "How does batch size affect embedding throughput on CPU?",
        "passage": "Larger batches amortise Python and matrix-setup overhead, improving CPU "
                   "utilisation. However, very large batches can exceed cache size, causing "
                   "memory bandwidth bottlenecks. Optimal batch size on CPU is typically 16–64.",
    },
]

# ---------------------------------------------------------------------------
# Dataset loading (IR mode)
# ---------------------------------------------------------------------------

# corpus  : {doc_id: text}
# queries : {query_id: text}
# qrels   : {query_id: {doc_id: relevance_score}}
IRDataset = tuple[dict[str, str], dict[str, str], dict[str, dict[str, int]]]


# Conservative character limit that fits within 512 tokens for any language.
# multilingual-e5-large-instruct hard limit is 512 tokens; scientific English
# tokenizes at ~3 chars/token, so 1400 chars ≈ 467 tokens with margin.
_MAX_TEXT_CHARS = 1400


def _truncate(text: str) -> str:
    return text[:_MAX_TEXT_CHARS] if len(text) > _MAX_TEXT_CHARS else text


def load_beir(task_name: str, max_queries: int, max_corpus: Optional[int]) -> IRDataset:
    try:
        import ir_datasets
    except ImportError:
        raise RuntimeError(
            "ir-datasets is required for IR benchmark tasks.\n"
            "Install it with: uv sync --extra eval"
        )

    ir_name = SUPPORTED_IR_TASKS[task_name]["ir_name"]
    print(f"  Loading {ir_name} via ir-datasets...", flush=True)
    dataset = ir_datasets.load(ir_name)

    print("  Reading corpus...", flush=True)
    all_corpus: dict[str, str] = {}
    for doc in dataset.docs_iter():
        title = (getattr(doc, "title", None) or "").strip()
        text = (getattr(doc, "text", None) or "").strip()
        all_corpus[doc.doc_id] = _truncate(f"{title} {text}".strip() if title else text)

    print("  Reading queries...", flush=True)
    all_queries: dict[str, str] = {q.query_id: _truncate(q.text) for q in dataset.queries_iter()}

    print("  Reading qrels...", flush=True)
    all_qrels: dict[str, dict[str, int]] = {}
    for qrel in dataset.qrels_iter():
        if qrel.relevance > 0:
            all_qrels.setdefault(qrel.query_id, {})[qrel.doc_id] = qrel.relevance

    # Sample queries that have relevant docs
    eligible = [qid for qid in all_qrels if qid in all_queries]
    random.shuffle(eligible)
    selected_qids = set(eligible[:max_queries])

    queries: dict[str, str] = {qid: all_queries[qid] for qid in selected_qids}
    qrels: dict[str, dict[str, int]] = {qid: all_qrels[qid] for qid in selected_qids}

    # Always include relevant docs; pad with random corpus docs up to max_corpus
    relevant_ids: set[str] = {did for rel in qrels.values() for did in rel}
    other_ids = [did for did in all_corpus if did not in relevant_ids]
    random.shuffle(other_ids)
    if max_corpus is not None:
        other_ids = other_ids[:max(0, max_corpus - len(relevant_ids))]

    corpus: dict[str, str] = {did: all_corpus[did] for did in relevant_ids}
    corpus.update({did: all_corpus[did] for did in other_ids})

    return corpus, queries, qrels


def load_ir_dataset(
    task_name: str,
    max_queries: int,
    max_corpus: Optional[int],
) -> IRDataset:
    task = SUPPORTED_IR_TASKS[task_name]
    effective_max_corpus = max_corpus if max_corpus is not None else task["default_max_corpus"]
    corpus, queries, qrels = load_beir(task_name, max_queries, effective_max_corpus)

    # Drop queries with no relevant docs in corpus
    qrels = {
        qid: {did: s for did, s in rel.items() if did in corpus}
        for qid, rel in qrels.items()
    }
    qrels = {qid: rel for qid, rel in qrels.items() if rel and qid in queries}

    print(
        f"  Dataset ready: {len(corpus)} corpus docs, "
        f"{len(queries)} queries, {sum(len(r) for r in qrels.values())} relevant pairs."
    )
    return corpus, queries, qrels


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def ndcg_at_k(ranked_ids: list[str], relevant: dict[str, int], k: int = 10) -> float:
    dcg = sum(
        relevant.get(ranked_ids[i], 0) / log2(i + 2)
        for i in range(min(k, len(ranked_ids)))
    )
    ideal = sorted(relevant.values(), reverse=True)[:k]
    idcg = sum(rel / log2(i + 2) for i, rel in enumerate(ideal))
    return dcg / idcg if idcg > 0 else 0.0


def mrr_at_k(ranked_ids: list[str], relevant: dict[str, int], k: int = 10) -> float:
    for i, did in enumerate(ranked_ids[:k]):
        if relevant.get(did, 0) > 0:
            return 1.0 / (i + 1)
    return 0.0


def recall_at_k(ranked_ids: list[str], relevant: dict[str, int], k: int) -> float:
    if not relevant:
        return 0.0
    hits = sum(1 for did in ranked_ids[:k] if relevant.get(did, 0) > 0)
    return hits / len(relevant)


def compute_ir_metrics(
    query_ids: list[str],
    all_ranked: dict[str, list[str]],
    qrels: dict[str, dict[str, int]],
) -> dict:
    ndcgs, mrrs, r1s, r5s, r10s = [], [], [], [], []
    for qid in query_ids:
        ranked = all_ranked[qid]
        rel = qrels.get(qid, {})
        ndcgs.append(ndcg_at_k(ranked, rel, 10))
        mrrs.append(mrr_at_k(ranked, rel, 10))
        r1s.append(recall_at_k(ranked, rel, 1))
        r5s.append(recall_at_k(ranked, rel, 5))
        r10s.append(recall_at_k(ranked, rel, 10))
    n = len(query_ids)
    return {
        "nDCG@10":   sum(ndcgs) / n,
        "MRR@10":    sum(mrrs) / n,
        "Recall@1":  sum(r1s) / n,
        "Recall@5":  sum(r5s) / n,
        "Recall@10": sum(r10s) / n,
    }


def rank_of_target(query_vec: np.ndarray, corpus_vecs: np.ndarray, target_idx: int) -> int:
    norms = np.linalg.norm(corpus_vecs, axis=1) * np.linalg.norm(query_vec) + 1e-10
    sims = corpus_vecs @ query_vec / norms
    order = np.argsort(-sims)
    return int(np.where(order == target_idx)[0][0]) + 1


def compute_pair_metrics(ranks: list[int], ks: tuple[int, ...] = (1, 5, 10)) -> dict:
    n = len(ranks)
    metrics: dict = {"MRR@10": sum(1 / r for r in ranks if r <= 10) / n}
    for k in ks:
        metrics[f"Recall@{k}"] = sum(1 for r in ranks if r <= k) / n
    return metrics


# ---------------------------------------------------------------------------
# Evaluation runners
# ---------------------------------------------------------------------------

async def evaluate_preset_pairs(preset_name: str, pairs: list[dict]) -> dict:
    """Pair mode: tiny corpus, one relevant passage per query."""
    preset = get_preset(preset_name)
    svc = create_embedding_service(preset.embedding)

    passages = [p["passage"] for p in pairs]
    queries = [p["query"] for p in pairs]

    print(f"\n[{preset_name}] Embedding {len(passages)} corpus passages...", flush=True)
    t0 = time.perf_counter()
    passage_vecs = np.array(await svc.embed_batch(passages))
    corpus_time = time.perf_counter() - t0

    print(f"[{preset_name}] Embedding {len(queries)} queries...", flush=True)
    t1 = time.perf_counter()
    query_vecs = np.array(await svc.embed_batch(queries))
    query_time = time.perf_counter() - t1

    total_throughput = (len(passages) + len(queries)) / (corpus_time + query_time)
    ranks = [rank_of_target(query_vecs[i], passage_vecs, i) for i in range(len(pairs))]

    metrics = compute_pair_metrics(ranks)
    metrics["throughput_texts_per_sec"] = round(total_throughput, 1)
    metrics["corpus_time_s"] = round(corpus_time, 2)
    metrics["embedding_dim"] = passage_vecs.shape[1]
    metrics["ranks"] = ranks

    if hasattr(svc, "close"):
        svc.close()
    return metrics


async def evaluate_preset_ir(
    preset_name: str,
    corpus: dict[str, str],
    queries: dict[str, str],
    qrels: dict[str, dict[str, int]],
) -> dict:
    """IR mode: large corpus, multiple relevant docs per query, nDCG@10."""
    preset = get_preset(preset_name)
    svc = create_embedding_service(preset.embedding)

    doc_ids = list(corpus.keys())
    doc_texts = [corpus[did] for did in doc_ids]
    query_ids = list(queries.keys())
    query_texts = [queries[qid] for qid in query_ids]

    print(f"\n[{preset_name}] Embedding {len(doc_texts)} corpus docs...", flush=True)
    t0 = time.perf_counter()
    corpus_vecs = np.array(await svc.embed_batch(doc_texts))
    corpus_time = time.perf_counter() - t0

    print(f"[{preset_name}] Embedding {len(query_texts)} queries...", flush=True)
    t1 = time.perf_counter()
    query_vecs = np.array(await svc.embed_batch(query_texts))
    query_time = time.perf_counter() - t1

    total_throughput = (len(doc_texts) + len(query_texts)) / (corpus_time + query_time)

    # Rank corpus for each query
    corpus_norms = np.linalg.norm(corpus_vecs, axis=1, keepdims=True) + 1e-10
    corpus_normed = corpus_vecs / corpus_norms
    query_norms = np.linalg.norm(query_vecs, axis=1, keepdims=True) + 1e-10
    query_normed = query_vecs / query_norms

    all_ranked: dict[str, list[str]] = {}
    for i, qid in enumerate(query_ids):
        sims = corpus_normed @ query_normed[i]
        order = np.argsort(-sims)
        all_ranked[qid] = [doc_ids[j] for j in order]

    metrics = compute_ir_metrics(query_ids, all_ranked, qrels)
    metrics["throughput_texts_per_sec"] = round(total_throughput, 1)
    metrics["corpus_time_s"] = round(corpus_time, 2)
    metrics["embedding_dim"] = corpus_vecs.shape[1]

    if hasattr(svc, "close"):
        svc.close()
    return metrics


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def fmt(val: float) -> str:
    return f"{val:.3f}"


def print_results(
    preset_a: str,
    preset_b: str,
    metrics_a: dict,
    metrics_b: dict,
    metric_keys: list[str],
    corpus_info: dict,
) -> None:
    col_w = 24
    width = col_w * 3 + 10
    print("\n" + "=" * width)
    print("RESULTS")
    print("=" * width)
    print(f"  Corpus : {corpus_info['name']}")
    if "description" in corpus_info:
        print(f"  Task   : {corpus_info['description']}")
    print(f"  Docs   : {corpus_info['n_corpus']:,}")
    print(f"  Queries: {corpus_info['n_queries']:,}")
    if "n_relevant_pairs" in corpus_info:
        avg = corpus_info["n_relevant_pairs"] / max(corpus_info["n_queries"], 1)
        print(f"  Qrels  : {corpus_info['n_relevant_pairs']:,} pairs  (avg {avg:.1f} relevant/query)")
    if "seed" in corpus_info:
        print(f"  Seed   : {corpus_info['seed']}")
    print("-" * width)
    print(f"{'Metric':<{col_w}} {preset_a:<{col_w}} {preset_b:<{col_w}} {'Delta':>8}")
    print("-" * width)

    for key in metric_keys:
        va = metrics_a[key]
        vb = metrics_b[key]
        delta = vb - va
        sign = "+" if delta >= 0 else ""
        print(f"{key:<{col_w}} {fmt(va):<{col_w}} {fmt(vb):<{col_w}} {sign}{fmt(delta):>8}")

    print("-" * width)
    print(
        f"{'Throughput (txt/s)':<{col_w}} "
        f"{metrics_a['throughput_texts_per_sec']:<{col_w}} "
        f"{metrics_b['throughput_texts_per_sec']:<{col_w}}"
    )
    print(
        f"{'Corpus encode time (s)':<{col_w}} "
        f"{metrics_a['corpus_time_s']:<{col_w}} "
        f"{metrics_b['corpus_time_s']:<{col_w}}"
    )
    print(
        f"{'Embedding dim':<{col_w}} "
        f"{metrics_a['embedding_dim']:<{col_w}} "
        f"{metrics_b['embedding_dim']:<{col_w}}"
    )
    print("=" * width)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def pick_preset(prompt: str) -> str:
    available = sorted(PRESETS.keys())
    print(f"\n{prompt}")
    for i, name in enumerate(available, 1):
        p = PRESETS[name]
        print(f"  {i:2d}. {name:<32s}  [{p.embedding.model_type}] {p.embedding.model_name}")
    while True:
        raw = input("Enter number or preset name: ").strip()
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(available):
                return available[idx]
        elif raw in PRESETS:
            return raw
        print("  Invalid choice, try again.")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Compare embedding quality and speed between two presets.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Supported --mteb-task values: " + ", ".join(SUPPORTED_IR_TASKS),
    )
    p.add_argument("--preset-a", help="First preset name")
    p.add_argument("--preset-b", help="Second preset name")
    p.add_argument("--data", help="JSONL file with {query, passage} pairs")
    p.add_argument(
        "--mteb-task",
        choices=list(SUPPORTED_IR_TASKS),
        metavar="TASK",
        help="Published IR benchmark to download and evaluate on",
    )
    p.add_argument(
        "--max-queries",
        type=int,
        default=50,
        help="Max queries to evaluate in IR mode (default: 50)",
    )
    p.add_argument(
        "--max-corpus",
        type=int,
        default=None,
        help="Max corpus docs in IR mode; relevant docs always included (default: task-specific)",
    )
    p.add_argument("--seed", type=int, default=42, help="Random seed for sampling (default: 42)")
    return p.parse_args()


async def main() -> None:
    args = parse_args()
    random.seed(args.seed)

    if args.mteb_task and args.data:
        print("ERROR: --mteb-task and --data are mutually exclusive.")
        sys.exit(1)

    preset_a = args.preset_a or pick_preset("Select preset A:")
    preset_b = args.preset_b or pick_preset("Select preset B:")

    if preset_a == preset_b:
        print("WARNING: both presets are the same — results will be identical.")

    # ---- IR mode -----------------------------------------------------------
    if args.mteb_task:
        task_info = SUPPORTED_IR_TASKS[args.mteb_task]
        print(f"\nEmbedding evaluation — {args.mteb_task}: {task_info['desc']}")
        print(f"Max queries: {args.max_queries}  |  Seed: {args.seed}")

        corpus, queries, qrels = load_ir_dataset(
            args.mteb_task, args.max_queries, args.max_corpus
        )

        print(f"\nEvaluating preset A: {preset_a}")
        metrics_a = await evaluate_preset_ir(preset_a, corpus, queries, qrels)

        print(f"\nEvaluating preset B: {preset_b}")
        metrics_b = await evaluate_preset_ir(preset_b, corpus, queries, qrels)

        metric_keys = ["nDCG@10", "MRR@10", "Recall@1", "Recall@5", "Recall@10"]
        corpus_info = {
            "name": args.mteb_task,
            "description": task_info["desc"],
            "n_corpus": len(corpus),
            "n_queries": len(queries),
            "n_relevant_pairs": sum(len(r) for r in qrels.values()),
            "seed": args.seed,
        }
        print_results(preset_a, preset_b, metrics_a, metrics_b, metric_keys, corpus_info)
        return

    # ---- Pair mode ---------------------------------------------------------
    if args.data:
        lines = Path(args.data).read_text(encoding="utf-8").splitlines()
        pairs = [json.loads(l) for l in lines if l.strip()]
        source = args.data
    else:
        pairs = BUILTIN_PAIRS
        source = f"built-in corpus ({len(pairs)} pairs)"

    print(f"\nEmbedding evaluation — {source}")
    print(f"Corpus size: {len(pairs)} passages  |  Queries: {len(pairs)}")

    print(f"\nEvaluating preset A: {preset_a}")
    metrics_a = await evaluate_preset_pairs(preset_a, pairs)

    print(f"\nEvaluating preset B: {preset_b}")
    metrics_b = await evaluate_preset_pairs(preset_b, pairs)

    metric_keys = ["MRR@10", "Recall@1", "Recall@5", "Recall@10"]
    corpus_info = {
        "name": source,
        "n_corpus": len(pairs),
        "n_queries": len(pairs),
    }
    print_results(preset_a, preset_b, metrics_a, metrics_b, metric_keys, corpus_info)

    # Per-query rank breakdown
    n = len(pairs)
    mismatches = [i for i in range(n) if metrics_a["ranks"][i] != metrics_b["ranks"][i]]
    if mismatches:
        print(f"\nPer-query rank differences ({len(mismatches)} queries differed):")
        print(f"  {'#':<4} {'Rank-A':>6} {'Rank-B':>6}  Query (truncated)")
        print("  " + "-" * 62)
        for i in mismatches:
            q = pairs[i]["query"][:55]
            print(f"  {i+1:<4} {metrics_a['ranks'][i]:>6} {metrics_b['ranks'][i]:>6}  {q}")
    else:
        print("\nAll queries returned the same rank for both presets.")


if __name__ == "__main__":
    asyncio.run(main())
