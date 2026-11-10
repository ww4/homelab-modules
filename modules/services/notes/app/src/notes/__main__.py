"""Entry point: ``notes`` runs the app under uvicorn."""

from __future__ import annotations

import ipaddress
import logging
import sys

import uvicorn

from .app import create_app
from .config import Settings

log = logging.getLogger("notes")


def is_loopback(host: str) -> bool:
    """True for a host that only this machine can reach."""
    h = (host or "").strip().lower()
    if h in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False        # a hostname we cannot judge: treat it as public


def check_exposure(cfg: Settings) -> str | None:
    """The reason this configuration must not start, or None.

    There is no authentication in this app. On loopback that is fine — it is
    the normal shape, with a reverse proxy in front doing whatever gating the
    deployment needs. Binding a public interface with nothing in front is a
    different thing entirely, and it is almost always an accident: someone
    reached for 0.0.0.0 to make Docker or a LAN test work.

    So refuse, and name the three ways out. The goal is not to stop anybody —
    ``allow_public_bind`` is right there — it is that exposing an unprotected
    notes app should be something you typed on purpose.
    """
    if is_loopback(cfg.host) or cfg.trust_proxy_auth or cfg.allow_public_bind:
        return None
    return (
        f"refusing to listen on {cfg.host}:{cfg.port} — this app has NO "
        "authentication, so anything that can reach that address can read and "
        "delete every note.\n"
        "  Pick one:\n"
        "   * NOTES_HOST=127.0.0.1 and put a reverse proxy in front (the normal setup);\n"
        "   * NOTES_TRUST_PROXY_AUTH=1 if a proxy already authenticates users AND\n"
        "     nothing can reach this port except that proxy;\n"
        "   * NOTES_ALLOW_PUBLIC_BIND=1 to say you meant it.\n"
        "  (In a container, binding 0.0.0.0 inside the container is fine — publish\n"
        "   the port to 127.0.0.1 on the host and set NOTES_ALLOW_PUBLIC_BIND=1.)"
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = Settings()
    problem = check_exposure(cfg)
    if problem:
        log.error("%s", problem)
        sys.exit(2)
    if not is_loopback(cfg.host) and not cfg.trust_proxy_auth:
        log.warning(
            "listening on %s with no authentication (NOTES_ALLOW_PUBLIC_BIND is set) — "
            "every note is readable and deletable by anything that can reach this port",
            cfg.host,
        )
    if cfg.trust_proxy_auth:
        log.info(
            "trusting %s from the proxy as the user identity — make sure nothing "
            "can reach this port except that proxy", cfg.auth_user_header,
        )
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, access_log=False)


if __name__ == "__main__":
    main()
