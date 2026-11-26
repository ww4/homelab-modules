"""The device registry, and the calls the page makes through it."""

from __future__ import annotations

import io
import json
import os
import pathlib
import unittest
import urllib.error
from unittest import mock

from netradio import devices

NIX = pathlib.Path(os.environ.get("NETRADIO_NIX", "")) if os.environ.get("NETRADIO_NIX") else None

W = {"id": "wallace", "name": "Wallace", "endpoint": "http://wallace:8013",
     "capabilities": ["play", "stop", "volume", "mute", "stations", "bed", "outputs"],
     "statePath": "state"}
CARD = {"id": "card", "name": "Kitchen", "endpoint": "http://127.0.0.1:8013",
        "capabilities": ["play", "stop", "volume", "mute"], "statePath": "state"}
BRIDGE = {"id": "room", "name": "Receiver", "endpoint": "http://127.0.0.1:8791",
          "capabilities": ["play", "stop", "volume", "mute"], "statePath": "status"}


class Sent:
    """Captures the request a call would have made."""

    def __init__(self, status=200, payload=None, raise_http=None, raise_url=None):
        self.status, self.payload = status, payload if payload is not None else {}
        self.raise_http, self.raise_url = raise_http, raise_url
        self.req = None

    def __call__(self, req, timeout=None):
        self.req = req
        self.timeout = timeout
        if self.raise_url:
            raise urllib.error.URLError(self.raise_url)
        if self.raise_http:
            code, body = self.raise_http
            raise urllib.error.HTTPError(req.full_url, code, "nope", {},
                                         io.BytesIO(json.dumps(body).encode()))
        return self

    # context-manager duck typing for urlopen's `with`
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return json.dumps(self.payload).encode()


def send(fake):
    return mock.patch.object(devices.urllib.request, "urlopen", fake)


class Finding(unittest.TestCase):
    def test_a_device_is_found_by_id(self):
        self.assertEqual(devices.find([CARD, W], "wallace")["name"], "Wallace")

    def test_an_unknown_id_is_a_key_error_not_a_none(self):
        """The router turns KeyError into a 404. Returning None here would make
        the caller dereference it and answer 500 for a typo."""
        with self.assertRaises(KeyError):
            devices.find([CARD], "nope")


class WhatTheBrowserIsTold(unittest.TestCase):
    def test_the_endpoint_and_the_token_path_are_not_in_it(self):
        """The page addresses a device by id, so it needs neither — and a page
        that cannot name an internal address cannot leak one."""
        out = devices.public([dict(W, tokenFile="/run/secrets/x")])[0]
        self.assertNotIn("endpoint", out)
        self.assertNotIn("tokenFile", out)
        self.assertEqual(out["id"], "wallace")
        self.assertIn("bed", out["capabilities"])

    @unittest.skipUnless(NIX, "NETRADIO_NIX not set")
    def test_the_module_hands_the_page_the_same_short_list(self):
        """site.json is served to the browser, so the list built for it must not
        carry an endpoint either. Checked against the module, because this is
        the kind of thing a later edit quietly widens."""
        src = NIX.read_text()
        start = src.index("deviceList = map")
        # To the END of that expression, not a window: a slice that runs past
        # it reads the NEXT definition's comment, which legitimately says
        # "endpoints" — the guard then fails against correct code, and a wider
        # window would pass against wrong code.
        block = src[start:src.index("}) deviceIds;", start)]
        self.assertNotIn("endpoint", block,
                         "the page's device list carries the device's address")
        self.assertNotIn("tokenFile", block,
                         "the page's device list carries the path of a secret")

    @unittest.skipUnless(NIX, "NETRADIO_NIX not set")
    def test_the_registry_the_forwarder_reads_does_carry_them(self):
        """The other half, and the one that fails silently if it regresses: drop
        `tokenFile` here and every call to a token-protected device 401s, with
        nothing in the module looking wrong."""
        src = NIX.read_text()
        start = src.index("devicesJson = pkgs.writeText")
        block = src[start:src.index("));", start)]
        self.assertIn("endpoint", block, "the forwarder is given no address to call")
        self.assertIn("tokenFile", block, "the forwarder is given no token to send")


class ReadingTheToken(unittest.TestCase):
    def test_a_device_without_one_sends_no_header(self):
        f = Sent()
        with send(f):
            devices.state(CARD)
        self.assertNotIn("Authorization", f.req.headers)

    def test_it_is_read_from_the_file_and_stripped(self):
        with mock.patch.object(devices.Path, "read_text", lambda self: "  sekret\n"):
            f = Sent()
            with send(f):
                devices.state(dict(W, tokenFile="/run/secrets/x"))
        self.assertEqual(f.req.headers["Authorization"], "Bearer sekret")

    def test_it_is_read_per_call_so_a_rotation_needs_no_restart(self):
        """Reading at start-up would pin the old token until the unit was
        restarted, and would make a file that does not exist yet a permanent
        failure rather than a temporary one."""
        vals = iter(["one", "two"])
        with mock.patch.object(devices.Path, "read_text", lambda self: next(vals)):
            d = dict(W, tokenFile="/run/secrets/x")
            f1, f2 = Sent(), Sent()
            with send(f1):
                devices.state(d)
            with send(f2):
                devices.state(d)
        self.assertEqual(f1.req.headers["Authorization"], "Bearer one")
        self.assertEqual(f2.req.headers["Authorization"], "Bearer two")

    def test_an_unreadable_file_does_not_raise(self):
        with mock.patch.object(devices.Path, "read_text",
                               lambda self: (_ for _ in ()).throw(OSError("nope"))):
            self.assertEqual(devices.read_token(dict(W, tokenFile="/x")), "")


