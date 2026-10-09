"""Browser-compatible HTTPS transport for the local WebVPN runner.

requests retains redirect handling and its in-memory CookieJar. curl only
changes TLS/HTTP negotiation; it neither reads nor writes browser sessions.
"""
from email.message import Message
from types import SimpleNamespace

import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict
from requests.utils import get_encoding_from_headers


class BrowserHTTPSAdapter(BaseAdapter):
    def __init__(self):
        from curl_cffi.requests import Session
        self._curl = Session(impersonate="chrome", default_headers=True, discard_cookies=True)

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        if stream:
            raise ValueError("浏览器兼容适配器只用于 API 和截图；媒体使用独立取流管道")
        from curl_cffi.requests.exceptions import RequestException
        try:
            result = self._curl.request(
                request.method, request.url, headers=dict(request.headers),
                data=request.body, allow_redirects=False,
                timeout=timeout or 30, verify=verify, cert=cert,
                proxies=proxies or None,
            )
        except RequestException as exc:
            error = requests.exceptions.ReadTimeout if exc.code == 28 else requests.exceptions.ConnectionError
            raise error(str(exc), request=request) from exc
        response = requests.Response()
        response.status_code = result.status_code
        response.headers = CaseInsensitiveDict(result.headers)
        response.url = request.url
        response.request = request
        response.encoding = get_encoding_from_headers(response.headers)
        response._content = result.content
        response._content_consumed = True
        # requests' CookieJar extracts each Set-Cookie through this interface.
        message = Message()
        for key, value in result.headers.multi_items():
            message.add_header(key, value)
        response.raw = SimpleNamespace(
            _original_response=SimpleNamespace(msg=message),
            release_conn=lambda: None, close=lambda: None,
        )
        return response

    def close(self):
        self._curl.close()
