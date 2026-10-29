"""Model configuration is validated without contacting the provider."""

import os
import unittest
from unittest.mock import patch

from evaluation_agent.model_config import model_from_environment


class ModelConfigTests(unittest.TestCase):
    def test_unconfigured_server_runs_without_model(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            self.assertIsNone(model_from_environment())

    def test_bounded_compatible_model_config(self):
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "local-fake-key",
            "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
            "MODEL_NAME": "test-model",
            "MODEL_CONTEXT_TOKENS": "128000",
        }):
            model = model_from_environment()
        self.assertEqual(model.model_name, "test-model")
        self.assertEqual(model._default_params["max_completion_tokens"], 8192)
        self.assertTrue(model.streaming)
        self.assertEqual(model._get_ls_params()["ls_provider"], "openai")

    def test_small_model_window_rejected_before_startup(self):
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "local-fake-key", "MODEL_NAME": "small",
            "MODEL_CONTEXT_TOKENS": "32000",
        }):
            with self.assertRaisesRegex(RuntimeError, "smaller"):
                model_from_environment()

    def test_missing_model_window_rejected_before_startup(self):
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "local-fake-key", "MODEL_NAME": "unknown",
            "MODEL_CONTEXT_TOKENS": "",
        }):
            with self.assertRaisesRegex(RuntimeError, "MODEL_CONTEXT_TOKENS is required"):
                model_from_environment()


if __name__ == "__main__":
    unittest.main()
