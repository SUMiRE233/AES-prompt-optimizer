import unittest

from offline_smoke import run


class OfflineSmokeTest(unittest.TestCase):
    def test_synthetic_smoke_requires_no_network_or_private_data(self):
        result = run()

        self.assertEqual(result["status"], "pass")
        self.assertFalse(result["network_used"])
        self.assertEqual(result["rows"], 6)
        self.assertEqual(result["dimensions"], ["content", "expression", "structure"])


if __name__ == "__main__":
    unittest.main()
