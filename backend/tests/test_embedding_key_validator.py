"""Unit tests for validate_embedding_key."""

import unittest
from unittest.mock import AsyncMock, patch

from backend.config.presets import EmbeddingConfig
from backend.services.embedding_key_validator import validate_embedding_key
from backend.services.embeddings import EmbeddingAuthenticationError


def _config():
    return EmbeddingConfig(
        model_type="remote",
        model_name="multilingual-e5-large-instruct",
        model_kwargs={"api_key_env": "KISSKI_API_KEY", "base_url": "https://example.test/v1"},
    )


class ValidateEmbeddingKeyTest(unittest.IsolatedAsyncioTestCase):
    async def test_valid_key_returns_ok(self):
        with patch("backend.services.embedding_key_validator.create_embedding_service") as mock_create:
            mock_service = AsyncMock()
            mock_service.embed_text = AsyncMock(return_value=[0.1, 0.2])
            mock_create.return_value = mock_service
            result = await validate_embedding_key("GOODKEY", _config())
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.key_name, "KISSKI_API_KEY")

    async def test_auth_error_returns_invalid(self):
        with patch("backend.services.embedding_key_validator.create_embedding_service") as mock_create:
            mock_service = AsyncMock()
            mock_service.embed_text = AsyncMock(side_effect=EmbeddingAuthenticationError("bad creds"))
            mock_create.return_value = mock_service
            result = await validate_embedding_key("BADKEY", _config())
        self.assertEqual(result.status, "invalid")
        self.assertIn("bad creds", result.reason)

    async def test_network_error_returns_unverified(self):
        with patch("backend.services.embedding_key_validator.create_embedding_service") as mock_create:
            mock_service = AsyncMock()
            mock_service.embed_text = AsyncMock(side_effect=ConnectionError("timeout"))
            mock_create.return_value = mock_service
            result = await validate_embedding_key("SOMEKEY", _config())
        self.assertEqual(result.status, "unverified")

    async def test_local_model_type_returns_unverified_without_testing(self):
        local_config = EmbeddingConfig(model_type="local", model_name="some-local-model")
        with patch("backend.services.embedding_key_validator.create_embedding_service") as mock_create:
            result = await validate_embedding_key("IRRELEVANT", local_config)
        mock_create.assert_not_called()
        self.assertEqual(result.status, "unverified")


if __name__ == "__main__":
    unittest.main()
