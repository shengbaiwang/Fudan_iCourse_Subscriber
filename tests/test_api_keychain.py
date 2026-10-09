from types import SimpleNamespace

import pytest

from local_web.api_keychain import LocalAPIKeychain
from scripts import local_course


class MemoryBackend:
    def __init__(self):
        self.items = {}

    def write(self, service, key):
        self.items[service] = key

    def read(self, service):
        return self.items.get(service)


def provider(base='https://token-plan-cn.xiaomimimo.com/v1', name='Mimo'):
    return {'name': name, 'default_base_url': base, 'models': ['mimo-v2.6-pro'],
            'api_key_env': 'LLM_KEYCHAIN_TEST_API_KEY', 'base_url_env': 'LLM_KEYCHAIN_TEST_BASE_URL'}


def test_key_is_reusable_but_bound_to_the_selected_provider_and_address():
    store = LocalAPIKeychain(MemoryBackend())
    store.save(provider(), 'test-only-key')
    assert store.load(provider()) == 'test-only-key'
    assert store.load(provider('https://other.test/v1')) is None
    assert store.load(provider(name='other')) is None
    store.save(provider(), 'replacement-test-key')
    assert store.load(provider()) == 'replacement-test-key'


def test_keychain_does_not_accept_empty_keys():
    with pytest.raises(ValueError):
        LocalAPIKeychain(MemoryBackend()).save(provider(), '  ')


def test_local_summarizer_reads_remembered_key_without_prompting(monkeypatch, capsys):
    from src.ai import summarizer
    from src.runtime import config
    selected = provider()
    monkeypatch.setattr(config, 'MODEL_PROVIDERS', config.MODEL_PROVIDERS)
    monkeypatch.delenv(selected['api_key_env'], raising=False)
    monkeypatch.delenv(selected['base_url_env'], raising=False)
    monkeypatch.setattr(local_course, 'select_summary_provider', lambda *_: selected)
    monkeypatch.setattr(LocalAPIKeychain, 'load', lambda *_: 'synthetic-key')
    monkeypatch.setattr(local_course.getpass, 'getpass', lambda *_: pytest.fail('should not prompt'))
    monkeypatch.setattr(summarizer, 'Summarizer', lambda: SimpleNamespace(ready=True))
    monkeypatch.setenv(selected['api_key_env'], '')  # restore environment after this test
    assert local_course.build_summarizer('default', None).ready
    assert 'synthetic-key' not in capsys.readouterr().out


def test_changed_destination_is_rejected_before_loading_a_key(monkeypatch):
    selected = provider()
    monkeypatch.setattr(local_course, 'select_summary_provider', lambda *_: selected)
    monkeypatch.setenv(selected['base_url_env'], 'https://other.test/v1')
    monkeypatch.setattr(LocalAPIKeychain, 'load', lambda *_: pytest.fail('should not read a key'))
    with pytest.raises(ValueError, match='地址与本机保存配置不符'):
        local_course.build_summarizer('default', None)
