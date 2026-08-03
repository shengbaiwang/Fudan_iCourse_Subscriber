from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from nacl.public import PrivateKey, SealedBox

from local_web.github_client import GitHubClient
from local_web.provider_test import ProviderTestError, test_provider
from local_web.state import ModelConfigStore
from src.runtime.model_config import select_runtime_model, validate_model_config


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _provider(
    name: str = "alpha",
    *,
    base_url: str = "https://alpha.example/v1",
    api_key_env: str = "LLM_ALPHA_API_KEY",
    models: list[str] | None = None,
) -> dict:
    return {
        "name": name,
        "base_url": base_url,
        "api_key_env": api_key_env,
        "models": models or ["alpha-chat"],
    }


def _import_runtime_config(model_config: str | None) -> subprocess.CompletedProcess[str]:
    """Import config in a fresh interpreter so module globals cannot leak."""
    env = os.environ.copy()
    env.pop("MODEL_PROVIDERS_JSON", None)
    if model_config is not None:
        env["MODEL_PROVIDERS_JSON"] = model_config
    return subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; "
                "from src.runtime import config; "
                "print(json.dumps(config.MODEL_PROVIDERS))"
            ),
        ],
        cwd=REPOSITORY_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


class RuntimeModelConfigurationTest(unittest.TestCase):
    def test_missing_environment_variable_falls_back_to_defaults(self):
        result = _import_runtime_config(None)

        self.assertEqual(result.returncode, 0, result.stderr)
        providers = json.loads(result.stdout)
        self.assertEqual(
            [item["name"] for item in providers],
            ["modelscope", "deepseek", "gemini"],
        )
        self.assertEqual(
            providers[0]["models"],
            [
                "deepseek-ai/DeepSeek-V4-Pro",
                "deepseek-ai/DeepSeek-V4-Flash",
            ],
        )

    def test_dynamic_configuration_preserves_provider_and_model_order(self):
        document = {
            "version": 1,
            "providers": [
                _provider(
                    "beta",
                    base_url="https://beta.example/openai/v1/",
                    api_key_env="LLM_BETA_API_KEY",
                    models=["beta-large", "beta-fast"],
                ),
                _provider(
                    "alpha",
                    models=["alpha-reasoning", "alpha-chat"],
                ),
            ],
        }

        result = _import_runtime_config(json.dumps(document))

        self.assertEqual(result.returncode, 0, result.stderr)
        providers = json.loads(result.stdout)
        self.assertEqual([item["name"] for item in providers], ["beta", "alpha"])
        self.assertEqual(providers[0]["models"], ["beta-large", "beta-fast"])
        self.assertEqual(
            providers[1]["models"], ["alpha-reasoning", "alpha-chat"]
        )
        self.assertEqual(
            providers[0]["default_base_url"],
            "https://beta.example/openai/v1",
        )

    def test_invalid_json_fails_closed_instead_of_using_defaults(self):
        result = _import_runtime_config('{"providers": [')

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid MODEL_PROVIDERS_JSON", result.stderr)
        self.assertNotIn("DeepSeek-V4-Pro", result.stdout)

    def test_launcher_exposes_only_selected_model_secrets(self):
        document = {
            "version": 1,
            "providers": [_provider()],
        }
        env = os.environ.copy()
        env.pop("UISPSW", None)
        env["MODEL_PROVIDERS_JSON"] = json.dumps(document)
        env["SECRETS_CONTEXT"] = json.dumps(
            {
                "LLM_ALPHA_API_KEY": "fake-model-key",
                "UISPSW": "must-not-reach-child",
            }
        )
        result = subprocess.run(
            [
                sys.executable,
                "scripts/provider_env.py",
                "--",
                sys.executable,
                "-c",
                (
                    "import json, os; "
                    "print(json.dumps({"
                    "'key': os.environ.get('LLM_ALPHA_API_KEY'), "
                    "'context': 'SECRETS_CONTEXT' in os.environ, "
                    "'uis': os.environ.get('UISPSW')}))"
                ),
            ],
            cwd=REPOSITORY_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        child = json.loads(result.stdout)
        self.assertEqual(child["key"], "fake-model-key")
        self.assertFalse(child["context"])
        self.assertIsNone(child["uis"])

    def test_base_url_secret_override_is_validated_before_use(self):
        document = {
            "version": 1,
            "providers": [
                {
                    **_provider(),
                    "base_url_env": "LLM_ALPHA_BASE_URL",
                }
            ],
        }
        env = os.environ.copy()
        env["MODEL_PROVIDERS_JSON"] = json.dumps(document)
        env["LLM_ALPHA_API_KEY"] = "fake-key"
        env["LLM_ALPHA_BASE_URL"] = "http://insecure.example/v1"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from src.runtime.config import resolve_model_providers; "
                    "print(resolve_model_providers())"
                ),
            ],
            cwd=REPOSITORY_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HTTPS URL", result.stderr)
        self.assertNotIn("fake-key", result.stderr)


