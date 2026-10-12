# RAG quality evaluation

The `rag-quality-eval` skill (`skills/rag-quality-eval/`) measures the quality
of the query pipeline — routing, retrieval, generation and citations — against
a fixed gold standard: 10 questions ranging from literal lookups to
cross-document inference, in English, German, French and Spanish, over the
public [test-rag-plugin](https://www.zotero.org/groups/6297749/test-rag-plugin)
Zotero group library. It runs each question through the real `/api/query`
endpoint for every preset/model you ask it to test, scores the answers
automatically (length, source coverage, citation compliance, language match),
and then has Claude judge what regex matching can't: paraphrases,
translations, correct attribution and inferred conclusions.

Everything an evaluation run needs beyond this repository — the library's
source PDFs, a prebuilt index snapshot, and every past report — lives in one
public Hugging Face dataset repo:
**[huggingface.co/datasets/cmboulanger/zotero-rag-gold](https://huggingface.co/datasets/cmboulanger/zotero-rag-gold)**.
It is used both as the **report target** (every published run is archived
there, so later runs have a baseline to compare against) and, for a cloud
session without the full local stack, as the **data source** that recreates
the environment.

This evaluation is a development/QA tool, not part of the plugin or the
production query pipeline — you don't need it to use zotero-rag, only to
benchmark a change to it.

## Running an evaluation locally

Use this when you have the full local environment: the backend running with
the Kreuzberg extraction sidecar (`npm start`), and the gold library already
indexed. Just ask an agent, for example:

> Use the rag-quality-eval skill to evaluate the `remote-kisski` preset
> against all its models on the full gold standard, then judge and report.

The skill (see its `SKILL.md` for the exact steps) checks the environment is
ready, asks you which preset(s)/model(s)/repetitions to run, executes the
questions, judges the answers, and writes a report to
`data/logs/rag_eval/<run>/report.md`. Once a run is judged and final (not a
smoke test), publish it so it becomes the next baseline:

```bash
uv run python skills/rag-quality-eval/scripts/hf_store.py push-report data/logs/rag_eval/<run>
```

This needs a Hugging Face token with **write** access to the dataset repo;
without one, the report stays local only.

## Running evaluation in the cloud

A cloud coding session usually has no container runtime and can't run the
Kreuzberg extraction sidecar, so it can't build the index itself. Instead it
restores the **production-equivalent index** — the same chunks and vectors
the local environment indexed — from the Hugging Face dataset repo, then runs
the same evaluation workflow against it.

To start one, open (or ask for) a cloud session and give it a prompt such as:

> Use the rag-quality-eval skill. Run `gold_env.py up`, then evaluate the
> `remote-kisski` preset on the full gold standard, judge the answers and
> report the results.

`gold_env.py up` starts a backend on an embedded Qdrant store, pulls the
snapshot matching the backend's embedding model from the dataset repo, and
verifies every gold document has chunks before continuing. From there, the
rest of the workflow (running questions, judging, reporting) is identical to
a local run, including publishing the finished report back to the same
dataset repo with `hf_store.py push-report`.

A cloud run is valid for anything that happens at or after retrieval:
`top_k`, score thresholds, deduplication, reranking, prompts, models and
routing. It is **not** valid for evaluating chunking or extraction changes —
without Kreuzberg, a cloud session can only re-index with a fallback
extractor whose chunks differ from production, so changes of that kind must
be evaluated locally and re-published.

To compare a cloud run against the latest published baseline:

```bash
uv run python skills/rag-quality-eval/scripts/hf_store.py pull-report latest
uv run python skills/rag-quality-eval/scripts/report.py data/logs/rag_eval/<new-run> \
  --baseline data/logs/rag_eval/latest
```

## The Hugging Face dataset repo

[cmboulanger/zotero-rag-gold](https://huggingface.co/datasets/cmboulanger/zotero-rag-gold)
is public (the gold library's documents are open access, CC BY 4.0) and holds:

| Path | Content |
|---|---|
| `sources/` | the gold library's PDFs plus `manifest.json` (item metadata, checksums) |
| `snapshots/<embedding-model>/latest.jsonl.gz` + `latest.json` | the production-equivalent index and how it was built |
| `reports/<run>/` | every published evaluation run, kept permanently |
| `reports/latest/` | a copy of the most recently published run |
| `reports/index.json`, `README.md` | run summaries and the dataset card, both regenerated from the repo's own contents |

Only compare runs built from the same gold-standard version and the same
index snapshot — `report.md` shows the gold version a run used, and the
dataset card shows each snapshot's provenance (extractor, code commit, chunk
counts).

See [skills/rag-quality-eval/SKILL.md](../skills/rag-quality-eval/SKILL.md)
for the full workflow, including preparing or refreshing the dataset repo
after a chunking, extraction or embedding-model change, and extending the
gold standard with new questions.
