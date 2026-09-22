"""MiMo regression coverage using synthetic keys and offline API responses."""
import json
import os
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from local_web.provider_models import fetch_provider_models
from local_web.provider_test import ProviderTestError, test_provider as probe_provider
from src.runtime import config
from src.runtime.model_config import normalize_base_url, validate_model_config


class MimoCompatibilityTest(unittest.TestCase):
    def test_shared_url_cases_are_idempotent(self):
        cases = json.loads(Path(__file__).with_name("provider_url_cases.json").read_text())
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(normalize_base_url(value), expected)
                self.assertEqual(normalize_base_url(expected), expected)

    def test_invalid_urls_are_not_repaired(self):
        for value in ("http://api.xiaomimimo.com", "https://user:secret@api.xiaomimimo.com", "https://api.xiaomimimo.com/?key=secret"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_base_url(value)

    def test_saved_config_and_environment_override_resolve_for_summarizer(self):
        document = validate_model_config({"providers": [{
            "name": "Mimo", "base_url": "https://api.xiaomimimo.com",
            "api_key_env": "LLM_MIMO_API_KEY", "base_url_env": "LLM_MIMO_BASE_URL",
            "models": ["mimo-test"],
        }]})
        self.assertEqual(document["providers"][0]["default_base_url"], "https://api.xiaomimimo.com/v1")
        for override in ("", "https://api.xiaomimimo.com/", "https://api.xiaomimimo.com/v1/chat/completions"):
            with self.subTest(override=override), patch.object(config, "MODEL_PROVIDERS", document["providers"]), patch.dict(os.environ, {
                "LLM_MIMO_API_KEY": "fake-key", "LLM_MIMO_BASE_URL": override,
            }):
                self.assertEqual(config.resolve_model_providers()[0]["base_url"], "https://api.xiaomimimo.com/v1")

    def test_probe_repairs_root_and_uses_documented_token_limit(self):
        for base in ("https://api.xiaomimimo.com", "https://api.xiaomimimo.com/v1/chat/completions"):
            response = BytesIO(json.dumps({"choices": [{"message": {"content": "OK"}}]}).encode())
            with self.subTest(base=base), patch("urllib.request.urlopen", return_value=response) as urlopen:
                result = probe_provider(base, "mimo-test", "fake-key")
            request = urlopen.call_args.args[0]
            self.assertEqual(request.full_url, "https://api.xiaomimimo.com/v1/chat/completions")
            self.assertEqual(request.get_header("Authorization"), "Bearer fake-key")
            payload = json.loads(request.data)
            self.assertNotIn("max_tokens", payload)
            self.assertEqual(payload["max_completion_tokens"], 32)
            self.assertEqual(payload["thinking"], {"type": "disabled"})
            self.assertTrue(result["ok"])

    def test_directory_uses_same_base_as_probe(self):
        for base in ("https://api.xiaomimimo.com", "https://api.xiaomimimo.com/v1/chat/completions/"):
            opener = MagicMock()
            opener.open.return_value = BytesIO(b'{"data":[{"id":"mimo-test"}]}')
            with self.subTest(base=base), patch("urllib.request.build_opener", return_value=opener):
                result = fetch_provider_models(base, "fake-key")
            self.assertEqual(opener.open.call_args.args[0].full_url, "https://api.xiaomimimo.com/v1/models")
            self.assertEqual(result, {"models": ["mimo-test"]})

    def test_404_explains_configuration_without_dumping_html(self):
        error = urllib.error.HTTPError("https://api.xiaomimimo.com/v1/chat/completions", 404,
                                       "Not Found", {}, BytesIO(b"<html>fake-key</html>"))
        with patch("urllib.request.urlopen", side_effect=error), self.assertRaises(ProviderTestError) as caught:
            probe_provider("https://api.xiaomimimo.com", "mimo-test", "fake-key")
        self.assertIn("404", str(caught.exception))
        self.assertIn("模型 ID", str(caught.exception))
        self.assertNotIn("<html>", str(caught.exception))
        self.assertNotIn("fake-key", str(caught.exception))
