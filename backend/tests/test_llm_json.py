"""Unit tests for backend.utils.llm_json."""

import unittest

from backend.utils.llm_json import parse_json_array, parse_json_object


class TestParseJsonObject(unittest.TestCase):
    def test_plain_json(self):
        data = parse_json_object('{"agents": ["rag"], "year_min": null}')
        self.assertEqual(data["agents"], ["rag"])

    def test_strips_markdown_fence(self):
        raw = '```json\n{"agents": ["metadata"]}\n```'
        data = parse_json_object(raw)
        self.assertEqual(data["agents"], ["metadata"])

    def test_strips_plain_code_fence(self):
        raw = '```\n{"agents": ["rag", "metadata"]}\n```'
        data = parse_json_object(raw)
        self.assertEqual(data["agents"], ["rag", "metadata"])

    def test_json_with_leading_text(self):
        raw = 'Here is the answer: {"agents": ["rag"]}'
        data = parse_json_object(raw)
        self.assertEqual(data["agents"], ["rag"])

    def test_raises_on_no_braces(self):
        with self.assertRaises(ValueError):
            parse_json_object("no json here at all")

    def test_raises_on_invalid_json(self):
        with self.assertRaises(Exception):
            parse_json_object("{bad json}")


class TestParseJsonArray(unittest.TestCase):
    def test_plain_array(self):
        data = parse_json_array('[{"title": "Intro"}, {"title": "Conclusion"}]')
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["title"], "Intro")

    def test_strips_markdown_fence(self):
        raw = '```json\n[{"title": "Intro"}]\n```'
        data = parse_json_array(raw)
        self.assertEqual(data[0]["title"], "Intro")

    def test_json_with_leading_text(self):
        raw = 'Here is the chapter list: [{"title": "Intro"}]'
        data = parse_json_array(raw)
        self.assertEqual(data[0]["title"], "Intro")

    def test_raises_on_no_brackets(self):
        with self.assertRaises(ValueError):
            parse_json_array("no json here at all")

    def test_raises_on_invalid_json(self):
        with self.assertRaises(Exception):
            parse_json_array("[bad json]")


if __name__ == "__main__":
    unittest.main()
