# Judging rubric (the reasoning step)

Regex matching in `gold/questions.json` (`answer_patterns`) is only the
automatic **floor**: it proves a literal figure or name appears. It cannot see a
paraphrase, a translation, a differently-worded correct statement, or a
conclusion that has to be *inferred*, and it cannot tell a correct attribution
from a swapped one. The reviewing agent (you) closes that gap by reading every
answer and writing one judgment file per run. Judged recall replaces the regex
recall in the composite; the report shows where the two disagree.

## Procedure per packet

`judge.py prepare RUN_DIR` creates, for every answered query:

- `judge/<stem>.packet.json`: question, language, `minimum_answer`, `judge_notes`,
  `inference_chain`, the gold facts (id, text, level, whether the regex hit), the
  answer, the numbered sources (`[S3]` in the answer is `sources[n=3]`) and the
  automatic findings.
- `judge/<stem>.context.txt`: the exact context passed to the LLM. This is the
  only ground truth for "is this claim supported".
- `judgments/<stem>.json`: an empty skeleton to complete.

For each packet:

1. **Form your own expectation first.** Read `minimum_answer` and, for
   questions that have one, `inference_chain`. Decide what a complete correct
   answer must say before you read the answer; fluent text is not evidence.
2. **Judge every gold fact** (`facts.<id>`), including facts the regex already
   hit (it can be a false positive) and the judge-only inference facts:

   | verdict | meaning |
   |---|---|
   | `verbatim` | stated with the source's literal figures/names |
   | `semantic` | same meaning in other words, another unit/number format, or another language (e.g. "etwa die Hälfte" for 52% is `partial`, "52 %" in German is `verbatim`/`semantic`) |
   | `inferred` | not stated in any source; the answer itself draws the correct conclusion by combining sources, and the cited sources support the premises |
   | `partial` | half right: only one of two parts, an approximation, or the premises without the conclusion |
   | `missing` | absent, or only gestured at |
   | `contradicted` | the answer states the opposite or a wrong value |

   `attribution_ok`: `false` when the fact is present but assigned to the wrong
   document, tool or system (47% given to Zotero, Neo4j given to PyZoBot). A
   wrong attribution removes the credit. `evidence`: a quote of at most 20
   words from the answer, or why it is missing. No evidence, no credit.
3. **Unsupported claims.** List each factual claim in the answer that is not
   supported by `context.txt`. World knowledge does not count: the generation
   prompt forbids supplementing the context, so a true-but-uncontextual claim is
   unsupported. Also list wrong claims about sources (`attribution_errors`).
4. **Citation support** (0..1). For each cited sentence check that source `[SN]`
   (and the page, if given) really contains the claim; the score is
   supported / checked. Sentences without a citation are handled by the
   automatic coverage metric, not here.
5. **Inference quality** (only when `inference_chain` exists): `1` the answer
   reaches the required conclusion and the reasoning is sound and grounded; `0.5`
   it states the premises or a weaker/unclear conclusion, or reasons correctly
   from only one source; `0` no inference, a wrong inference, or a conclusion
   not licensed by the sources (e.g. "Zotero is unpopular").
6. **Overall (0..5)** for the answer as a user would experience it:
   5 complete, correct, well-cited, right language; 4 minor gap; 3 usable but
   incomplete or weakly cited; 2 major gaps or a significant error; 1 mostly
   wrong or unusable; 0 empty/refusal/wrong language entirely.
7. `notes`: one or two sentences (what went wrong or right, failure pattern).

## Judging rules

- Accept equivalent number formats and languages: `79,5 %`, `79.5%`, `1 613`,
  `1.613` and `1,613` are the same. The answer language is not a reason to deny
  a fact (language compliance is scored separately).
- Judge each fact independently; do not let one miss colour the others.
- Hard questions: be strict about *attribution* and *inference*. Listing two
  papers' claims side by side without noticing they conflict is `partial` at
  best for a contrast fact.
- Be consistent across runs: the same wording earns the same verdict for every
  preset and model. Do not look at which model produced the answer before
  judging if you can avoid it (the packet shows it; ignore it).
- Do not edit the answer, the packet or the gold file while judging. If a gold
  fact itself looks wrong or ambiguous, finish the judging, then note it in
  your report and fix the gold file separately (see SKILL.md, "Maintaining the
  gold standard").

## Delegating many packets

For more than about 20 packets give each subagent one preset/model (or a slice
of questions) and say: absolute run directory, "read-only: read packets and
context files, write only `judgments/<stem>.json`, never query the backend or
change scripts", the rubric above (point at this file), and ask for a short
summary (counts per verdict, recurring failure patterns). Afterwards run
`judge.py validate RUN_DIR` and spot-check a few judgments yourself,
particularly all hard-tier ones.

## Judgment file schema

```json
{
  "run": "<stem>",
  "facts": {
    "zotero_no_api": {"verdict": "semantic", "attribution_ok": true, "evidence": "\"Zotero exposes no way to download user data\""},
    "inference_access_not_popularity": {"verdict": "inferred", "attribution_ok": true, "evidence": "..."}
  },
  "unsupported_claims": ["claims that Mendeley has 10 million users"],
  "attribution_errors": [],
  "citation_support": 0.8,
  "inference_quality": 1,
  "overall": 4,
  "notes": "Correct and well attributed; one uncited aside."
}
```

`inference_quality` is present only for questions with an `inference` chain.
