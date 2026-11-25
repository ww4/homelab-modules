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

# Bump when SYSTEM_PROMPT or the schema changes meaning: stored evaluations
# made under an older version are re-judged on the next run.
JUDGE_VERSION = 4

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

SCREEN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["score", "headline", "standout", "seller_kind", "scam_risk"],
    "properties": {
        "score": {"type": "number", "description": "0-10 fit for this brief"},
        "headline": {"type": "string", "description": "One short sentence"},
        "standout": {"type": "array", "items": {"type": "string"}, "maxItems": 3,
                     "description": "What sets THIS car apart from other examples of the same model, good or bad"},
        "seller_kind": {"type": "string", "enum": ["private", "dealer_or_flipper", "unclear"]},
        "scam_risk": {"type": "string", "enum": ["low", "medium", "high"]},
    },
}

SYSTEM_PROMPT = """You evaluate secondhand listings for one buyer, against the buyer's written brief.

The buyer is choosing between many similar listings, often several of the same model. What helps is what sets THIS listing apart: its condition, mileage, records, seller, price for what it is, what the photos show. Model-wide facts (the recalls and bulletins in the references) are already shown to the buyer as tags on every listing of that model. Do not restate them. Mention one only when this listing has evidence about it: records of the repair, symptoms in the text or photos, a VIN that settles it.

Rules:
- Use only the facts in the packet: the brief, the listing's own fields and text, the checks, the signals, the references, and the photos. Each reference says whether it is verified.
- Do not add model- or year-specific claims (known problems, recalls, specs) that are not in the references. If you suspect something the references don't cover, put it in seller_questions, not concerns.
- Unverified references are leads: turn them into seller questions, never state them as fact.
- A check marked "unknown" is not a pass. List it in unknowns and say how to settle it (often: ask for the VIN).
- Seller claims ("no rust", "runs great") are claims. Photos can support or contradict them; say which.
- Judge photos for: rust (especially frame and rocker panels), body damage, mismatched paint, tire wear, dashboard warning lights, interior wear, and whether the photos match the description.
- The brief's requirements are hard: a listing that clearly fails one scores 2 or below; one the listing does not settle goes in unknowns with the question that would settle it.
- Score 0-10 for fit to THIS brief, weighing the brief's preferences. Anchors: 8+ = would contact today, little doubt; 7 = contact today; 5-6 = worth one question before driving; 3-4 = poor fit; 0-2 = do not pursue. Set verdict to match (7+ top, 5-6.9 consider, below 5 skip); the page derives it from the score anyway.
- A car whose drivetrain the listing does not establish cannot score above 6 on an AWD/4WD brief; say what would settle it.
- Facts with a `*_evidence` saying every US version of that model year was built one way (from the EPA configuration list) are settled: do not ask the seller about them. `drivetrain_options` and `transmission_spec` list what that model year was actually sold with; ask only about what those options leave open (e.g. FWD or AWD), and name the options.
- Electric cars and plug-ins: `rated_range_miles` is the EPA range when NEW. A used battery holds less, so weigh what the listing says (battery health %, capacity bars, a range the seller actually gets, a battery replacement) against it, and ask for battery health when the listing doesn't say.
- Scam risk medium or high, financing / down-payment / buy-here-pay-here ads, and text copied across listings are near-disqualifying for a private buyer: score them 3 or below.
- Be plain and specific. Short sentences. No hype. At most four items per list; skip a list rather than pad it."""

SCREEN_PROMPT = SYSTEM_PROMPT + """

This is a QUICK SCREEN, not the full evaluation: score it, write one short headline, and give at most three standout points. Only listings that screen well get the full read."""


def packet(brief: Brief, c: Candidate) -> dict[str, Any]:
    d = c.detail
    return {
        "brief": {
            "title": brief.title,
            "notes": brief.notes.strip(),
            "requirements": brief.requirements,
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


def _stage(depth: str) -> tuple[dict[str, Any], str, str]:
    """(schema, system prompt, effort) for a judging depth."""
    if depth == "screen":
        return SCREEN_SCHEMA, SCREEN_PROMPT, "low"
    return EVALUATION_SCHEMA, SYSTEM_PROMPT, "medium"


def _to_evaluation(raw: dict[str, Any], depth: str) -> Evaluation:
    raw = dict(raw, depth=depth)
    score = float(raw.get("score", 0))
    # The page derives the verdict from the score (policy.py); keep them consistent here too.
    raw["verdict"] = "top" if score >= 7 else "consider" if score >= 5 else "skip"
    return Evaluation.model_validate(raw)


async def ask_cli(settings: Settings, *, prompt: str, schema: dict[str, Any], system: str, effort: str,
                  workdir: Path) -> dict[str, Any]:
    """One structured answer from the claude CLI; photos are read from `workdir`."""
    args = [
        settings.claude_bin, "-p", prompt,
        "--output-format", "json",
        "--json-schema", json.dumps(schema),
        "--model", settings.model,
        "--effort", effort,
        "--system-prompt", system,
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
    return structured


async def ask_api(settings: Settings, *, prompt: str, schema: dict[str, Any], system: str, effort: str,
                  photos: list[Path]) -> dict[str, Any]:
    """One structured answer from the API, photos sent inline."""
    try:
        import anthropic
    except ImportError as e:
        raise JudgeError("MARKET_AGENT_JUDGE=api needs the anthropic package (pip install 'market-agent[api]')") from e
    content: list[dict[str, Any]] = []
    for p in photos:
        media = mimetypes.guess_type(p.name)[0] or "image/jpeg"
        content.append({"type": "image", "source": {"type": "base64", "media_type": media,
                                                     "data": base64.standard_b64encode(p.read_bytes()).decode()}})
    content.append({"type": "text", "text": prompt})
    client = anthropic.AsyncAnthropic()
    response = await client.beta.messages.create(
        model=settings.model,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        # Route a policy decline to a fallback model instead of failing the listing.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise JudgeError(f"model declined: {response.stop_details}")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise JudgeError(f"no text in response (stop_reason={response.stop_reason})")
    return json.loads(text)


async def ask(settings: Settings, *, prompt_cli: str, prompt_api: str, schema: dict[str, Any], system: str,
              effort: str, photos: list[Path], workdir: Path) -> dict[str, Any]:
    if settings.judge == "api":
        return await ask_api(settings, prompt=prompt_api, schema=schema, system=system, effort=effort, photos=photos)
    return await ask_cli(settings, prompt=prompt_cli, schema=schema, system=system, effort=effort, workdir=workdir)


async def judge_cli(
    settings: Settings, brief: Brief, c: Candidate, photos: list[Path], workdir: Path, depth: str = "full"
) -> Evaluation:
    schema, system, effort = _stage(depth)
    raw = await ask_cli(settings, prompt=_prompt(brief, c, photos, inline_images=False), schema=schema,
                        system=system, effort=effort, workdir=workdir)
    return _to_evaluation(raw, depth)


async def judge_api(
    settings: Settings, brief: Brief, c: Candidate, photos: list[Path], depth: str = "full"
) -> Evaluation:
    schema, system, effort = _stage(depth)
    raw = await ask_api(settings, prompt=_prompt(brief, c, photos, inline_images=True), schema=schema,
                        system=system, effort=effort, photos=photos)
    return _to_evaluation(raw, depth)


async def evaluate(
    settings: Settings, brief: Brief, c: Candidate, photos: list[Path], workdir: Path, depth: str = "full"
) -> Evaluation:
    if settings.judge == "api":
        return await judge_api(settings, brief, c, photos, depth)
    return await judge_cli(settings, brief, c, photos, workdir, depth)
