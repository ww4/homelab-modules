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


def _get(port, path):
    return json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5))


def _send(port, method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=5))


def test_new_edit_pause_delete_watch(tmp_path):
    s, httpd, rebuilt = _server(tmp_path)
    port = httpd.server_address[1]
    try:
        # A new watch from the form: plain fields, lines as text, requests a run.
        res = _send(port, "POST", "/api/briefs", {
            "title": "Cabinet table saw", "notes": "shop use", "queries": "table saw\ncabinet saw",
            "requirements": "Fence included", "preferences": "Hybrid or cabinet", "home": "Springfield, IL",
            "max_miles": "60", "price_min": "50", "price_max": "900", "kind": "general"})
        assert res == {"ok": True, "name": "cabinet-table-saw", "searching": True}
        assert (s.home / "requests" / "cabinet-table-saw").exists()
        b = briefs.load(s.briefs_dir / "cabinet-table-saw.toml")
        assert b.search.queries == ["table saw", "cabinet saw"] and b.requirements == ["Fence included"]
        assert b.price.max == 900 and b.location.max_miles == 60
        # Same title again never overwrites the first watch.
        assert _send(port, "POST", "/api/briefs", {"title": "Cabinet table saw", "queries": "x",
                                                   "home": "Springfield, IL"})["name"] == "cabinet-table-saw-2"
        # Edit keeps settings the form doesn't show.
        form = _get(port, "/api/briefs/winter")
        form["preferences"] += "\nNew preference"
        _send(port, "POST", "/api/briefs", form)
        w = briefs.load(s.briefs_dir / "winter.toml")
        assert w.preferences[-1] == "New preference" and w.search.price_bands == 2 and w.kind == "vehicle"
        # Pause, list, delete.
        assert _send(port, "POST", "/api/briefs/winter/pause", {"paused": True})["paused"] is True
        listing = {x["name"]: x for x in _get(port, "/api/briefs")}
        assert listing["winter"]["paused"] is True and "cabinet-table-saw" in listing
        assert _send(port, "DELETE", "/api/briefs/cabinet-table-saw-2")["ok"] is True
        assert not (s.briefs_dir / "cabinet-table-saw-2.toml").exists()
        assert list((s.briefs_dir / ".deleted").glob("cabinet-table-saw-2-*.toml"))
        # Defaults for a new watch come from the last one saved.
        assert _get(port, "/api/briefs/defaults")["home"]
    finally:
        httpd.shutdown()


def test_bad_watch_is_a_readable_400(tmp_path):
    s, httpd, _ = _server(tmp_path)
    port = httpd.server_address[1]
    try:
        for body, needle in (({"title": "", "queries": "x", "home": "A, B"}, "title"),
                             ({"title": "Saw", "queries": "", "home": "A, B"}, "query"),
                             ({"title": "Saw", "queries": "x", "home": ""}, "home")):
            try:
                _send(port, "POST", "/api/briefs", body)
                raise AssertionError(f"accepted {body}")
            except urllib.error.HTTPError as e:
                assert e.code == 400 and needle in json.load(e)["error"].lower()
    finally:
        httpd.shutdown()


def test_min_range_round_trips_through_the_form():
    from market_agent.brief import from_form, to_form

    form = {"title": "Commuter EV", "kind": "vehicle", "home": "Springfield, IL", "max_miles": "60",
            "queries": "leaf\nbolt", "vehicle": {"drivetrain": [], "min_range": "150"}}
    b = from_form(form)
    assert b.vehicle.min_range == 150
    assert to_form(b)["vehicle"]["min_range"] == 150
