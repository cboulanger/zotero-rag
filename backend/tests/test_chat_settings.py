"""Unit tests for the follow-up-chat settings fields."""

import os
import unittest
from unittest.mock import patch

from backend.config.settings import Settings


class ChatSettingsTest(unittest.TestCase):
    def test_metadata_narrowing_threshold_defaults_to_50(self):
        s = Settings()
        self.assertEqual(s.metadata_narrowing_threshold, 50)

    def test_metadata_narrowing_threshold_from_env(self):
        with patch.dict(os.environ, {"METADATA_NARROWING_THRESHOLD": "10"}):
            s = Settings()
        self.assertEqual(s.metadata_narrowing_threshold, 10)

    def test_max_conversation_context_chars_defaults_to_6000(self):
        s = Settings()
        self.assertEqual(s.max_conversation_context_chars, 6000)

    def test_max_conversation_context_chars_from_env(self):
        with patch.dict(os.environ, {"MAX_CONVERSATION_CONTEXT_CHARS": "2000"}):
            s = Settings()
        self.assertEqual(s.max_conversation_context_chars, 2000)


if __name__ == "__main__":
    unittest.main()
