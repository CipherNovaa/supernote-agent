#!/usr/bin/env python3
"""
idea_plan.py — Hermes "idea planner" engine.

Takes a goal the user wants to accomplish (anything — a project, a skill, a life
thing) and turns it into a concrete, structured plan: a short framing, ordered
milestones, actionable next steps as Obsidian-Tasks checkboxes (with 📅 due
dates so they show up in Tasks/Calendar plugins and sync to the Supernote),
web-sourced resources, and *suggested* reminders. It only proposes reminders —
it never creates scheduled jobs on its own (that's a deliberate, separate step).

Fully local model (qwen3:8b on Ollama). Web research is optional and time-boxed
so it can never hang a caller (the Supernote watcher imports this).

Usage:
    idea_plan.py "learn Rust well enough to ship a CLI tool"
    idea_plan.py --no-web "declutter the home office this month"
    echo "get back into running" | idea_plan.py

Importable:
    import idea_plan
    body_md = idea_plan.plan("learn Rust")
"""

import os
import re
import sys
import argparse
import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sn_config as _cfg

# ── Config ────────────────────────────────────────────────────────────────────

MODEL          = _cfg.TEXT_MODEL                  # local; qwen3:8b fits 8GB VRAM
OLLAMA_URL     = _cfg.OLLAMA_GENERATE
SEARX_URL      = _cfg.searx_url()                 # optional local SearXNG JSON API
PLAN_STYLE     = _cfg.state_path("planning_style.md")

GEN_TIMEOUT    = 200     # seconds for the LLM
WEB_TIMEOUT    = 8       # seconds for SearXNG (kept short so it can't hang us)
WEB_RESULTS    = 5       # snippets to feed the model

# ── Personalisation (optional, grows over time) ───────────────────────────────

def load_planning_style() -> str:
    """Optional planning-preferences profile. Like learning_style.md, the
    feedback loop can append `plan:` rules here so plans fit the user better over
    time. Absent by default — that's fine."""
    try:
        return PLAN_STYLE.read_text().strip()
    except Exception:
        return ""

# ── Web research (optional) ───────────────────────────────────────────────────

def web_research(goal: str) -> str:
    """Fetch a few web snippets via SearXNG. Returns '' on any failure."""
    try:
        r = requests.get(
            SEARX_URL,
            params={"q": goal, "format": "json", "safesearch": 0},
            timeout=WEB_TIMEOUT,
        )
        results = r.json().get("results", [])[:WEB_RESULTS]
        lines = []
        for res in results:
            title = (res.get("title") or "").strip()
            content = (res.get("content") or "").strip()
            url = (res.get("url") or "").strip()
            if title and url:
                snippet = f"- {title}: {content} ({url})" if content else f"- {title} ({url})"
                lines.append(snippet)
        return "\n".join(lines)
    except Exception as e:
        print(f"[plan] web_research skipped: {e}", file=sys.stderr)
        return ""

# ── Core planning ─────────────────────────────────────────────────────────────

_SYSTEM = (
    "You are Hermes, a sharp, dry-humoured personal chief-of-staff. "
    "The user gives you a GOAL they want to accomplish. Turn it into a "
    "concrete, motivating, *actionable* plan they can start today.\n\n"
    "Output GitHub-flavoured markdown in EXACTLY this structure, nothing before "
    "or after it.\n\n"
    "Open with one or two plain sentences framing the goal and your approach "
    "(no heading, no fluff, no 'great goal'). Then these sections, in order:\n\n"
    "### 🎯 Milestones\n"
    "1. <milestone — a meaningful checkpoint, in order>\n"
    "2. <milestone>\n"
    "(3–6 milestones; coarse, outcome-focused.)\n\n"
    "### ✅ Next steps\n"
    "- [ ] <small concrete action they can do soon> 📅 <YYYY-MM-DD>\n"
    "- [ ] <small concrete action> 📅 <YYYY-MM-DD>\n"
    "(5–9 steps. These are Obsidian Tasks checkboxes — ALWAYS start each with "
    "`- [ ] ` and end with a single `📅 YYYY-MM-DD` due date. Space the dates "
    "realistically over the coming days/weeks starting from TODAY's date given "
    "below. Front-load the easy first step so they get momentum.)\n\n"
    "### 📚 Resources\n"
    "- [<title>](<url>) — <one line: why it's useful>\n"
    "(ONLY real links from the web sources provided below. If none were "
    "provided, write 'No web sources fetched — search yourself or ask me to.' "
    "Never invent URLs.)\n\n"
    "### ⏰ Suggested reminders\n"
    "- <a check-in or deadline worth a reminder, in plain words with a rough "
    "time>\n"
    "(1–3. These are only SUGGESTIONS — do not claim they are scheduled.)\n\n"
    "Rules: be specific to THIS goal (no generic filler that fits any goal); "
    "keep it tight; correct, honest dates; no preamble, no sign-off, no meta "
    "commentary about the plan."
)