class TheContractAndTheExtras(unittest.TestCase):
    def test_the_four_verbs_need_no_declaring(self):
        for verb in ("play", "stop", "volume", "mute"):
            with send(Sent(payload={"playing": True})):
                status, _ = devices.call(CARD, verb, {})
            self.assertEqual(status, 200, f"{verb} was refused")

    def test_an_undeclared_extra_is_a_400_not_a_500(self):
        """The device would refuse it too. Refusing here says so without a
        round trip, and 400 is the device correctly saying no — the page should
        not have offered the control at all."""
        status, body = devices.call(CARD, "bed", {"on": True})
        self.assertEqual(status, 400)
        self.assertIn("bed", body["error"])

    def test_a_declared_extra_goes_through(self):
        with send(Sent(payload={"bed": {"on": True}})) as _:
            status, _ = devices.call(W, "bed", {"on": True})
        self.assertEqual(status, 200)

    def test_an_unknown_verb_is_refused_before_the_network(self):
        f = Sent()
        with send(f):
            status, _ = devices.call(W, "reboot", {})
        self.assertEqual(status, 400)
        self.assertIsNone(f.req, "an unknown verb was sent to the device anyway")

    def test_outputs_needs_the_capability(self):
        self.assertEqual(devices.outputs(CARD)[0], 400)
        with send(Sent(payload={"outputs": []})):
            self.assertEqual(devices.outputs(W)[0], 200)


class WhereTheCallGoes(unittest.TestCase):
    def test_the_verb_is_posted_with_json(self):
        f = Sent()
        with send(f):
            devices.call(W, "play", {"mount": "blues"})
        self.assertEqual(f.req.full_url, "http://wallace:8013/play")
        self.assertEqual(f.req.get_method(), "POST")
        self.assertEqual(json.loads(f.req.data), {"mount": "blues"})

    def test_state_uses_the_declared_path(self):
        """`state` is the contract, but a device already speaking another
        dialect declares its own rather than being rewritten: yamaha-ync
        answers `status`."""
        f = Sent()
        with send(f):
            devices.state(BRIDGE)
        self.assertEqual(f.req.full_url, "http://127.0.0.1:8791/status")
        self.assertEqual(f.req.get_method(), "GET")

    def test_a_device_with_no_endpoint_is_503_not_a_crash(self):
        status, body = devices.state(dict(W, endpoint=""))
        self.assertEqual(status, 503)
        self.assertIn("endpoint", body["error"])


class WhenItGoesWrong(unittest.TestCase):
    def test_the_devices_own_refusal_is_carried_through(self):
        """A 400 from the device means it refused for a reason it can state —
        an output that cannot mix, a mount it does not have. Flattening that to
        a 500 loses the message the page would have shown."""
        with send(Sent(raise_http=(400, {"error": "this output cannot mix"}))):
            status, body = devices.call(W, "bed", {"on": True})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "this output cannot mix")

    def test_a_401_says_what_to_check(self):
        """"unreachable" would be wrong and send somebody to the network. The
        device answered; it rejected the token."""
        with send(Sent(raise_http=(401, {}))):
            status, body = devices.call(W, "play", {"mount": "x"})
        self.assertEqual(status, 401)
        self.assertIn("token", body["error"])

    def test_an_unreachable_device_is_502_and_says_so(self):
        with send(Sent(raise_url="connection refused")):
            status, body = devices.state(W)
        self.assertEqual(status, 502)
        self.assertEqual(body["error"], "unreachable")

    def test_a_timeout_is_not_an_exception_either(self):
        def boom(req, timeout=None):
            raise TimeoutError("timed out")
        with mock.patch.object(devices.urllib.request, "urlopen", boom):
            status, body = devices.state(W)
        self.assertEqual(status, 502)
        self.assertEqual(body["error"], "unreachable")

    def test_a_bounded_timeout_is_always_passed(self):
        """No timeout means urlopen can hang indefinitely, and this call is on
        the path of a page request."""
        f = Sent()
        with send(f):
            devices.state(W)
        self.assertTrue(f.timeout and f.timeout > 0, "the call has no timeout")


if __name__ == "__main__":
    unittest.main()


