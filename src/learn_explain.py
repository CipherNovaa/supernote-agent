#!/usr/bin/env python3
"""
learn_explain.py — Hermes "learning accelerator" engine.

Explains a math problem, word problem, or programming concept (C++ mainly)
back to the user in *their* learning style: answer-first, numbered step-by-step,
a memory trick/mnemonic, and a worked-example/gotcha. Personalised from their
own Obsidian math notes and a growing learning-style profile, optionally
fact-checked against the web via a local SearXNG instance.

Fully local model (qwen3:8b on Ollama). Web grounding is optional and
time-boxed so it can never hang a caller (the Supernote watcher imports this).

Usage:
    learn_explain.py "explain exponents"
    learn_explain.py --web "is 1 a prime number"
    learn_explain.py --no-web "solve 4^2 + (15/3)*2 - 7"
    echo "what is a pointer in c++" | learn_explain.py

Importable:
    import learn_explain
    body_md = learn_explain.explain("explain exponents")
"""

import os
import re
import sys
import json
import random
import argparse
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sn_config as _cfg

# ── Config ────────────────────────────────────────────────────────────────────

MODEL          = _cfg.TEXT_MODEL                  # local; qwen3:8b fits 8GB VRAM
OLLAMA_URL     = _cfg.OLLAMA_GENERATE
SEARX_URL      = _cfg.searx_url()                 # optional local SearXNG JSON API
VAULT          = _cfg.vault_root()
NOTES_DIR      = _cfg.examples_dir()
LEARN_STYLE    = _cfg.state_path("learning_style.md")

GEN_TIMEOUT    = 180     # seconds for the LLM
WEB_TIMEOUT    = 8       # seconds for SearXNG (kept short so it can't hang us)
WEB_RESULTS    = 4       # snippets to feed the model

# ── Personalisation sources ───────────────────────────────────────────────────

def load_learning_style() -> str:
    try:
        return LEARN_STYLE.read_text().strip()
    except Exception:
        return ""


def sample_math_notes(n: int = 2, max_chars: int = 1400) -> str:
    """Return up to n of the user's own math notes as style few-shot examples."""
    try:
        candidates = []
        for p in NOTES_DIR.rglob("*.md"):
            try:
                txt = p.read_text()
            except Exception:
                continue
            # Only well-structured math notes (tagged Math, has a Step-by-Step).
            if "[[Math]]" in txt and "Step-by-Step" in txt:
                candidates.append(txt)
        if not candidates:
            return ""
        picks = random.sample(candidates, min(n, len(candidates)))
        return "\n\n---\n\n".join(p[:max_chars] for p in picks)
    except Exception:
        return ""

# ── Web grounding (optional) ──────────────────────────────────────────────────

_MATH_ONLY = re.compile(r"^[\s\d+\-*/^()\[\].=,x×÷√%]+$")


def _is_pure_computation(topic: str) -> bool:
    """Bare arithmetic like '4^2 + (15/3)*2 - 7' — no web needed."""
    t = re.sub(r"(?i)^(solve|calculate|evaluate|compute|what\s+is)\b", "", topic).strip()
    return bool(t) and bool(_MATH_ONLY.match(t))


def web_check(topic: str) -> str:
    """Fetch a few web snippets via SearXNG. Returns '' on any failure."""
    try:
        r = requests.get(
            SEARX_URL,
            params={"q": topic, "format": "json", "safesearch": 0},
            timeout=WEB_TIMEOUT,
        )
        results = r.json().get("results", [])[:WEB_RESULTS]
        lines = []
        for res in results:
            title = (res.get("title") or "").strip()
            content = (res.get("content") or "").strip()
            url = (res.get("url") or "").strip()
            if title and content:
                lines.append(f"- {title}: {content} ({url})")
        return "\n".join(lines)
    except Exception as e:
        print(f"[learn] web_check skipped: {e}", file=sys.stderr)
        return ""


def should_use_web(topic: str, mode: str) -> bool:
    if mode == "off":
        return False
    if mode == "on":
        return True
    # auto: skip bare arithmetic, ground everything conceptual.
    return not _is_pure_computation(topic)

# ── Core explanation ──────────────────────────────────────────────────────────