def plan(goal: str, note_context: str = "", use_web: str = "auto",
         model: str = MODEL) -> str:
    """Return a structured plan for `goal` as markdown body text."""
    goal = goal.strip()
    if not goal:
        return "_(no goal given)_"

    style  = load_planning_style()
    today  = datetime.date.today().isoformat()
    web    = web_research(goal) if use_web != "off" else ""

    prompt = _SYSTEM + "\n\n"
    prompt += f"# Today's date (anchor all due dates to this)\n{today}\n\n"
    if style:
        prompt += f"# The user's planning preferences\n{style}\n\n"
    if note_context.strip():
        prompt += f"# Context from the surrounding note\n{note_context[:800]}\n\n"
    if web:
        prompt += f"# Web sources (use ONLY these for the Resources links)\n{web}\n\n"
    prompt += f"# The user's goal — make the plan\n{goal}"

    try:
        r = requests.post(
            OLLAMA_URL,
            json={
                "model": model,
                "stream": False,
                "options": {"temperature": 0.5, "think": False},
                "prompt": prompt,
            },
            timeout=GEN_TIMEOUT,
        )
        ans = r.json().get("response", "").strip()
    except Exception as e:
        print(f"[plan] {e}", file=sys.stderr)
        return f"_(couldn't reach the local model: {e})_"

    # Clean leftover thinking / stray markers.
    ans = re.sub(r"<think>.*?</think>", "", ans, flags=re.DOTALL).strip()
    ans = re.sub(r"^/?think\s*$", "", ans, flags=re.MULTILINE).strip()
    ans = ans.replace("<!-- sn:end -->", "")
    # qwen sometimes echoes the template's placeholder text/`<...>` delimiters in
    # the framing paragraph (everything before the first heading). Clean only the
    # head: strip surrounding angle brackets per line and drop any line that is a
    # verbatim echo of the template instructions. Real markup below `### ` is
    # left untouched.
    _ECHO_FRAGMENTS = (
        "framing the goal", "no fluff", "great goal", "sentences framing",
        "these sections", "in order:",
    )
    head, sep, rest = ans.partition("\n### ")
    cleaned = []
    for ln in head.splitlines():
        s = ln.strip()
        if s.startswith("<"):
            s = s[1:]
        if s.endswith(">"):
            s = s[:-1]
        s = s.strip()
        low = s.lower()
        if s and any(frag in low for frag in _ECHO_FRAGMENTS):
            continue
        cleaned.append(s)
    head = "\n".join(cleaned).strip()
    ans = (head + ("\n\n### " + rest if sep else "")).strip()
    # Guard against invented resource links when no web sources were given:
    # if we fetched nothing, drop any markdown links the model hallucinated.
    if not web:
        ans = re.sub(r"\[([^\]]+)\]\(https?://[^)]+\)", r"\1", ans)
    return ans or "_(no plan produced)_"


def format_block(goal: str, body: str) -> str:
    """Standalone presentation (CLI/Discord): header + attributed plan."""
    return f"## 🎯 Plan\n\n> **Goal:** {goal}\n\n{body}"

# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Hermes idea planner")
    ap.add_argument("goal", nargs="*", help="the goal to plan (or pipe via stdin)")
    ap.add_argument("--web", dest="web", action="store_const", const="on",
                    default="auto", help="force web research on (default: auto)")
    ap.add_argument("--no-web", dest="web", action="store_const", const="off",
                    help="disable web research")
    ap.add_argument("-m", "--model", default=MODEL, help="ollama model")
    ap.add_argument("--raw", action="store_true",
                    help="print body only, no header")
    args = ap.parse_args()

    goal = " ".join(args.goal).strip()
    if not goal and not sys.stdin.isatty():
        goal = sys.stdin.read().strip()
    if not goal:
        ap.error("no goal given (pass as args or via stdin)")

    body = plan(goal, use_web=args.web, model=args.model)
    print(body if args.raw else format_block(goal, body))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
