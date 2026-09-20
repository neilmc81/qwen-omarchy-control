"""One-of-N selection with Jev: the shared "which supplied candidate?" primitive.

Why this exists
---------------
Both targeting paths face the same problem: code has a bounded list of real
candidates (accessibility elements, or browser DOM refs) and a natural-language
goal ("the Save button", "the search box"), and must pick exactly one - or admit
that none fits. That is a judgement, not a string match, so it belongs to Jev
rather than to a hand-tuned heuristic.

The contract, following Cua's own `jev-use` recipe:

    candidates (id, role, name, actions)   -> from the live tree/DOM
    Jev (System One, one Choice question)  -> pick one id, or "none", + confidence
    code                                   -> act on that id, enforce the floor

The model never invents an id that was not supplied, and never returns a
coordinate. Code owns whether the confidence is high enough to act, and what
"none" means. This is deliberately NOT an LLM and generates no prose.

Why the role matters
--------------------
Measured on this machine: with the goal "Search", plain name overlap chose the
*combobox* "Search with DuckDuckGo" over the "Search" button, so "click Search"
filled the field instead of submitting. A candidate's actions are therefore part
of what Jev sees, and callers pre-filter to refs that actually declare the
action they need - a readable element is not a clickable one.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import triage


class SelectionError(RuntimeError):
    """No candidate satisfied the goal, or not confidently enough to act."""


@dataclass
class Choice:
    id: str
    confidence: float
    probabilities: dict
    cost_usd: float


def ask(state: str, criteria: dict, instructions: str, cfg: dict) -> dict:
    """One Jev Choice question over a state payload. Raises TriageError.

    `criteria` maps candidate ids to a human description, plus a caller-added
    "none" option. `state` carries whatever context the judgement needs.
    """
    questions = {
        "candidate": {
            "type": "choice",
            "instructions": instructions,
            "criteria": criteria,
        },
    }
    return triage.ask(state, questions, cfg)


def choose(payload: dict, ids: set[str], cfg: dict,
           *, none_label: str = "none") -> Choice:
    """Read a Choice answer and enforce the policy. Raises SelectionError.

    The confidence floor is a *policy* decision, so it lives here rather than in
    the question. Below it, the honest answer is "cannot tell", which the caller
    surfaces rather than acting on a plausible-looking guess.
    """
    answers = payload.get("answers") or {}
    choice = answers.get("candidate") or {}
    chosen = str(choice.get("choice") or none_label)
    confidence = float(choice.get("confidence") or 0.0)
    floor = float(cfg.get("minConfidence", 0.60))
    if chosen == none_label or chosen not in ids:
        raise SelectionError(
            f"no supplied candidate matches the goal (model said {chosen!r})"
        )
    if confidence < floor:
        raise SelectionError(
            f"selection not confident enough ({confidence:.2f} < {floor})"
        )
    return Choice(
        id=chosen,
        confidence=confidence,
        probabilities=choice.get("probabilities") or {},
        cost_usd=float((payload.get("usage") or {}).get("cost") or 0.0),
    )


def jev_config(cfg: dict) -> dict:
    """The triage transport config, overridden with this caller's model/key.

    Selection reuses the one Jev transport the project already has (and tests),
    so there is a single place where the endpoint and key are resolved.
    """
    jev = dict(triage.load_config())
    for key in ("model", "endpoint", "apiKeyEnv"):
        if cfg.get(key):
            jev[key] = cfg[key]
    return jev


def available(cfg: dict) -> tuple[bool, str]:
    """Whether a Jev selection can run now, and why not if not."""
    try:
        triage._read_key(jev_config(cfg))
    except triage.TriageError as exc:
        return False, str(exc)
    return True, ""


__all__ = ["Choice", "SelectionError", "ask", "choose", "jev_config", "available"]