_SYSTEM = (
    "You are Hermes, a sharp, dry-humoured personal tutor. "
    "You are a *learning accelerator*: you explain math problems, word problems, "
    "and programming concepts (C++ mainly) so they actually stick.\n\n"
    "Explain in THE LEARNER'S style (profile + their own notes below). Rules:\n"
    "1. Answer/solution FIRST. No 'Great question', no filler, no sign-off.\n"
    "2. If it's a problem, show a numbered **Step-by-Step**, rewriting the "
    "expression after each step. Use LaTeX for ALL math: inline $x^2$, block $$...$$.\n"
    "3. Wrap the single most important term or takeaway in ==double equals== so "
    "Obsidian highlights it (e.g. ==base==, ==left to right==). Never write the "
    "literal word 'highlight'.\n"
    "4. Include a **🧠 Memory trick**: a mnemonic, acronym, analogy, or rule of "
    "thumb they can recall under pressure.\n"
    "5. End with a short worked example OR a 'watch out' gotcha framed as a "
    "mistake they might make.\n"
    "6. ONLY if the topic is about programming/C++: also give a tiny minimal "
    "```cpp snippet, name the common beginner pitfall, and the mental model "
    "(what the machine actually does). Do NOT add code for pure-math topics.\n"
    "7. Be correct and concise. If web sources are provided, use them to verify "
    "facts; do not invent citations.\n"
    "Output GitHub-flavoured markdown only — no preamble, no meta commentary."
)


def explain(topic: str, note_context: str = "", use_web: str = "auto",
            model: str = MODEL) -> str:
    """Return a personalised explanation of `topic` as markdown body text."""
    topic = topic.strip()
    if not topic:
        return "_(nothing to explain)_"

    style    = load_learning_style()
    examples = sample_math_notes(2)
    web      = web_check(topic) if should_use_web(topic, use_web) else ""

    prompt = _SYSTEM + "\n\n"
    if style:
        prompt += f"# The learner's learning-style profile\n{style}\n\n"
    if examples:
        prompt += (
            "# Examples of how the learner structures their own notes "
            "(match this style, do NOT copy the content):\n"
            f"{examples}\n\n"
        )
    if note_context.strip():
        prompt += f"# Context from the surrounding note\n{note_context[:800]}\n\n"
    if web:
        prompt += f"# Web sources to verify against\n{web}\n\n"
    prompt += f"# Explain this for the learner\n{topic}"

    try:
        r = requests.post(
            OLLAMA_URL,
            json={
                "model": model,
                "stream": False,
                "options": {"temperature": 0.4, "think": False},
                "prompt": prompt,
            },
            timeout=GEN_TIMEOUT,
        )
        ans = r.json().get("response", "").strip()
    except Exception as e:
        print(f"[learn] {e}", file=sys.stderr)
        return f"_(couldn't reach the local model: {e})_"

    # Clean leftover thinking / stray markers.
    ans = re.sub(r"<think>.*?</think>", "", ans, flags=re.DOTALL).strip()
    ans = re.sub(r"^/?think\s*$", "", ans, flags=re.MULTILINE).strip()
    ans = ans.replace("<!-- sn:end -->", "")
    # qwen3 sometimes bolts a code block onto a pure-arithmetic answer; the
    # "no code for math" instruction isn't always obeyed, so enforce it here.
    if _is_pure_computation(topic):
        ans = re.sub(r"\n*```[a-zA-Z+]*\n.*?```", "", ans, flags=re.DOTALL).strip()
    return ans or "_(no answer)_"


def format_block(topic: str, body: str) -> str:
    """Standalone presentation (CLI/Discord): header + attributed answer."""
    return f"## 📚 Learn\n\n> **Topic:** {topic}\n\n{body}"

# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Hermes learning accelerator")
    ap.add_argument("topic", nargs="*", help="what to explain (or pipe via stdin)")
    ap.add_argument("--web", dest="web", action="store_const", const="on",
                    default="auto", help="force web grounding on")
    ap.add_argument("--no-web", dest="web", action="store_const", const="off",
                    help="disable web grounding")
    ap.add_argument("-m", "--model", default=MODEL, help="ollama model")
    ap.add_argument("--raw", action="store_true",
                    help="print body only, no header")
    args = ap.parse_args()

    topic = " ".join(args.topic).strip()
    if not topic and not sys.stdin.isatty():
        topic = sys.stdin.read().strip()
    if not topic:
        ap.error("no topic given (pass as args or via stdin)")

    body = explain(topic, use_web=args.web, model=args.model)
    print(body if args.raw else format_block(topic, body))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
