# Import Open Access Articles from OpenAlex

The script `scripts/openalex_import.py` bulk-imports all open-access articles
of a journal (identified by ISSN) from [OpenAlex](https://openalex.org) into
a Zotero group library, including PDF attachments.

## Step 1 — fetch article metadata into a local CSV

```bash
uv run python scripts/openalex_import.py fetch --issn 2050-084X --email you@example.com
```

This writes `.local/openalex_2050_084X.csv` with one row per OA article that
has a PDF URL. If interrupted, re-running resumes from where it left off.

## Step 2 — import into Zotero

```bash
uv run python scripts/openalex_import.py import \
    --issn 2050-084X \
    --group-id 12345 \
    --api-key YOUR_ZOTERO_API_KEY
```

This reads the CSV, creates a `journalArticle` item in the Zotero group
library for each pending row (fetching full metadata from OpenAlex), and
uploads the PDF as a child attachment. Already-imported rows are skipped,
making the command safe to re-run after interruption.

## Requirements

- `OPENALEX_EMAIL` / `--email` — recommended for the OpenAlex
  [polite pool](https://docs.openalex.org/how-to-use-the-api/rate-limits-and-authentication)
  (higher rate limits; no sign-up needed).
- `ZOTERO_API_KEY` / `--api-key` — required for the `import` command; must
  have read+write access to the group library. Create one at
  [zotero.org/settings/keys](https://www.zotero.org/settings/keys). This key
  is used only by this script — the server, automatic indexing, and the
  plugin all use separate, read-only keys.

Both env vars can be set in `.env` (see `.env.dist` for the template
entries). Only group libraries are supported.
