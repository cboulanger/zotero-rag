"""Tests for backend.api.admin_pages."""

import unittest

from fastapi.testclient import TestClient

from backend.main import app


class TestReviewPage(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_renders_without_error(self):
        response = self.client.get("/admin/review")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Chapter Review Queue", response.text)


class TestRunPage(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_renders_without_error(self):
        response = self.client.get("/admin/run")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Run Chapter-Linking Pipeline", response.text)
