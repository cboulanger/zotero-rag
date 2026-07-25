"""Unit tests for backend.services.extraction's create_document_extractor factory."""

import inspect
import unittest

from backend.services.extraction import create_document_extractor


class TestExtractorFactory(unittest.TestCase):
    def test_kreuzberg_extractor_accepts_ocr_language_kwarg(self):
        """Verify that ocr_language is a parameter of extract_and_chunk, not constructor-level."""
        extractor = create_document_extractor(backend="kreuzberg", ocr_enabled=True)
        # Doesn't raise — extract_and_chunk's ocr_language kwarg exists on this instance.
        sig = inspect.signature(extractor.extract_and_chunk)
        self.assertIn("ocr_language", sig.parameters)


if __name__ == "__main__":
    unittest.main()
