from types import SimpleNamespace

import pytest
import requests
from curl_cffi.requests import Headers
from curl_cffi import requests as curl_requests

from scripts.local_http import BrowserHTTPSAdapter


@pytest.mark.parametrize("target,expects_cookie", [("example.test", True), ("other.test", False)])
def test_redirects_preserve_cookie_scope_and_history(monkeypatch, target, expects_cookie):
    calls = []

    def request(method, url, **kwargs):
        calls.append((url, kwargs))
        if len(calls) == 1:
            return SimpleNamespace(status_code=302, content=b"", headers=Headers([
                ("Location", f"https://{target}/target"),
                ("Set-Cookie", "session=test-only; Secure; Path=/"),
            ]))
        return SimpleNamespace(status_code=200, content=b"ok", headers=Headers({"Content-Type": "text/plain"}))

    monkeypatch.setattr(curl_requests, "Session", lambda **kwargs: SimpleNamespace(request=request, close=lambda: None))
    with requests.Session() as session:
        session.mount("https://", BrowserHTTPSAdapter())
        response = session.get("https://example.test/start", timeout=10)
        assert response.text == "ok"
        assert response.history[0].status_code == 302
        assert ("Cookie" in calls[1][1]["headers"]) is expects_cookie
        assert all(call[1]["verify"] is True for call in calls)
        assert all(call[1]["allow_redirects"] is False for call in calls)


def test_transport_rejects_accidental_media_buffering():
    with pytest.raises(ValueError, match="媒体使用独立取流管道"):
        BrowserHTTPSAdapter().send(requests.Request("GET", "https://example.test/video").prepare(), stream=True)
