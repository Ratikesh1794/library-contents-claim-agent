"""HTTP errors must never carry secrets (an API key in the query string was printed in a log line)."""
import pytest
import requests

from backend.core import http


def test_scrub_hides_keys_tokens_and_secrets_in_urls():
    msg = "503 Server Error for url: https://x.test/v1?q=dune&key=AIzaSECRET123&maxResults=40&token=abc&client_secret=xyz"
    out = http.scrub(msg)
    assert "AIzaSECRET123" not in out and "abc" not in out and "xyz" not in out
    assert "q=dune" in out and "maxResults=40" in out and "key=***" in out


def test_a_failed_request_raises_an_error_without_the_key(monkeypatch, tmp_path):
    class Resp:
        def raise_for_status(self): raise requests.HTTPError("503 for url: https://x.test/v1?key=TOPSECRET&q=a")
    monkeypatch.setattr(http.requests, "get", lambda *a, **k: Resp())
    monkeypatch.setattr(http, "cached", lambda ns, key, call: call())
    with pytest.raises(RuntimeError) as e:
        http.get_json("t", "https://x.test/v1", {"key": "TOPSECRET", "q": "a"})
    assert "TOPSECRET" not in str(e.value) and e.value.__cause__ is None and e.value.__suppress_context__
