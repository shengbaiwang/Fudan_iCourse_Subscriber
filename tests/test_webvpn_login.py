from unittest.mock import Mock

import pytest
import requests

from src.api import webvpn
from src.api.webvpn import SecondFactorError, WebVPNSession, WebVPNSessionError, get_vpn_url
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
@pytest.mark.parametrize("second_factor", [False, True])
def test_cas_checks_icourse_instead_of_portal_home(monkeypatch, verified, second_factor):
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
    replies = [response(idp_url, data={"data": [{"moduleCode": "userAndPwd", "authChainCode": "pwd"}]})]
    if second_factor:
        # RFC 6238 test key and timestamp, using the last six digits of its SHA1 vector.
        monkeypatch.setattr(config, "TOTP_SECRET", "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
        monkeypatch.setattr(webvpn.time, "time", lambda: 59)
        replies.append(response(idp_url, data={"code": 200, "second": True,
            "moduleCodes": ["userAndOtp"], "authChainCode": "otp", "requestNumber": "challenge"}))
    replies.extend([
        response(idp_url, data={"code": 200, "loginToken": "synthetic"}),
        response(idp_url, body=f'locationValue = "{icourse_url}/casapi/?ticket=synthetic"'),
    ])
    post = Mock(side_effect=replies)
    monkeypatch.setattr(vpn.session, "post", post)
    monkeypatch.setattr(vpn, "_encrypt_password", lambda *_: "synthetic")

    if verified:
        assert vpn.authenticate_icourse("test-user", "test-password")
    else:
        with pytest.raises(WebVPNSessionError, match="session verification failed"):
            vpn.authenticate_icourse("test-user", "test-password")
    assert get.call_args_list[0].args[0].startswith(f"{icourse_url}/casapi/")
    assert get.call_args_list[-1].args[0] == userinfo_url
    if second_factor:
        otp_call = post.call_args_list[2]
        assert otp_call.args[0] == f"{idp_url}/idp/authn/authExecute"
        assert otp_call.kwargs["json"]["requestNumber"] == "challenge"
        assert otp_call.kwargs["json"]["authChainCode"] == "otp"
        assert otp_call.kwargs["json"]["authPara"]["otpCode"] == "287082"
        assert post.call_args.kwargs["data"] == {"loginToken": "synthetic"}


def test_missing_totp_secret_stops_password_retries(monkeypatch):
    import main

    vpn = WebVPNSession()
    post = Mock(return_value=response(config.IDP_BASE, data={"code": 200, "second": True,
        "moduleCodes": ["userAndOtp"], "authChainCode": "otp", "requestNumber": "challenge"}))
    monkeypatch.setattr(vpn.session, "post", post)
    monkeypatch.setattr(config, "TOTP_SECRET", "")
    monkeypatch.setattr(vpn, "login", lambda: vpn._auth_execute("user", "encrypted", "lck", "entity", "pwd", "type"))
    factory = Mock(return_value=vpn)
    monkeypatch.setattr(main, "WebVPNSession", factory)

    with pytest.raises(SecondFactorError, match="UIS_TOTP_SECRET"):
        main.login_with_retry()
    factory.assert_called_once()
    post.assert_called_once()


def test_cas_returning_to_vpn_login_is_retryable(monkeypatch):
    vpn = WebVPNSession()
    monkeypatch.setattr(vpn.session, "get", Mock(return_value=response(f"{config.WEBVPN_BASE}/login")))
    post = Mock()
    monkeypatch.setattr(vpn.session, "post", post)

    with pytest.raises(WebVPNSessionError, match="VPN login page"):
        vpn.authenticate_icourse("test-user", "test-password")
    post.assert_not_called()
