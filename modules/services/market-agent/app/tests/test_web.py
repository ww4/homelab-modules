import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from market_agent import brief as briefs
from market_agent.config import Settings
from market_agent.store import Store
from market_agent.web import _Rerender, make_handler


def _server(tmp_path: Path):
    s = Settings(home=tmp_path, judge="none")
    s.ensure_dirs()
    briefs.new_brief("winter", s.briefs_dir, "winter-commuter")
    rebuilt = []
    rr = _Rerender(lambda: rebuilt.append(1), delay=0.01)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(s, rr))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return s, httpd, rebuilt


def _post(port, body):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/favorites", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return urllib.request.urlopen(req, timeout=5)


def test_star_round_trip_and_rerender(tmp_path):
    s, httpd, rebuilt = _server(tmp_path)
    port = httpd.server_address[1]
    try:
        assert _post(port, {"brief": "winter", "id": "2202177927361916", "starred": True}).status == 200
        got = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/favorites", timeout=5))
        assert got == {"winter": ["2202177927361916"]}
        st = Store(s.db_path)
        assert st.favorites("winter") == {"2202177927361916"}
        st.close()
        _post(port, {"brief": "winter", "id": "2202177927361916", "starred": False})
        assert json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/favorites", timeout=5)) == {}
        import time
        time.sleep(0.2)
        assert rebuilt  # the page is rebuilt after a change
    finally:
        httpd.shutdown()


def test_rejects_unknown_brief_and_bad_ids(tmp_path):
    s, httpd, _ = _server(tmp_path)
    port = httpd.server_address[1]
    try:
        for body in ({"brief": "nope", "id": "1234567", "starred": True},
                     {"brief": "winter", "id": "../../etc", "starred": True},
                     {"brief": "winter"}):
            try:
                _post(port, body)
                raise AssertionError(f"accepted {body}")
            except urllib.error.HTTPError as e:
                assert e.code == 400
    finally:
        httpd.shutdown()


def test_serve_refuses_a_public_address(tmp_path):
    import pytest
    from market_agent.web import serve

    with pytest.raises(ValueError):
        serve(Settings(home=tmp_path), 0, lambda: None, host="0.0.0.0")
