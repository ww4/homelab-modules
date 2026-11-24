"""Runtime settings, read from the environment (see .env.example)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

JudgeBackend = Literal["cli", "api", "none"]


class Settings(BaseModel):
    home: Path
    judge: JudgeBackend = "cli"
    model: str = "claude-opus-5-5"
    claude_bin: str = "claude"
    judge_concurrency: int = 3
    proxy: str | None = None
    osrm_url: str | None = None
    fb_interval: float = 1.5

    @property
    def briefs_dir(self) -> Path:
        return self.home / "briefs"

    @property
    def db_path(self) -> Path:
        return self.home / "market-agent.db"

    @property
    def photos_dir(self) -> Path:
        return self.home / "photos"

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv(Path.cwd() / ".env")
        env = os.environ
        home = env.get("MARKET_AGENT_HOME") or str(Path.home() / ".local/share/market-agent")
        judge = env.get("MARKET_AGENT_JUDGE") or "cli"
        if judge not in ("cli", "api", "none"):
            raise ValueError(f"MARKET_AGENT_JUDGE must be cli, api or none, not {judge!r}")
        return cls(
            home=Path(home).expanduser(),
            judge=judge,  # type: ignore[arg-type]
            model=env.get("MARKET_AGENT_MODEL") or "claude-opus-5-5",
            claude_bin=env.get("MARKET_AGENT_CLAUDE_BIN") or "claude",
            judge_concurrency=int(env.get("MARKET_AGENT_JUDGE_CONCURRENCY") or 3),
            proxy=env.get("MARKET_AGENT_PROXY") or None,
            osrm_url=env.get("MARKET_AGENT_OSRM_URL") or None,
            fb_interval=float(env.get("MARKET_AGENT_FB_INTERVAL") or 1.5),
        )

    def ensure_dirs(self) -> None:
        for d in (self.home, self.briefs_dir, self.photos_dir):
            d.mkdir(parents=True, exist_ok=True)


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader: KEY=VALUE lines; real environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)
