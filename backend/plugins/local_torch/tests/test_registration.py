"""The local_torch plugin registers a torch engine for every side."""

import unittest

from backend.pluginhost import select_local_engine


class RegistrationTest(unittest.TestCase):
    def test_local_torch_serves_every_side(self):
        for side in ("embedding", "llm", "rerank"):
            self.assertEqual(select_local_engine(side).plugin_id, "local_torch")
            self.assertEqual(select_local_engine(side, "torch").engine, "torch")


if __name__ == "__main__":
    unittest.main()
