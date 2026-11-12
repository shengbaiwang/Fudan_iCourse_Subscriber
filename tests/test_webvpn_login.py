from unittest.mock import Mock

import pytest
import requests

from src.api.webvpn import WebVPNSession, WebVPNSessionError, get_vpn_url
from src.runtime import config


def response(url, status=200, *, body="", data=None):
    result = requests.Response()
    result.url = url
    result.status_code = status
    result._content = body.encode()
    if data is not None:
        result.json = Mock(return_value=data)
    return result


@pytest.mark.parametrize("cookie_received", [False, True])
def test_ticket_timeout_does_not_redeem_same_ticket_twice(monkeypatch, cookie_received):
    vpn = WebVPNSession()
    ticket = f"{config.WEBVPN_BASE}/login?ticket=synthetic"

    def timed_out(*args, **kwargs):
        if cookie_received:
            vpn.session.cookies.set("wengine_vpn_ticket", "synthetic")
        raise requests.ReadTimeout()

    get = Mock(side_effect=timed_out)
    monkeypatch.setattr(vpn.session, "get", get)
    if cookie_received:
        vpn._establish_session(ticket)
    else:
        with pytest.raises(WebVPNSessionError, match="ticket redemption timed out"):
            vpn._establish_session(ticket)
    assert [call.args[0] for call in get.call_args_list] == [ticket]


@pytest.mark.parametrize("verified", [True, False])
def test_cas_checks_icourse_instead_of_portal_home(monkeypatch, verified):
    vpn = WebVPNSession()
    idp_url = get_vpn_url(config.IDP_BASE)
    icourse_url = get_vpn_url(config.ICOURSE_BASE)
    userinfo_url = f"{icourse_url}/userapi/v1/infosimple"
    get = Mock(side_effect=[
        response(f"{idp_url}/ac/?lck=synthetic"),
        response(f"{idp_url}/idp/authn/getJsPublicKey", data={"data": "public-key"}),
        response(icourse_url),
        response(userinfo_url, data={"code": 0}) if verified
        else response(userinfo_url, body="<html>Login</html>"),
    ])
    monkeypatch.setattr(vpn.session, "get", get)
    monkeypatch.setattr(vpn.session, "post", Mock(side_effect=[
        response(idp_url, data={"data": [{"moduleCode": "userAndPwd", "authChainCode": "pwd"}]}),
        response(idp_url, data={"code": 200, "loginToken": "synthetic"}),
        response(idp_url, body=f'locationValue = "{icourse_url}/casapi/?ticket=synthetic"'),
    ]))
    monkeypatch.setattr(vpn, "_encrypt_password", lambda *_: "synthetic")

    if verified:
        assert vpn.authenticate_icourse("test-user", "test-password")
    else:
        with pytest.raises(WebVPNSessionError, match="session verification failed"):
            vpn.authenticate_icourse("test-user", "test-password")
    assert get.call_args_list[0].args[0].startswith(f"{icourse_url}/casapi/")
    assert get.call_args_list[-1].args[0] == userinfo_url


def test_cas_returning_to_vpn_login_is_retryable(monkeypatch):
    vpn = WebVPNSession()
    monkeypatch.setattr(vpn.session, "get", Mock(return_value=response(f"{config.WEBVPN_BASE}/login")))
    post = Mock()
    monkeypatch.setattr(vpn.session, "post", post)

    with pytest.raises(WebVPNSessionError, match="VPN login page"):
        vpn.authenticate_icourse("test-user", "test-password")
    post.assert_not_called()
