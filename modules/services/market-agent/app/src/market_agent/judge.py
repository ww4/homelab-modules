"""Claude reads each surviving candidate and scores it against the brief.

The judge gets only what we can stand behind: the brief, Facebook's fields,
the seller's text, our checks and signals, references (the facts table, NHTSA
recalls, a VIN decode) and photos. It is told not to add model-specific claims
of its own; anything it suspects but can't support becomes a seller question.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import time
from pathlib import Path
from typing import Any

from .brief import Brief
from .config import Settings
from .models import Candidate, Evaluation

log = logging.getLogger(__name__)

EVALUATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "score", "verdict", "headline", "strengths", "concerns", "unknowns",
        "seller_questions", "photo_findings", "seller_kind", "scam_risk",
    ],
    "properties": {
        "score": {"type": "number", "description": "0-10 fit for this brief"},
        "verdict": {"type": "string", "enum": ["top", "consider", "skip"]},
        "headline": {"type": "string", "description": "One sentence a friend would read first"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "concerns": {"type": "array", "items": {"type": "string"}},
        "unknowns": {"type": "array", "items": {"type": "string"},
                     "description": "Requirements the listing does not establish either way"},
        "seller_questions": {"type": "array", "items": {"type": "string"}},
        "photo_findings": {"type": "array", "items": {"type": "string"},
                           "description": "What the photos show; empty if no photos were viewed"},
        "seller_kind": {"type": "string", "enum": ["private", "dealer_or_flipper", "unclear"]},
        "scam_risk": {"type": "string", "enum": ["low", "medium", "high"]},
    },
}

SYSTEM_PROMPT = """You evaluate secondhand listings for one buyer, against the buyer's written brief.

Rules:
- Use only the facts in the packet: the brief, the listing's own fields and text, the checks, the signals, the references, and the photos. Each reference says whether it is verified.
- Do not add model- or year-specific claims (known problems, recalls, specs) that are not in the references. If you suspect something the references don't cover, put it in seller_questions, not concerns.
- Unverified references are leads: turn them into seller questions, never state them as fact.
- A check marked "unknown" is not a pass. List it in unknowns and say how to settle it (often: ask for the VIN).
- Seller claims ("no rust", "runs great") are claims. Photos can support or contradict them; say which.
- Judge photos for: rust (especially frame and rocker panels), body damage, mismatched paint, tire wear, dashboard warning lights, interior wear, and whether the photos match the description.
- Score 0-10 for fit to THIS brief, weighing the brief's preferences. verdict: top = worth contacting today; consider = worth a question first; skip = not worth the drive.
- Be plain and specific. Short sentences. No hype."""


def packet(brief: Brief, c: Candidate) -> dict[str, Any]:
    d = c.detail
    return {
        "brief": {
            "title": brief.title,
            "notes": brief.notes.strip(),
            "preferences": brief.preferences,
            "price_limits": brief.price.model_dump(),
            "home": brief.location.home,
            "destinations": [p.model_dump() for p in brief.location.destinations],
        },
        "listing": {
            "url": c.listing.url,
            "title": (d.title if d and d.title else c.listing.title),
            "price": c.listing.price_text,
            "location": (d.location_text if d and d.location_text else c.listing.location_text),
            "listed_days_ago": int((time.time() - d.created_at) / 86400) if d and d.created_at else None,
            "description": d.description if d else "",
            "facebook_vehicle_fields": d.vehicle if d else {},
        },
        "facts": c.facts,
        "checks": [ch.model_dump() for ch in c.checks],
        "signals": [s.model_dump() for s in c.signals],
        "references": c.references,
    }


def _prompt(brief: Brief, c: Candidate, photo_paths: list[Path], inline_images: bool) -> str:
    body = json.dumps(packet(brief, c), indent=1, default=str)
    if photo_paths and not inline_images:
        listing = "\n".join(f"- {p}" for p in photo_paths)
        photos = f"\n\nView every one of these listing photos with the Read tool before scoring:\n{listing}"
    elif photo_paths:
        photos = f"\n\nThe {len(photo_paths)} listing photos are attached above."
    else:
        photos = "\n\nNo photos could be downloaded; leave photo_findings empty and say so in unknowns."
    return f"Evaluate this listing.\n\n<packet>\n{body}\n</packet>{photos}"


class JudgeError(Exception):
    pass


async def judge_cli(settings: Settings, brief: Brief, c: Candidate, photos: list[Path], workdir: Path) -> Evaluation:
    args = [
        settings.claude_bin, "-p", _prompt(brief, c, photos, inline_images=False),
        "--output-format", "json",
        "--json-schema", json.dumps(EVALUATION_SCHEMA),
        "--model", settings.model,
        "--system-prompt", SYSTEM_PROMPT,
        # Keep the judge's context to the packet: no project/user settings,
        # CLAUDE.md, MCP servers, skills or saved sessions.
        "--setting-sources", "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--tools", "Read",
        "--allowedTools", "Read",
        "--add-dir", str(workdir),
    ]
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=workdir, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=600)
    except asyncio.TimeoutError as e:
        proc.kill()
        raise JudgeError("claude CLI timed out after 10 minutes") from e
    if proc.returncode != 0:
        # The CLI reports most failures as JSON on stdout, not on stderr.
        detail = (err.decode(errors="replace").strip() or out.decode(errors="replace").strip())[-600:]
        raise JudgeError(f"claude CLI exited {proc.returncode}: {detail or 'no output'}")
    try:
        result = json.loads(out)
    except ValueError as e:
        raise JudgeError(f"claude CLI printed non-JSON: {out[:300]!r}") from e
    if result.get("is_error"):
        raise JudgeError(f"claude CLI reported an error: {str(result.get('result'))[:300]}")
    structured = result.get("structured_output")
    if not isinstance(structured, dict):
        raise JudgeError("claude CLI returned no structured_output")
    if result.get("permission_denials"):
        log.warning("judge was denied tools: %s", result["permission_denials"])
    return Evaluation.model_validate(structured)


async def judge_api(settings: Settings, brief: Brief, c: Candidate, photos: list[Path]) -> Evaluation:
    try:
        import anthropic
    except ImportError as e:
        raise JudgeError("MARKET_AGENT_JUDGE=api needs the anthropic package (pip install 'market-agent[api]')") from e
    content: list[dict[str, Any]] = []
    for p in photos:
        media = mimetypes.guess_type(p.name)[0] or "image/jpeg"
        content.append({"type": "image", "source": {"type": "base64", "media_type": media,
                                                     "data": base64.standard_b64encode(p.read_bytes()).decode()}})
    content.append({"type": "text", "text": _prompt(brief, c, photos, inline_images=True)})
    client = anthropic.AsyncAnthropic()
    response = await client.beta.messages.create(
        model=settings.model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": EVALUATION_SCHEMA}},
        # Route a policy decline to a fallback model instead of failing the listing.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise JudgeError(f"model declined: {response.stop_details}")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise JudgeError(f"no text in response (stop_reason={response.stop_reason})")
    return Evaluation.model_validate_json(text)


async def evaluate(settings: Settings, brief: Brief, c: Candidate, photos: list[Path], workdir: Path) -> Evaluation:
    if settings.judge == "api":
        return await judge_api(settings, brief, c, photos)
    return await judge_cli(settings, brief, c, photos, workdir)
