"""Configuration — environment only, never baked into the store."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NOTES_", env_file=".env", extra="ignore")

    space_dir: str = "/var/lib/silverbullet"
    # Who an unlabelled note belongs to. ⚠️ Not cosmetic: every note written
    # before this app recorded authorship has no author key, so this value is
    # who they all belong to. Changing it on a live space re-attributes them.
    default_author: str = "owner"
    host: str = "127.0.0.1"
    port: int = 3337

    # ---- identity from a reverse proxy (OFF by default) ----
    # This app has no accounts and never will: there are no per-note
    # permissions to express, so access is all-or-nothing, and a proxy does
    # all-or-nothing better than we could — before the request reaches Python.
    # What it CAN do is believe a proxy that has already authenticated the
    # user, and use that name as the identity that archives and pins notes.
    #
    # ⚠️ Trusting the header is only safe when nothing can reach this port
    # except the proxy. Anyone who can talk to it directly can send
    # "Remote-User: anyone". That is why this is opt-in and not a default.
    trust_proxy_auth: bool = False
    auth_user_header: str = "Remote-User"
    # The same proxies send a human-readable name alongside the identity, and
    # it is usually the one somebody wants to see: Authelia "Remote-Name",
    # Authentik "X-authentik-name", oauth2-proxy
    # "X-Forwarded-Preferred-Username". Used only as the INITIAL display name
    # for a newly-seen identity; the people file wins ever after.
    auth_name_header: str = "Remote-Name"

    # Required to bind a non-loopback address with no proxy auth configured.
    # The point is not to stop anyone — it is to make exposing an unprotected
    # notes app a thing you typed, rather than a thing you didn't notice.
    allow_public_bind: bool = False
