import unittest

from api_response import extract_response_text


class ApiResponseTest(unittest.TestCase):
    def test_anthropic_blocks_are_concatenated_without_thinking(self):
        response = {
            "content": [
                {"type": "thinking", "thinking": "hidden"},
                {"type": "text", "text": "first"},
                {"type": "text", "text": "second"},
            ]
        }
        self.assertEqual(extract_response_text(response), "first\nsecond")

    def test_openai_message_string_is_supported(self):
        response = {"choices": [{"message": {"content": "answer"}}]}
        self.assertEqual(extract_response_text(response), "answer")

    def test_missing_visible_text_returns_none(self):
        response = {"content": [{"type": "thinking", "thinking": "hidden"}]}
        self.assertIsNone(extract_response_text(response))


if __name__ == "__main__":
    unittest.main()
