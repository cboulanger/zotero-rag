"""
Ad-hoc integration test: metadata query routing against the dev server.

Tests that the query orchestrator correctly routes bibliographic questions
to the MetadataAgent and returns answers that reference known library content.

Library under test: Zotero group 6297749 (public)
  https://api.zotero.org/groups/6297749/items/

Run:
    uv run python scripts/test_metadata_queries.py [--url http://localhost:8119] [--library-id <id>]
"""

import argparse
import json
import sys
import urllib.request
import urllib.error

LIBRARY_ID = "6297749"
BASE_URL = "http://localhost:8119"


# ---------------------------------------------------------------------------
# Test cases
# Each entry:
#   question  : sent to POST /api/query
#   expected  : list of strings — at least one must appear in the answer (case-insensitive)
#   description: human-readable intent
# ---------------------------------------------------------------------------
TEST_CASES = [
    {
        "description": "List items by a specific author (Fenner)",
        "question": "Which items in the library were written by Fenner?",
        "expected": ["Fenner", "Reference Management"],
    },
    {
        "description": "Items published in a specific year range (2012–2015)",
        "question": "List all journal articles published between 2012 and 2015.",
        "expected": ["2012", "2013", "2014", "2015", "Basak", "Tramullas", "Li"],
    },
    {
        "description": "Find items by item type (book)",
        "question": "Are there any books in the library? If so, list them.",
        "expected": ["EndNote", "Agrawal", "book"],
    },
]


def _post_query(base_url: str, library_id: str, question: str, enable_routing: bool = True) -> dict:
    payload = json.dumps({
        "question": question,
        "library_ids": [library_id],
        "enable_routing": enable_routing,
    }).encode()
    req = urllib.request.Request(
        f"{base_url}/api/query",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def _check_health(base_url: str, retries: int = 5, delay: float = 2.0) -> bool:
    import time
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        if attempt < retries:
            print(f"  Server not ready, retrying ({attempt}/{retries})...")
            time.sleep(delay)
    return False


def _contains_any(text: str, needles: list[str]) -> list[str]:
    text_lower = text.lower()
    return [n for n in needles if n.lower() in text_lower]


def run_tests(base_url: str, library_id: str) -> int:
    print(f"Target: {base_url}  |  Library: {library_id}\n")

    if not _check_health(base_url):
        print(f"[FAIL] Server not reachable at {base_url}/health")
        return 1

    passed = 0
    failed = 0

    for i, tc in enumerate(TEST_CASES, 1):
        print(f"[{i}/{len(TEST_CASES)}] {tc['description']}")
        print(f"  Q: {tc['question']}")
        try:
            result = _post_query(base_url, library_id, tc["question"])
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            print(f"  [FAIL] HTTP {e.code}: {body[:200]}\n")
            failed += 1
            continue
        except Exception as e:
            print(f"  [FAIL] Request error: {e}\n")
            failed += 1
            continue

        answer_html = result.get("answer", "")
        sources = result.get("sources", [])

        # Strip simple HTML tags for matching
        import re
        answer_text = re.sub(r"<[^>]+>", " ", answer_html)

        hits = _contains_any(answer_text, tc["expected"])
        source_titles = [s.get("title", "") for s in sources]

        if hits:
            print(f"  [PASS] Answer contains: {hits}")
        else:
            print(f"  [FAIL] None of {tc['expected']} found in answer")
            print(f"  Answer (first 300 chars): {answer_text[:300]}")

        print(f"  Sources ({len(sources)}): {[t[:50] for t in source_titles[:5]]}")
        print()

        if hits:
            passed += 1
        else:
            failed += 1

    print(f"Results: {passed} passed, {failed} failed out of {len(TEST_CASES)} tests")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test metadata query routing against the dev server.")
    parser.add_argument("--url", default=BASE_URL, help=f"Backend URL (default: {BASE_URL})")
    parser.add_argument("--library-id", default=LIBRARY_ID, help=f"Zotero library ID (default: {LIBRARY_ID})")
    args = parser.parse_args()

    sys.exit(run_tests(args.url, args.library_id))
