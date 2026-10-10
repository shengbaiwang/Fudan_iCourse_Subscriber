"""Explicitly save the selected API key to this Mac's Keychain."""
from __future__ import annotations

import argparse
import getpass
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from local_web.api_keychain import LocalAPIKeychain
from scripts.local_course import select_summary_provider


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', default='default')
    parser.add_argument('--model')
    args = parser.parse_args()
    provider = select_summary_provider(args.provider, args.model)
    print(f"永久保存到 macOS 钥匙串：{provider['name']}\n地址：{provider['default_base_url']}", flush=True)
    key = getpass.getpass('API Key（隐藏输入，保存在本机钥匙串）：').strip()
    store = LocalAPIKeychain()
    store.save(provider, key)
    if store.load(provider) != key:
        raise RuntimeError('API 钥匙串读回验证失败')
    print('API Key 已永久保存并通过读回验证。以后本机课程摘要会自动读取。', flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('已取消保存。', file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f'保存失败：{type(exc).__name__}', file=sys.stderr)
        raise SystemExit(1)