class TheRouteTheBrowserActuallyTakes(unittest.TestCase):
    """End to end: a real admin API, a real device, real HTTP.

    The pieces existing is not the same as the route working. This stands up
    both ends and drives the exact path the page uses —
    `/api/devices/<id>/<verb>` — because that is what nginx proxies
    `/admin/api/` onto, and because the token is added in the middle where no
    unit test of either end can see it.
    """

    def setUp(self):
        import tempfile, threading, urllib.request
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from netradio import admin, config

        self.seen = []
        outer = self

        class Device(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def _answer(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                outer.seen.append(("GET", self.path, self.headers.get("Authorization")))
                if self.path == "/state":
                    return self._answer(200, {"playing": True, "mount": "blues", "volume": 40,
                                              "muted": False, "target": "hdmi",
                                              "bed": {"on": False, "level": 25, "available": True}})
                if self.path == "/outputs":
                    return self._answer(200, {"outputs": [{"id": "hdmi", "name": "HDMI"},
                                                          {"id": "line", "name": "Line out"}]})
                return self._answer(404, {"error": "no"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.seen.append(("POST", self.path, self.headers.get("Authorization"), body))
                if self.path == "/play" and not body.get("mount"):
                    return self._answer(400, {"error": "which station?"})
                return self._answer(200, {"playing": self.path == "/play", "mount": body.get("mount", "")})

        self.dev_srv = ThreadingHTTPServer(("127.0.0.1", 0), Device)
        threading.Thread(target=self.dev_srv.serve_forever, daemon=True).start()
        dev_url = f"http://127.0.0.1:{self.dev_srv.server_address[1]}"

        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        (root / "token").write_text("s3kret\n")
        registry = root / "devices.json"
        registry.write_text(json.dumps([
            {"id": "wallace", "name": "Wallace", "endpoint": dev_url,
             "capabilities": ["play", "stop", "volume", "mute", "stations", "bed", "outputs"],
             "statePath": "state", "tokenFile": str(root / "token")},
            {"id": "plain", "name": "Card", "endpoint": dev_url,
             "capabilities": ["play", "stop", "volume", "mute"], "statePath": "state",
             "tokenFile": ""},
        ]))
        cfg = config.Config(root / "config")
        self.srv = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            admin.make_handler(admin.Admin(cfg, devices_file=registry)))
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self._req = urllib.request

    def tearDown(self):
        self.srv.shutdown()
        self.dev_srv.shutdown()
        self.tmp.cleanup()

    def call(self, method, path, body=None):
        req = self._req.Request(self.url + path, method=method,
                                data=json.dumps(body).encode() if body is not None else None,
                                headers={"Content-Type": "application/json"})
        try:
            with self._req.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_the_registry_lists_the_devices_without_their_addresses(self):
        code, got = self.call("GET", "/api/devices")
        self.assertEqual(code, 200)
        self.assertEqual([d["id"] for d in got], ["wallace", "plain"])
        self.assertNotIn("endpoint", got[0])
        self.assertNotIn("tokenFile", got[0])

    def test_a_verb_reaches_the_device_with_its_token(self):
        code, st = self.call("POST", "/api/devices/wallace/play", {"mount": "blues"})
        self.assertEqual(code, 200)
        self.assertEqual(st["mount"], "blues")
        method, path, auth, body = self.seen[-1]
        self.assertEqual((method, path), ("POST", "/play"))
        self.assertEqual(auth, "Bearer s3kret", "the token never reached the device")

    def test_a_device_that_wants_no_token_is_sent_none(self):
        self.call("POST", "/api/devices/plain/stop", {})
        self.assertIsNone(self.seen[-1][2], "an unasked-for Authorization header was sent")

    def test_the_state_round_trip(self):
        code, st = self.call("GET", "/api/devices/wallace/state")
        self.assertEqual(code, 200)
        self.assertTrue(st["playing"])
        self.assertEqual(st["bed"]["level"], 25)

    def test_outputs_are_listed_for_the_picker(self):
        code, got = self.call("GET", "/api/devices/wallace/outputs")
        self.assertEqual(code, 200)
        self.assertEqual([o["id"] for o in got["outputs"]], ["hdmi", "line"])

    def test_an_extra_the_device_did_not_declare_is_refused_here(self):
        """Without a round trip, and as a 400: the page should not have offered
        the control."""
        before = len(self.seen)
        code, body = self.call("POST", "/api/devices/plain/bed", {"on": True})
        self.assertEqual(code, 400)
        self.assertEqual(len(self.seen), before, "the call went to the device anyway")

    def test_the_devices_own_400_is_carried_through_with_its_message(self):
        code, body = self.call("POST", "/api/devices/wallace/play", {})
        self.assertEqual(code, 400)
        self.assertEqual(body["error"], "which station?")

    def test_an_unknown_device_is_a_404(self):
        code, body = self.call("GET", "/api/devices/nope/state")
        self.assertEqual(code, 404)
        self.assertIn("nope", body["error"])

    def test_the_rest_of_the_api_still_answers(self):
        """The device branch runs before the lock and returns None when the
        path is not its own, so everything else has to be untouched."""
        code, _ = self.call("GET", "/api/dislikes")
        self.assertEqual(code, 200)
