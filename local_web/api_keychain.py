"""Provider-bound API credentials in macOS Keychain, never in subprocess argv."""
from __future__ import annotations

import ctypes
from contextlib import contextmanager
import hashlib
import sys

from src.runtime.model_config import normalize_base_url


class _NativeKeychain:
    def __init__(self):
        self.cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        self.security = ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
        pointer = ctypes.c_void_p
        signatures = {
            'CFDictionaryCreateMutable': (pointer, [pointer, ctypes.c_long, pointer, pointer]),
            'CFDictionarySetValue': (None, [pointer, pointer, pointer]),
            'CFStringCreateWithCString': (pointer, [pointer, ctypes.c_char_p, ctypes.c_uint32]),
            'CFDataCreate': (pointer, [pointer, ctypes.c_char_p, ctypes.c_long]),
            'CFDataGetLength': (ctypes.c_long, [pointer]),
            'CFDataGetBytePtr': (pointer, [pointer]),
            'CFRelease': (None, [pointer]),
        }
        for name, (restype, argtypes) in signatures.items():
            function = getattr(self.cf, name)
            function.restype, function.argtypes = restype, argtypes
        for name in ('SecItemCopyMatching', 'SecItemAdd', 'SecItemUpdate'):
            function = getattr(self.security, name)
            function.restype = ctypes.c_int32
            function.argtypes = [pointer, pointer]

    def constant(self, name):
        library = self.cf if name == 'kCFBooleanTrue' else self.security
        return ctypes.c_void_p.in_dll(library, name).value

    @contextmanager
    def dictionary(self, values):
        # Keep every created value alive until the synchronous Security call
        # finishes. Constant keys/values are owned by the system frameworks.
        dictionary = self.cf.CFDictionaryCreateMutable(None, 0, None, None)
        owned = []
        try:
            for key, value in values.items():
                if isinstance(value, bytes):
                    reference = self.cf.CFDataCreate(None, value, len(value))
                    owned.append(reference)
                elif isinstance(value, str):
                    reference = self.cf.CFStringCreateWithCString(None, value.encode(), 0x08000100)
                    owned.append(reference)
                else:
                    reference = value
                self.cf.CFDictionarySetValue(dictionary, self.constant(key), reference)
            yield dictionary
        finally:
            self.cf.CFRelease(dictionary)
            for reference in owned:
                self.cf.CFRelease(reference)

    def query(self, service):
        return {'kSecClass': self.constant('kSecClassGenericPassword'),
                'kSecAttrService': service, 'kSecAttrAccount': 'api-key'}

    def read(self, service):
        values = {**self.query(service), 'kSecReturnData': self.constant('kCFBooleanTrue'),
                  'kSecMatchLimit': self.constant('kSecMatchLimitOne')}
        result = ctypes.c_void_p()
        with self.dictionary(values) as query:
            status = self.security.SecItemCopyMatching(query, ctypes.byref(result))
        if status == -25300:  # errSecItemNotFound
            return None
        if status != 0:
            raise OSError(f'无法读取 API 钥匙串（系统状态 {status}）')
        try:
            return ctypes.string_at(self.cf.CFDataGetBytePtr(result),
                                    self.cf.CFDataGetLength(result)).decode()
        finally:
            self.cf.CFRelease(result)

    def write(self, service, key):
        with self.dictionary({**self.query(service), 'kSecValueData': key.encode()}) as item:
            status = self.security.SecItemAdd(item, None)
        if status == -25299:  # errSecDuplicateItem
            with self.dictionary(self.query(service)) as query:
                with self.dictionary({'kSecValueData': key.encode()}) as update:
                    status = self.security.SecItemUpdate(query, update)
        if status != 0:
            raise OSError(f'无法保存 API 钥匙串（系统状态 {status}）')


class LocalAPIKeychain:
    def __init__(self, backend=None):
        self.backend = backend

    @staticmethod
    def service(provider):
        base = normalize_base_url(provider['default_base_url'], provider['name'])
        identity = hashlib.sha256(f"{provider['name']}\n{base}".encode()).hexdigest()
        return f'com.fudan-icourse-subscriber.local-api:{identity}'

    def _backend(self):
        if self.backend is None:
            if sys.platform != 'darwin':
                raise OSError('当前系统不支持 macOS API 钥匙串')
            self.backend = _NativeKeychain()
        return self.backend

    def load(self, provider):
        if sys.platform != 'darwin' and self.backend is None:
            return None
        return self._backend().read(self.service(provider))

    def save(self, provider, key):
        if not isinstance(key, str) or not key.strip():
            raise ValueError('API Key 不能为空')
        self._backend().write(self.service(provider), key.strip())
