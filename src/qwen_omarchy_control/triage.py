"""Dormant pre-dispatch triage for the agent-delegation seam.

Why this exists
---------------
The desktop controller is safe by construction: allowlisted argv, no shell, a
fixed confirmation set, unknown tools fail closed. The *agent handoff* is not.
`launch_agent` submits a spoken prompt to a coding agent (Hermes/Codex/OpenCode)
whose approval policy may be `off`, so a misheard or hostile sentence becomes a
real coding task with no gate. That is the one place on the voice path where
destructive capability is reachable, and it is the seam this module guards.

What it is
----------
One TypeSafe/System One (Jev) call: a small set of atomic, typed questions over
the request text, combined by *code* into a verdict. Jev supplies the judgment;
this module owns the policy. It is deliberately NOT an LLM and does not generate
prose.

It is a guard, not a filter
---------------------------
The earlier attempt to put a decision model in front of the voice pipeline
false-blocked routine actions, so this ships **off** and adds a log-only mode to
measure the false-block rate before anything is enforced:

  off       no network, no behaviour change (default)
  log       evaluate and record the verdict, but always allow
  enforce   refuse gibberish, hold risky/unclear requests for confirmation

It hangs off the side of the voice loop. It never sits inside the realtime
turn/commit path, so it cannot delay or drop speech.

Failure policy
--------------
An open microphone is untrusted. In `enforce` mode any failure (no key, network,
timeout, malformed answer) escalates to confirmation rather than allowing. In
`log` mode failures are recorded and ignored, because nothing is being enforced.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# --- endpoints -------------------------------------------------------------

DEFAULT_ENDPOINT = os.environ.get(
    "QWEN_TRIAGE_ENDPOINT", "https://openrouter.ai/api/alpha/decisions"
)
DEFAULT_MODEL = os.environ.get("QWEN_TRIAGE_MODEL", "typesafe/jev-1.13")

MODES = ("off", "log", "enforce")

# --- paths -----------------------------------------------------------------

CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
STATE_HOME = Path(
    os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
)
# Both overridable so tests never read or write the live config/audit log.
CONFIG_FILE = Path(
    os.environ.get("QWEN_TRIAGE_CONFIG", CONFIG_HOME / "qwen-omarchy-control" / "triage.json")
)
LOG_FILE = Path(
    os.environ.get("QWEN_TRIAGE_LOG", STATE_HOME / "qwen-omarchy-control" / "triage.jsonl")
)

# --- defaults (policy constants live here, not in the prompts) -------------

DEFAULTS = {
    "mode": "off",
    # Jev route confidence below this -> hold for confirmation.
    "minConfidence": 0.85,
    # A destructive reading at/above this -> hold for confirmation.
    "destructiveConfirm": 0.50,
    # A "clear request" reading below this -> treat as mis-heard, refuse.
    "clearFloor": 0.50,
    "timeoutMs": 8000,
    "model": DEFAULT_MODEL,
    "endpoint": DEFAULT_ENDPOINT,
    # Env var holding the key; falls back to keyFile, then ~/.hermes/.env.
    "apiKeyEnv": "OPENROUTER_API_KEY",
    "keyFile": "",
    "logPrompt": True,
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        data = json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        return cfg
    if not isinstance(data, dict):
        return cfg
    for key in DEFAULTS:
        if data.get(key) is not None:
            cfg[key] = data[key]
    if cfg.get("mode") not in MODES:
        cfg["mode"] = "off"
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(CONFIG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(cfg, stream, indent=2, ensure_ascii=False)
        stream.write("\n")


# --- secret masking (audit log must not become a credential store) ---------

_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_.-]{4})[A-Za-z0-9_.-]+"
    r"|(Bearer\s+)[A-Za-z0-9._-]{4,}"
    r"|((?:API_?KEY|TOKEN|SECRET|PASSWORD)\s*[=:]\s*)\S+",
    re.IGNORECASE,
)


def mask_secrets(text: str) -> str:
    def repl(match: re.Match) -> str:
        if match.group(1):
            return match.group(1) + "***"
        if match.group(2):
            return match.group(2) + "***"
        if match.group(3):
            return match.group(3) + "***"
        return "***"

    return _SECRET_RE.sub(repl, text or "")


# --- errors ----------------------------------------------------------------


class TriageError(RuntimeError):
    """A triage evaluation could not be completed."""


@dataclass
class Verdict:
    verdict: str          # allow | confirm | refuse
    route: str            # agent_task | desktop_command | question | garbage | unknown
    confidence: float
    destructive: float
    clear: float
    reason: str
    enforced: bool        # True when this verdict actually changed behaviour
    used_fallback: bool   # True when Jev failed and policy escalated
    cost_usd: float = 0.0
    latency_s: float = 0.0
    probabilities: dict[str, float] = field(default_factory=dict)


# --- Jev call --------------------------------------------------------------


def _read_key(cfg: dict) -> str:
    env_name = str(cfg.get("apiKeyEnv") or "OPENROUTER_API_KEY")
    value = (os.environ.get(env_name) or "").strip()
    if value:
        return value
    key_file = str(cfg.get("keyFile") or "").strip()
    candidates = [Path(key_file)] if key_file else []
    # The user already keeps an OpenRouter key here; reuse it read-only rather
    # than asking for a second copy. Never printed, never logged.
    candidates.append(Path.home() / ".hermes" / ".env")
    for path in candidates:
        try:
            for line in path.read_text().splitlines():
                if line.startswith(env_name + "="):
                    candidate = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if candidate:
                        return candidate
        except OSError:
            continue
    raise TriageError(
        f"no API key: set {env_name} in the environment or point keyFile at a file "
        "containing it"
    )


def _questions() -> dict:
    """Atomic, independent questions evaluated together in one call.

    Kept narrow per TypeSafe guidance: one coherent judgment each, combined by
    the policy below rather than encoded as a single multi-factor question.
    """
    return {
        "route": {
            "type": "choice",
            "instructions": (
                "The user spoke to a desktop voice assistant; the assistant is "
                "about to open a coding agent and submit this request to it. What "
                "is this request really?"
            ),
            "criteria": {
                "agent_task": (
                    "A request to write, build, change, fix, debug or run code or "
                    "commands in a project - genuine work for a coding agent."
                ),
                "desktop_command": (
                    "A desktop/GUI action the assistant itself should perform "
                    "(open an app, move a window, change volume), not a coding task."
                ),
                "question": (
                    "A request for information or an explanation; wants an answer, "
                    "not an action."
                ),
                "garbage": (
                    "Not a coherent request: background noise, a mis-transcription, "
                    "a fragment, or chatter."
                ),
            },
        },
        "clear": {
            "type": "noul",
            "instructions": (
                "Is this a clear, deliberate request that the user actually made, "
                "as opposed to a fragment or something mis-heard?"
            ),
        },
        "destructive": {
            "type": "noul",
            "instructions": (
                "If a coding agent carried this out, could it delete, overwrite or "
                "irreversibly change meaningful data (documents, configs, source "
                "code, databases), as opposed to only regenerable files like build "
                "artifacts, caches, logs or temp files?"
            ),
        },
        "regenerable": {
            "type": "noul",
            "instructions": (
                "If data is deleted or overwritten, could it be regenerated by "
                "rebuilding, re-downloading, or re-running a tool with little "
                "effort?"
            ),
        },
    }


def ask(state: str, questions: dict, cfg: dict) -> dict:
    """One Jev fan-out call. Raises TriageError on any failure."""
    body = {"model": cfg.get("model") or DEFAULT_MODEL, "state": state,
            "questions": questions}
    request = urllib.request.Request(
        str(cfg.get("endpoint") or DEFAULT_ENDPOINT),
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {_read_key(cfg)}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    timeout = max(0.5, float(cfg.get("timeoutMs", 8000)) / 1000.0)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode(errors="replace")[:200]
        except Exception:  # noqa: BLE001 - best effort only
            pass
        raise TriageError(f"Jev HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise TriageError(f"Jev request failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise TriageError("Jev returned a non-object response")
    return payload


# --- policy ----------------------------------------------------------------


def decide(answers: dict, cfg: dict) -> tuple[str, str, dict]:
    """Combine typed answers into (verdict, reason, details). Code owns policy."""
    route_answer = answers.get("route") or {}
    route = str(route_answer.get("choice") or "unknown")
    confidence = float(route_answer.get("confidence") or 0.0)
    probabilities = route_answer.get("probabilities") or {}
    destroys = float((answers.get("destructive") or {}).get("noul") or 0.0)
    regenerable = float((answers.get("regenerable") or {}).get("noul") or 0.0)
    clear = float((answers.get("clear") or {}).get("noul") or 0.0)
    # Data loss needs BOTH "might destroy meaningful data" AND "not easily
    # regenerable". The raw destructive reading alone over-fires on routine
    # work: measured live, "run the tests" and "clean build artifacts" both
    # scored ~0.6-0.68 destructive, which would have held ordinary requests.
    destructive = destroys * (1.0 - regenerable)

    min_conf = float(cfg.get("minConfidence", 0.85))
    destructive_floor = float(cfg.get("destructiveConfirm", 0.50))
    clear_floor = float(cfg.get("clearFloor", 0.50))

    details = {
        "route": route, "confidence": confidence,
        "destructive": destructive, "destroys": destroys,
        "regenerable": regenerable, "clear": clear,
        "probabilities": probabilities,
    }
    # Order matters: an unclear utterance is refused before anything else, so a
    # confident-but-mis-heard fragment cannot reach an agent.
    if route == "garbage" or clear < clear_floor:
        return "refuse", (
            f"not a clear request (route={route}, clear={clear:.2f} < {clear_floor})"
        ), details
    if destructive >= destructive_floor:
        return "confirm", (
            f"may be destructive (destructive={destructive:.2f} >= {destructive_floor})"
        ), details
    if confidence < min_conf:
        return "confirm", (
            f"low route confidence ({confidence:.2f} < {min_conf})"
        ), details
    if route != "agent_task":
        return "confirm", (
            f"routed to an agent but classified as {route}; confirm intent"
        ), details
    return "allow", f"clear agent task (confidence {confidence:.2f})", details


def _append_log(entry: dict) -> None:
    """Append one NDJSON record. Never raises into the caller."""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False, default=str)
        fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as stream:
            stream.write(line + "\n")
    except OSError:
        pass


def evaluate(prompt: str, context: str = "") -> Verdict:
    """Triage one agent-delegation prompt under the configured mode.

    Returns a Verdict whose `verdict` is what the CALLER should do. In `off` and
    `log` modes this is always `allow`; `log` still records what would have
    happened. The function never raises: failures are folded into an escalation
    verdict so an untrusted microphone cannot benefit from an outage.
    """
    cfg = load_config()
    mode = str(cfg.get("mode") or "off")
    if mode == "off":
        return Verdict("allow", "unknown", 0.0, 0.0, 0.0, "triage off",
                       enforced=False, used_fallback=False)

    prompt = (prompt or "").strip()
    state = (
        "Request a desktop voice assistant is about to submit to a coding agent.\n"
        f"Request: {prompt}\n"
        f"Context: {context or 'none'}"
    )
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    started = time.monotonic()

    resolved_model = ""
    try:
        payload = ask(state, _questions(), cfg)
        # The provider reports the exact build it served (e.g.
        # "typesafe/jev-1.13-20260917"). Recorded so a silent model change is
        # visible in the log instead of only inferable from a shift in answers.
        resolved_model = str(payload.get("model") or "")
        verdict, reason, details = decide(payload.get("answers") or {}, cfg)
        used_fallback = False
        usage = payload.get("usage") or {}
        cost = float(usage.get("cost") or 0.0)
    except TriageError as exc:
        verdict, reason, details = "confirm", f"triage unavailable: {exc}", {}
        used_fallback = True
        cost = 0.0

    latency = time.monotonic() - started
    enforced = (mode == "enforce")
    effective = verdict if enforced else "allow"

    entry = {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "mode": mode,
        "verdict": verdict,
        "effective": effective,
        "reason": reason,
        "used_fallback": used_fallback,
        "prompt_hash": prompt_hash,
        "cost_usd": cost,
        "latency_s": round(latency, 3),
        "model": cfg.get("model") or DEFAULT_MODEL,
        "resolved_model": resolved_model,
        **details,
    }
    if cfg.get("logPrompt"):
        entry["prompt"] = mask_secrets(prompt)[:2000]
    _append_log(entry)

    return Verdict(
        verdict=effective,
        route=details.get("route", "unknown"),
        confidence=float(details.get("confidence", 0.0) or 0.0),
        destructive=float(details.get("destructive", 0.0) or 0.0),
        clear=float(details.get("clear", 0.0) or 0.0),
        reason=reason,
        enforced=enforced,
        used_fallback=used_fallback,
        cost_usd=cost,
        latency_s=latency,
        probabilities=details.get("probabilities", {}) or {},
    )


# --- reporting -------------------------------------------------------------


def read_log(limit: int = 50) -> list[dict]:
    if not LOG_FILE.exists():
        return []
    rows: list[dict] = []
    for line in LOG_FILE.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows[-limit:]


def summarize(rows: list[dict]) -> dict:
    """Counts that answer 'is this safe to enforce yet?'."""
    by_verdict: dict[str, int] = {}
    by_route: dict[str, int] = {}
    fallbacks = 0
    cost = 0.0
    for row in rows:
        by_verdict[str(row.get("verdict"))] = by_verdict.get(str(row.get("verdict")), 0) + 1
        route = str(row.get("route") or "unknown")
        by_route[route] = by_route.get(route, 0) + 1
        if row.get("used_fallback"):
            fallbacks += 1
        cost += float(row.get("cost_usd") or 0.0)
    return {
        "entries": len(rows),
        "by_verdict": by_verdict,
        "by_route": by_route,
        "fallbacks": fallbacks,
        "cost_usd": round(cost, 6),
    }
