"""Devices: the things that can be told to play a station.

This machine's own sound card, a bridge in front of a receiver, a second PC
with its own speakers. None of them is a subclass of anything. A device is an
HTTP endpoint answering four verbs — play, stop, volume, mute — and whatever
extras it declares; that is the entire contract, which is why adding one is a
registry entry rather than code.

The page reaches a device *through here* rather than straight through nginx,
and the reason is the token. A device on another machine wants a bearer token,
the token lives in a file outside the Nix store, and the browser cannot be
handed it. nginx could add the header from an `include`, but a missing include
file stops nginx from starting — and nginx here serves every vhost on the box,
so the failure is not confined to the radio (2026-10-12 took every vhost down
twice in one day for a smaller reason than this). Reading the file in this
process has neither problem, and it is also what will let a device be added at
runtime without an nginx reload at all.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("netradio.devices")

# play/stop/volume/mute are the contract and need no declaring. The rest are
# extras: a device that has not declared one is not asked, because the page
# should not have offered the control in the first place.
CONTRACT = ("play", "stop", "volume", "mute")
NEEDS = {"bed": "bed", "output": "outputs"}
VERBS = CONTRACT + tuple(NEEDS)

# Long enough for a device waking a stream, short enough that an unreachable
# one does not hold a page request open. The admin API answers this call
# OUTSIDE its global lock for the same reason.
TIMEOUT = 20.0


def find(devices: list[dict], dev_id: str) -> dict:
    for d in devices or []:
        if d.get("id") == dev_id:
            return d
    raise KeyError(dev_id)


def read_token(device: dict) -> str:
    """The device's bearer token, or "" when it wants none.

    Read per call rather than at start-up, so rotating the file does not need
    this service restarted — and so a file that is not there yet is not a
    permanent failure.
    """
    path = device.get("tokenFile") or ""
    if not path:
        return ""
    try:
        return Path(path).read_text().strip()
    except OSError as e:
        log.warning("device %s: cannot read %s (%s)", device.get("id"), path, e)
        return ""


def public(devices: list[dict]) -> list[dict]:
    """The registry as the page may see it: no endpoints, no token paths.

    The browser addresses a device by id through this API, so it has no use
    for either, and a page that cannot name an internal address cannot leak
    one.
    """
    keep = ("id", "name", "capabilities", "statePath", "order", "codecs")
    return [{k: d[k] for k in keep if k in d} for d in devices or []]


def _request(device: dict, path: str, method: str, body: dict | None, timeout: float):
    url = f"{(device.get('endpoint') or '').rstrip('/')}/{path.lstrip('/')}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    token = read_token(device)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def call(device: dict, verb: str, body: dict | None = None,
         timeout: float = TIMEOUT) -> tuple[int, dict]:
    """Send one verb to one device. Returns (status, payload).

    Every failure is a status and a message, never an exception: this is on the
    path of a page request, and a device being off is ordinary.
    """
    if verb not in VERBS:
        return 400, {"error": f"no such verb: {verb}"}
    need = NEEDS.get(verb)
    if need and need not in (device.get("capabilities") or []):
        return 400, {"error": f"{device.get('name') or device.get('id')} does not do {need}"}
    return _send(device, verb, "POST", body or {}, timeout)


def state(device: dict, timeout: float = TIMEOUT) -> tuple[int, dict]:
    """The device's own status, on whatever path it answers on. `state` is the
    contract; a device already speaking another dialect declares its path
    rather than being rewritten (yamaha-ync answers `status`)."""
    return _send(device, device.get("statePath") or "state", "GET", None, timeout)


def outputs(device: dict, timeout: float = TIMEOUT) -> tuple[int, dict]:
    if "outputs" not in (device.get("capabilities") or []):
        return 400, {"error": f"{device.get('name') or device.get('id')} has one output"}
    return _send(device, "outputs", "GET", None, timeout)


def _send(device: dict, path: str, method: str, body: dict | None, timeout: float):
    if not (device.get("endpoint") or ""):
        return 503, {"error": f"{device.get('id')} has no endpoint"}
    try:
        return 200, _request(device, path, method, body, timeout)
    except urllib.error.HTTPError as e:
        # The device's own answer, carried through rather than flattened to a
        # 500: a 400 from a device means it correctly refused, and the page
        # shows that message. A 401 means the token is wrong, which is worth
        # saying plainly rather than reporting as "unreachable".
        try:
            payload = json.loads(e.read() or b"{}")
        except (ValueError, OSError):
            payload = {}
        if not payload.get("error"):
            payload["error"] = ("not authorised — check the device's token"
                                if e.code in (401, 403) else f"{e.code} {e.reason}")
        return e.code, payload
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 502, {"error": "unreachable", "detail": str(getattr(e, "reason", e))}
    except ValueError as e:
        return 502, {"error": f"the device answered something that is not JSON: {e}"}