class ModelConfigurationValidationTest(unittest.TestCase):
    def test_select_runtime_model_only_allows_a_saved_enabled_model(self):
        document = {
            "version": 1,
            "providers": [
                _provider("alpha", models=["alpha-pro", "alpha-fast"]),
                {**_provider("disabled", api_key_env="LLM_DISABLED_API_KEY"), "enabled": False},
            ],
        }

        selected = select_runtime_model(document, "alpha", "alpha-fast")
        self.assertEqual(selected[0]["name"], "alpha")
        self.assertEqual(selected[0]["models"], ["alpha-fast"])
        with self.assertRaisesRegex(ValueError, "已保存配置"):
            select_runtime_model(document, "alpha", "not-configured")
        with self.assertRaisesRegex(ValueError, "未启用"):
            select_runtime_model(document, "disabled", "alpha-chat")

    def test_duplicate_provider_names_are_rejected(self):
        document = {
            "version": 1,
            "providers": [
                _provider("duplicate", api_key_env="LLM_FIRST_API_KEY"),
                _provider("duplicate", api_key_env="LLM_SECOND_API_KEY"),
            ],
        }

        with self.assertRaisesRegex(ValueError, "供应商名称重复"):
            validate_model_config(document)

    def test_insecure_or_credential_bearing_urls_are_rejected(self):
        invalid_urls = [
            "http://provider.example/v1",
            "https://user:password@provider.example/v1",
            "https://provider.example/v1?api-version=2026-01-01",
            "https://provider.example/v1?",
            "https://provider.example/v1#",
        ]

        for base_url in invalid_urls:
            with self.subTest(base_url=base_url):
                with self.assertRaisesRegex(ValueError, "HTTPS URL"):
                    validate_model_config(
                        {"version": 1, "providers": [_provider(base_url=base_url)]}
                    )

    def test_unrelated_or_process_control_secret_names_are_rejected(self):
        for env_name in ("UISPSW", "PATH", "SMTP_PASSWORD"):
            with self.subTest(env_name=env_name):
                with self.assertRaisesRegex(ValueError, r"LLM_\*_API_KEY"):
                    validate_model_config(
                        {
                            "version": 1,
                            "providers": [_provider(api_key_env=env_name)],
                        }
                    )

    def test_boolean_version_is_not_accepted_as_version_one(self):
        with self.assertRaisesRegex(ValueError, "配置版本"):
            validate_model_config(
                {"version": True, "providers": [_provider()]}
            )

    def test_local_cache_drops_any_accidental_api_key_field(self):
        document = {
            "version": 1,
            "providers": [{**_provider(), "api_key": "must-not-persist"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            store = ModelConfigStore(Path(directory))
            store.save(document)
            stored = store.path.read_text("utf-8")
        self.assertNotIn("must-not-persist", stored)
        self.assertNotIn('"api_key"', stored)


class ProviderConnectionTest(unittest.TestCase):
    def test_http_error_never_echoes_the_api_key(self):
        api_key = "fake-sensitive-marker"
        error = urllib.error.HTTPError(
            "https://provider.example/v1/chat/completions",
            401,
            "Unauthorized",
            {},
            BytesIO(f"rejected {api_key}".encode()),
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(ProviderTestError) as caught:
                test_provider(
                    "https://provider.example/v1",
                    "test-model",
                    api_key,
                )
        self.assertNotIn(api_key, str(caught.exception))
        self.assertIn("***", str(caught.exception))


class RecordingSecretClient(GitHubClient):
    def __init__(self, public_key: bytes):
        super().__init__("alice", "fork", "token")
        self._actions_public_key = {
            "key": base64.b64encode(public_key).decode("ascii"),
            "key_id": "test-key-id",
        }
        self.calls: list[tuple[str, str, dict | None]] = []

    def _request(self, path, *, method="GET", payload=None, accept=None):
        self.calls.append((path, method, payload))
        return b""


class RecordingVariableClient(GitHubClient):
    def __init__(self, current_value: str | None):
        super().__init__("alice", "fork", "token")
        self.current_value = current_value
        self.calls: list[tuple[str, str, dict | None]] = []

    def repository_variable(self, name: str) -> str | None:
        self.variable_read = name
        return self.current_value

    def _request(self, path, *, method="GET", payload=None, accept=None):
        self.calls.append((path, method, payload))
        return b""


class GitHubModelConfigurationClientTest(unittest.TestCase):
    def test_repository_secret_payload_is_sealed_and_decryptable(self):
        private_key = PrivateKey.generate()
        client = RecordingSecretClient(bytes(private_key.public_key))
        plaintext = "plain-text-marker!not-base64"

        client.upsert_repository_secret("LLM_ALPHA_API_KEY", plaintext)

        self.assertEqual(len(client.calls), 1)
        path, method, payload = client.calls[0]
        self.assertEqual(
            path,
            "/repos/alice/fork/actions/secrets/LLM_ALPHA_API_KEY",
        )
        self.assertEqual(method, "PUT")
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["key_id"], "test-key-id")
        self.assertNotIn(plaintext, json.dumps(payload))
        ciphertext = base64.b64decode(payload["encrypted_value"])
        self.assertEqual(
            SealedBox(private_key).decrypt(ciphertext).decode("utf-8"),
            plaintext,
        )

    def test_repository_variable_is_created_when_missing(self):
        client = RecordingVariableClient(None)

        client.upsert_repository_variable("MODEL_PROVIDERS_JSON", '{"version":1}')

        self.assertEqual(client.variable_read, "MODEL_PROVIDERS_JSON")
        self.assertEqual(
            client.calls,
            [
                (
                    "/repos/alice/fork/actions/variables",
                    "POST",
                    {
                        "name": "MODEL_PROVIDERS_JSON",
                        "value": '{"version":1}',
                    },
                )
            ],
        )

    def test_repository_variable_is_updated_when_present(self):
        client = RecordingVariableClient('{"version":1,"providers":[]}')

        client.upsert_repository_variable("MODEL_PROVIDERS_JSON", '{"version":1}')

        self.assertEqual(client.variable_read, "MODEL_PROVIDERS_JSON")
        self.assertEqual(
            client.calls,
            [
                (
                    "/repos/alice/fork/actions/variables/MODEL_PROVIDERS_JSON",
                    "PATCH",
                    {
                        "name": "MODEL_PROVIDERS_JSON",
                        "value": '{"version":1}',
                    },
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
