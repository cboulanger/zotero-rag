"""Locale-appropriate real-word pools for scripts/evaluation_redaction/redact.py,
bucketed by character length so a redacted token can be replaced by a
same-length (or nearest-length) real word in the book's own language -- see
docs/superpowers/specs/2026-08-05-evaluation-corpus-redaction-design.md
section 6."""

from collections import defaultdict

from faker import Faker

# backend.services.chapter_ocr.detect_language returns one of these, or the
# combined-default "eng+deu+fra+spa" when detection is ambiguous -- mapped
# to English below, since an ambiguous-language book is the exception, not
# most of the evaluation set.
_LOCALE_NAMES = {"deu": "de_DE", "fra": "fr_FR", "spa": "es_ES", "eng": "en_US"}


def locale_for_detected_language(detected: str) -> str:
    return _LOCALE_NAMES.get(detected, "en_US")


def build_word_pool(detected_language: str) -> dict[int, list[str]]:
    """{word_length: [real dictionary words of that length, this language]},
    built once per book from Faker's locale lorem provider."""
    locale = locale_for_detected_language(detected_language)
    fake = Faker(locale)

    # Find the Lorem provider instance (it has word_list attribute)
    word_list = None
    for provider in fake.providers:
        if hasattr(provider, 'word_list'):
            word_list = provider.word_list
            break

    if not word_list:
        # Fallback to empty pool
        return {}

    pool: dict[int, list[str]] = defaultdict(list)
    for word in word_list:
        pool[len(word)].append(word)
    return dict(pool)


def pick_word(pool: dict[int, list[str]], length: int, seed: int) -> str:
    """Deterministic pick from `pool`'s bucket closest to `length` (exact
    match preferred; falls back to the nearest length that has any words at
    all, so a length with no dictionary entry doesn't crash)."""
    if not pool:
        return "x" * max(length, 1)
    candidates = pool.get(length)
    if not candidates:
        nearest_length = min(pool, key=lambda available: abs(available - length))
        candidates = pool[nearest_length]
    return candidates[seed % len(candidates)]
