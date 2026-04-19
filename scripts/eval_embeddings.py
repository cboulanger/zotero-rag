"""
Compare embedding quality and speed between two presets.

Usage:
    uv run python scripts/eval_embeddings.py
    uv run python scripts/eval_embeddings.py --preset-a remote-kisski --preset-b cloud-server-kisski
    uv run python scripts/eval_embeddings.py --preset-a remote-kisski --preset-b cloud-server-kisski --data pairs.jsonl

Evaluation method (self-retrieval):
  - For each (query, passage) pair: embed all passages as the corpus, embed the
    query, rank corpus by cosine similarity, check where the target passage lands.
  - Metrics: MRR@10, Recall@{1, 5, 10}, encode throughput (texts/sec).

Custom data format (JSONL, one object per line):
    {"query": "...", "passage": "..."}
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Optional

import numpy as np

# Allow running from repo root without installing the package.
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.config.presets import PRESETS, get_preset
from backend.services.embeddings import create_embedding_service

# ---------------------------------------------------------------------------
# Built-in multilingual test corpus
# Each entry: query + the one passage in the corpus that is most relevant.
# Queries and passages are intentionally paraphrased (not identical) to test
# semantic understanding rather than surface overlap.
# ---------------------------------------------------------------------------
BUILTIN_PAIRS: list[dict[str, str]] = [
    # English – natural sciences
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
    # English – humanities / social sciences
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
    # German – natural sciences
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
    # German – humanities
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
    # Cross-lingual: English query → German passage (and vice versa)
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
    # More English pairs to enlarge corpus
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
# Helpers
# ---------------------------------------------------------------------------

def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-10))


def rank_of_target(
    query_vec: np.ndarray,
    corpus_vecs: np.ndarray,
    target_idx: int,
) -> int:
    """Return 1-based rank of corpus[target_idx] when sorted by similarity to query."""
    sims = corpus_vecs @ query_vec / (
        np.linalg.norm(corpus_vecs, axis=1) * np.linalg.norm(query_vec) + 1e-10
    )
    order = np.argsort(-sims)  # descending
    return int(np.where(order == target_idx)[0][0]) + 1


def compute_metrics(ranks: list[int], ks: tuple[int, ...] = (1, 5, 10)) -> dict:
    n = len(ranks)
    metrics: dict = {"MRR@10": sum(1 / r for r in ranks if r <= 10) / n}
    for k in ks:
        metrics[f"Recall@{k}"] = sum(1 for r in ranks if r <= k) / n
    return metrics


def fmt(val: float) -> str:
    return f"{val:.3f}"


def pick_preset(prompt: str) -> str:
    available = sorted(PRESETS.keys())
    print(f"\n{prompt}")
    for i, name in enumerate(available, 1):
        preset = PRESETS[name]
        emb = preset.embedding
        src = emb.model_type
        print(f"  {i:2d}. {name:<30s}  [{src}] {emb.model_name}")
    while True:
        raw = input("Enter number or preset name: ").strip()
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(available):
                return available[idx]
        elif raw in PRESETS:
            return raw
        print("  Invalid choice, try again.")


# ---------------------------------------------------------------------------
# Core evaluation
# ---------------------------------------------------------------------------

async def evaluate_preset(
    preset_name: str,
    pairs: list[dict[str, str]],
) -> dict:
    preset = get_preset(preset_name)
    svc = create_embedding_service(preset.embedding)

    passages = [p["passage"] for p in pairs]
    queries = [p["query"] for p in pairs]

    print(f"\n[{preset_name}] Embedding {len(passages)} corpus passages...", flush=True)
    t0 = time.perf_counter()
    passage_vecs = np.array(await svc.embed_batch(passages))
    corpus_time = time.perf_counter() - t0
    corpus_throughput = len(passages) / corpus_time

    print(f"[{preset_name}] Embedding {len(queries)} queries...", flush=True)
    t1 = time.perf_counter()
    query_vecs = np.array(await svc.embed_batch(queries))
    query_time = time.perf_counter() - t1

    total_throughput = (len(passages) + len(queries)) / (corpus_time + query_time)

    ranks = [
        rank_of_target(query_vecs[i], passage_vecs, i)
        for i in range(len(pairs))
    ]

    metrics = compute_metrics(ranks)
    metrics["throughput_texts_per_sec"] = round(total_throughput, 1)
    metrics["corpus_time_s"] = round(corpus_time, 2)
    metrics["embedding_dim"] = passage_vecs.shape[1]
    metrics["ranks"] = ranks

    # Clean up local model to free RAM before loading the next one.
    if hasattr(svc, "close"):
        svc.close()

    return metrics


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Compare embedding quality and speed between two presets."
    )
    p.add_argument("--preset-a", help="First preset name")
    p.add_argument("--preset-b", help="Second preset name")
    p.add_argument(
        "--data",
        help="Path to JSONL file with {query, passage} pairs (default: built-in corpus)",
    )
    return p.parse_args()


def load_pairs(path: Optional[str]) -> list[dict[str, str]]:
    if path is None:
        return BUILTIN_PAIRS
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    pairs = [json.loads(l) for l in lines if l.strip()]
    for i, pair in enumerate(pairs):
        if "query" not in pair or "passage" not in pair:
            raise ValueError(f"Line {i+1}: each entry must have 'query' and 'passage' keys")
    return pairs


async def main() -> None:
    args = parse_args()

    pairs = load_pairs(args.data)
    n = len(pairs)
    source = args.data if args.data else f"built-in corpus ({n} pairs)"
    print(f"\nEmbedding evaluation — {source}")
    print(f"Corpus size: {n} passages  |  Queries: {n}")

    preset_a = args.preset_a or pick_preset("Select preset A:")
    preset_b = args.preset_b or pick_preset("Select preset B:")

    if preset_a == preset_b:
        print("WARNING: both presets are the same — results will be identical.")

    print(f"\nEvaluating preset A: {preset_a}")
    metrics_a = await evaluate_preset(preset_a, pairs)

    print(f"\nEvaluating preset B: {preset_b}")
    metrics_b = await evaluate_preset(preset_b, pairs)

    # ---------------------------------------------------------------------------
    # Report
    # ---------------------------------------------------------------------------
    col_w = 22
    metric_keys = ["MRR@10", "Recall@1", "Recall@5", "Recall@10"]

    print("\n" + "=" * 70)
    print("RESULTS")
    print("=" * 70)
    print(f"{'Metric':<{col_w}} {preset_a:<{col_w}} {preset_b:<{col_w}} {'Delta':>8}")
    print("-" * 70)

    for key in metric_keys:
        va = metrics_a[key]
        vb = metrics_b[key]
        delta = vb - va
        sign = "+" if delta >= 0 else ""
        print(f"{key:<{col_w}} {fmt(va):<{col_w}} {fmt(vb):<{col_w}} {sign}{fmt(delta):>8}")

    print("-" * 70)
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
    print("=" * 70)

    # Per-query breakdown for mismatches
    mismatches = [
        i for i in range(n)
        if metrics_a["ranks"][i] != metrics_b["ranks"][i]
    ]
    if mismatches:
        print(f"\nPer-query rank differences ({len(mismatches)} queries differed):")
        print(f"  {'#':<4} {'Rank-A':>6} {'Rank-B':>6}  Query (truncated)")
        print("  " + "-" * 60)
        for i in mismatches:
            q = pairs[i]["query"][:55]
            ra = metrics_a["ranks"][i]
            rb = metrics_b["ranks"][i]
            print(f"  {i+1:<4} {ra:>6} {rb:>6}  {q}")
    else:
        print("\nAll queries returned the same rank for both presets.")


if __name__ == "__main__":
    asyncio.run(main())
