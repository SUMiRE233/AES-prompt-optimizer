import os
import unittest
from unittest.mock import patch

from etype_preference_analyzer import ContrastiveETypeAnalyzer
from prompt_optimizer import PromptOptimizer


class ModelConfigurationTest(unittest.TestCase):
    def test_optimizer_model_comes_from_environment(self):
        with patch.dict(os.environ, {"AES_OPTIMIZER_MODEL": "new-optimizer-model"}):
            optimizer = PromptOptimizer()
        self.assertEqual(optimizer.model, "new-optimizer-model")

    def test_e_analysis_model_comes_from_environment(self):
        with patch.dict(os.environ, {"AES_E_ANALYSIS_MODEL": "new-analysis-model"}):
            analyzer = ContrastiveETypeAnalyzer()
        self.assertEqual(analyzer.model, "new-analysis-model")


if __name__ == "__main__":
    unittest.main()
