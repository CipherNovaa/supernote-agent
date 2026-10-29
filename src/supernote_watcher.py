#!/usr/bin/env python3
"""
Supernote → Obsidian conversion pipeline (the watcher).

Polls the folder Syncthing drops Supernote `.note` files into and, for each new
or changed notebook:

- waits for the device's own handwriting recognition; if it stalls, falls back
  to a local vision model (Ollama) that reads the rendered page
- always renders the pages to PNG and writes a readable "rough" note
- formats the transcript into a clean Obsidian note with a local LLM, guided by
  a style file (learned from feedback) + few-shot examples from the vault —
  automatically when a `//` command asks for it, otherwise on a Discord "format"
- infers tags (biased toward the existing tag vocabulary) and files the note
- runs handwritten `//` commands (ask, learn, plan, remind, book, …)
- pings Discord; replies to that ping are turned into JSON actions by an LLM
  (retag, retitle, refile, learn a style rule, remember a fact)
- mirrors selected vault folders back to the device as A5 PDFs

All paths, models and credentials come from sn_config (config.toml + env).
"""

import re
import os
import hashlib
import random
import sys
import time
import json
import io
import shutil
import subprocess
import requests
from pathlib import Path
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────

# Single source of truth for paths, models and credentials — nothing hardcoded.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sn_config as mp

VAULT        = mp.vault_root()
WATCH_DIR    = mp.supernote_raw()
RAW_REL      = mp.rel("supernote_raw")            # vault-relative, used in backlinks
RAW_LINK     = f"[[{RAW_REL}/"                    # every note links back to its .note
NOTES_DIR    = mp.inbox()                          # default landing for formatted notes (triage via links)
ROUGH_DIR    = mp.inbox()                          # readable handwriting-image copies (default)
EXAMPLES_DIR = mp.examples_dir()                   # well-formatted notes for few-shot examples
SN_IMG_DIR   = mp.images() / "Supernote"           # rendered page PNGs live here
TAGS_DIR     = mp.tags()
FAIL_DIR     = mp.state_path("fails")
PENDING_DIR  = mp.state_path("pending")            # Discord message id -> note metadata
STYLE_FILE   = mp.style_file()
OLLAMA_GENERATE = mp.OLLAMA_GENERATE

POLL_INTERVAL  = 10    # seconds between scans
STABLE_AGE     = 5     # file must be unchanged this long before processing
MAX_FAILS      = 5     # max consecutive failures when RECOGNSTATUS=1
RECOGN_TIMEOUT = int(os.environ.get("SUPERNOTE_RECOGN_TIMEOUT", "90"))  # secs before vision-OCR fallback. In practice the device syncs at RECOGNSTATUS=2 (recognition finishes after the sync snapshot), so the device path rarely wins — short timeout = notes land in ~1.5min not 5. Env-overridable.
# Vision-OCR fallback model (used only when the device's own recognition stalls
# at RECOGNSTATUS=2 past RECOGN_TIMEOUT). Model choice is a real trade-off,
# decided by live testing on real handwriting:
#   - gemma4:26b  → accurate, but ~20GB working set → CPU, slow (2.5-5 min).
#   - llava:7b    → fast, but PARAPHRASES & hallucinates (invented whole
#                   sections + fake // lines) → unsafe for study notes. Rejected.
#   - qwen2.5vl:7b→ faithful (captured the real prose AND every // command) and
#                   ~17s warm. Runs on CPU (13GB with image ctx > 8GB VRAM) but
#                   still much faster than gemma. WINNER.
# The num_predict cap below prevents runaway generation (which caused the
# original read-timeout), and VISION_TIMEOUT is generous to cover the ~6GB cold
# model load on the first call. Override via config or $SUPERNOTE_VISION_MODEL.
VISION_MODEL   = mp.VISION_MODEL
VISION_TIMEOUT = 300   # read timeout (covers cold model load + inference)

for d in [FAIL_DIR, PENDING_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ── Style file ────────────────────────────────────────────────────────────────

_DEFAULT_STYLE = """\
# Supernote Note Style Rules

- Use bullet points with `-` not `*`
- Book, film and game titles always get their own tag e.g. [[Title Name]]
- Use ==highlight== for key terms and definitions
- Lines starting with `#` in raw text become `##` headings in output
- Keep the author's voice — clean up OCR artefacts but don't rewrite
- If a note is about math, preserve all equations and structure
- Hardware/product ideas go under [[Hardware]] tag
- Notes about ideas or inventions should get an [[Ideas]] tag

## Handwriting commands (write these on the Supernote)

Prefix a line with `//` to tell the formatter what to do. Formatting
commands convert that line to markdown; metadata commands are applied
to the note and removed from the body.

Formatting:
- `//h1 Text` … `//h4 Text`  → headings (# .. ####); `//header` = ##
- `//bold Text`              → **Text**
- `//italic Text`            → *Text*
- `//highlight Text`         → ==Text==
- `//strike Text`            → ~~Text~~
- `//bullet Text`            → - Text
- `//numbered Text`          → 1. Text
- `//checklist Text` / `//todo Text` → - [ ] Text
- `//done Text`              → - [x] Text
- `//quote Text`             → > Text
- `//divider`                → ***
- `//code Text`              → `Text`  (or ``` on its own)
- `//math E = mc^2`          → $$E = mc^2$$ (block)
- `//imath x^2`              → $x^2$ (inline)

Metadata:
- `//title is My Title`      → set the note title
- `//tag as Physics`         → force-add a tag
- `//file under PATH`        → file the note in vault PATH
- `//link to [[Note]]`       → append a wikilink
"""


def load_style() -> str:
    if not STYLE_FILE.exists():
        STYLE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STYLE_FILE.write_text(_DEFAULT_STYLE)
        print(f"[Style] Created default style file at {STYLE_FILE}")
    return STYLE_FILE.read_text().strip()


def _example_body(text: str) -> str:
    """Extract just the clean body prose from a note for use as a style example.
    Strips the header block, the wikilink, the SN_END marker, and any
    post-marker computer edits — so none of that can leak into AI output."""
    # Cut everything from the marker onward (computer edits live after it)
    text = text.split("<!-- sn:end -->", 1)[0]
    # Drop the header: take everything after the Raw wikilink line if present
    m = re.search(re.escape(RAW_LINK) + r"[^\]]+\]\]\s*", text)
    if m:
        text = text[m.end():]
    else:
        # No wikilink: strip a leading separator/metadata block if present
        text = re.sub(r"^(?:_{20,}\n.*?\n)+", "", text, flags=re.DOTALL)
    return text.strip()


def sample_example_notes(n: int = 2) -> str:
    """Pick n random notes from the examples dir as few-shot formatting examples."""
    notes = list(EXAMPLES_DIR.rglob("*.md"))
    if not notes:
        return ""
    chosen = random.sample(notes, min(n, len(notes)))
    parts = []
    for p in chosen:
        body = _example_body(p.read_text())[:600]
        if body:
            parts.append(f"=== {p.stem} ===\n{body}")
    return "\n\n".join(parts)

# ── // command parser & formatter ─────────────────────────────────────────────

# Formatting commands: applied as a pre-processor before AI formatting.
# Metadata commands: consumed and stripped before anything else.
_FMT_COMMANDS = {
    # headings
    "h1": lambda rest: f"# {rest}",
    "h2": lambda rest: f"## {rest}",
    "h3": lambda rest: f"### {rest}",
    "h4": lambda rest: f"#### {rest}",
    "header": lambda rest: f"## {rest}",
    "heading": lambda rest: f"## {rest}",
    # inline emphasis (whole line)
    "bold": lambda rest: f"**{rest}**",
    "italic": lambda rest: f"*{rest}*",
    "italics": lambda rest: f"*{rest}*",
    "highlight": lambda rest: f"=={rest}==",
    "strike": lambda rest: f"~~{rest}~~",
    "strikethrough": lambda rest: f"~~{rest}~~",
    # lists
    "bullet": lambda rest: f"- {rest}",
    "list": lambda rest: f"- {rest}",
    "numbered": lambda rest: f"1. {rest}",
    "checklist": lambda rest: f"- [ ] {rest}",
    "todo": lambda rest: f"- [ ] {rest}",
    "done": lambda rest: f"- [x] {rest}",
    # blocks
    "divider": lambda rest: "***",
    "hr": lambda rest: "***",
    "quote": lambda rest: f"> {rest}",
    "blockquote": lambda rest: f"> {rest}",
    "code": lambda rest: f"`{rest}`" if rest else "```",
    # math
    "math": lambda rest: f"$${rest}$$" if rest else "$$",
    "imath": lambda rest: f"${rest}$",
}


# OCR-mangled "//" prefixes. Handwriting recognition on the Supernote rarely
# reads "//" cleanly — real captured examples from the device:
#   "I/ recall", "11 recall division", "I/remind ... @8pm", "I/what is weather"
# The slashes come back as I / l / 1 / | in various pairings, which is THE reason
# "commands from the Supernote do nothing": parse_commands only saw a literal
# "//". canon_command_prefix() rewrites these back to "//" before parsing.
_CMD_WORDS = (
    r"ask|q|learn|explain|teach|plan|goal|planner|remind|reminder|"
    r"books?|recall|remember|search|summari[sz]e|"
    r"summary|tldr|quiz|test|flashcards?|help|commands|md|format|pretty|"
    r"prettify|cleanup|h[1-4]|header|bold|italic|highlight|strike|bullet|"
    r"numbered|checklist|todo|done|quote|divider|code|i?math|title|tag|file|link"
)
_FUZZY_CMD   = re.compile(r"^\s*(?P<pre>[I1l|/]{1,3})\s*(?P<rest>\S.*)$")
_STARTS_CMD  = re.compile(rf"(?:{_CMD_WORDS})\b", re.IGNORECASE)


def canon_command_prefix(line: str) -> str:
    """Rewrite an OCR-mangled '//' command prefix back to a literal '//'.
    Conservative to avoid false positives (e.g. '1/2 cup', 'I learned …'):
    the remainder must start with a LETTER, and we only promote when the prefix
    contains a slash (a strong '//' signal) OR is a clear double-stroke that is
    immediately followed by a known command word. Literal '//' / 'Hermes:' lines
    pass through unchanged for the existing parser."""
    if re.match(r"^\s*(?://|[Hh]ermes:)", line):
        return line
    m = _FUZZY_CMD.match(line)
    if not m:
        return line
    pre, rest = m.group("pre"), m.group("rest")
    if not rest[:1].isalpha():          # blocks "1/2", "/2026", etc.
        return line
    has_slash  = "/" in pre
    strokes    = len(pre.replace(" ", ""))
    starts_cmd = _STARTS_CMD.match(rest) is not None
    # Block a lone-letter remainder ("I/O ports" -> rest "O ports") unless it's
    # an actual command — a single letter is never a free-form Hermes request.
    first_word = rest.split(None, 1)[0]
    if len(first_word) == 1 and not starts_cmd:
        return line
    if has_slash or (strokes >= 2 and starts_cmd):
        return "//" + rest
    return line


def parse_commands(text: str) -> tuple[str, dict]:
    """
    Process '//' lines.  Metadata commands are consumed into a dict.
    Formatting commands are converted to markdown in-place.
    Both 'Hermes:' (legacy) and '//' are accepted.
    OCR-mangled '//' prefixes (I/, 11, 1/, …) are normalized first.
    """
    commands: dict = {}
    out: list[str] = []

    for line in text.splitlines():
        line = canon_command_prefix(line)
        # Match // or legacy Hermes: prefix
        m = re.match(r"^\s*(?://|[Hh]ermes:)\s*(.+)$", line)
        if not m:
            out.append(line)
            continue
        cmd_full = m.group(1).strip()

        # Metadata commands (consume, don't pass to output)
        if (mm := re.match(r"tag as\s+(.+)", cmd_full, re.IGNORECASE)):
            commands.setdefault("extra_tags", []).append(mm.group(1).strip())
            continue
        if (mm := re.match(r"file under\s+(.+)", cmd_full, re.IGNORECASE)):
            commands["file_under"] = mm.group(1).strip()
            continue
        if (mm := re.match(r"title is\s+(.+)", cmd_full, re.IGNORECASE)):
            commands["title"] = mm.group(1).strip()
            continue
        if (mm := re.match(r"link to\s+(\[\[.+?\]\])", cmd_full, re.IGNORECASE)):
            commands.setdefault("links", []).append(mm.group(1).strip())
            continue
        # Ask a question from the note (answered inline, fully local)
        if (mm := re.match(r"(?:ask|q)\s+(.+)", cmd_full, re.IGNORECASE)):
            commands.setdefault("asks", []).append(mm.group(1).strip())
            continue
        # Learning accelerator: explain a concept/problem in the user's study style
        if (mm := re.match(r"(?:learn|explain|teach)\s+(.+)", cmd_full, re.IGNORECASE)):
            commands.setdefault("learns", []).append(mm.group(1).strip())
            continue
        # Idea planner: structure a goal into milestones + actionable steps
        if (mm := re.match(r"(?:plan|goal|planner)\s+(.+)", cmd_full, re.IGNORECASE)):
            commands.setdefault("plans", []).append(mm.group(1).strip())
            continue
        # Reminder: //remind@8:30 water plants  /  //remind@2026-06-10@9am return books
        # spec = first whitespace-free token (date and/or time, '@'-joined);
        # the rest of the line is the reminder text. Scheduled via hermes cron.
        if (mm := re.match(r"remind(?:er)?\s*@?\s*(\S+)(?:\s+(.+))?$", cmd_full, re.IGNORECASE)):
            commands.setdefault("reminds", []).append(
                (mm.group(1).strip(), (mm.group(2) or "").strip()))
            continue
        # Book grabber: //book stroustrup c++   /   //book@pdf linux kernel
        # Optional @ext format filter, then the search query. The results are
        # listed in the note; download one by telling Hermes (Discord/PC) the
        # number ("grab book 3").
        if (mm := re.match(r"books?\s*(?:@(\w+))?\s+(.+)$", cmd_full, re.IGNORECASE)):
            commands.setdefault("books", []).append(
                ((mm.group(1) or "").strip().lower() or None, mm.group(2).strip()))
            continue
        # Rich markdown reformat: opinionated restructure of the WHOLE note
        # (heavier than the default conservative formatter). Note-level flag,
        # takes no argument, so it is consumed without emitting a line.
        if re.match(r"(?:md|format|pretty|prettify|formatme|cleanup)(?:\s+.*)?$",
                    cmd_full, re.IGNORECASE):
            commands["md"] = True
            continue
        # ── Claude powers ──────────────────────────────────────────────────
        # //recall <q> — grounded QA over the whole vault (with citations).
        if (mm := re.match(r"(?:recall|remember|search\s+(?:my\s+)?notes?|"
                           r"find\s+in\s+(?:my\s+)?notes?)\s+(.+)",
                           cmd_full, re.IGNORECASE)):
            commands.setdefault("recalls", []).append(mm.group(1).strip())
            continue
        # //summarize / //tldr / //summary — TL;DR of THIS note (no argument).
        if re.match(r"(?:summari[sz]e|summary|tldr|tl;dr|tl dr)$",
                    cmd_full, re.IGNORECASE):
            commands["summarize"] = True
            continue
        # //quiz [n] — active-recall questions from THIS note.
        if (mm := re.match(r"(?:quiz|test\s+me|flashcards?)(?:\s+me)?(?:\s+(\d+))?$",
                           cmd_full, re.IGNORECASE)):
            commands["quiz"] = int(mm.group(1)) if mm.group(1) else 5
            continue
        # //help — list everything Hermes can do.
        if re.match(r"(?:help|commands|\?|what\s+can\s+you\s+do)$",
                    cmd_full, re.IGNORECASE):
            commands["help"] = True
            continue

        # Formatting commands: keyword [rest of line]
        parts = cmd_full.split(None, 1)
        keyword = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""
        if keyword in _FMT_COMMANDS:
            out.append(_FMT_COMMANDS[keyword](rest))
        else:
            # Not a known command → treat it as a free-form request to Hermes.
            # The intent is inferred later (resolve_infers) and routed to the
            # right engine. "//" is a direct line: nothing is silently dropped.
            print(f"[//] Free-form request → infer: {cmd_full!r}")
            commands.setdefault("infers", []).append(cmd_full)

    return "\n".join(out), commands

# ── AI body formatter ─────────────────────────────────────────────────────────
# The two formatters live in the shared engine md_format.py (single source of
# truth, also usable from an agent plugin or the CLI). Hard dependency:
# every note is formatted, so a broken import should fail loudly at startup.
from md_format import format_body, format_body_rich   # noqa: E402

# ── //ask — answer questions inline (fully local) ─────────────────────────────

ASK_MODEL = mp.TEXT_MODEL   # local assistant model (qwen3:8b fits 8GB VRAM)


def answer_question(question: str, note_context: str = "") -> str:
    """Answer a //ask question using the local LLM. Returns concise markdown."""
    prompt = (
        "You are Hermes, a sharp, no-fluff personal assistant. Answer the "
        "question directly and concisely in markdown. Use lists or math ($...$) "
        "where helpful. No preamble, no 'Great question', just the answer.\n\n"
    )
    if note_context.strip():
        prompt += f"Context from the surrounding note:\n{note_context[:800]}\n\n"
    prompt += f"Question: {question}"
    try:
        r = requests.post(
            OLLAMA_GENERATE,
            json={
                "model": ASK_MODEL,
                "stream": False,
                "options": {"temperature": 0.3, "think": False},
                "prompt": prompt,
            },
            timeout=180,
        )
        ans = r.json().get("response", "").strip()
        ans = re.sub(r"<think>.*?</think>", "", ans, flags=re.DOTALL).strip()
        ans = ans.replace("<!-- sn:end -->", "")   # never let it inject the marker
        return ans or "_(no answer)_"
    except Exception as e:
        print(f"[//ask] {e}")
        return f"_(couldn't reach the local model: {e})_"


def build_ask_block(asks: list[str], note_context: str = "") -> str:
    """Build a clearly-attributed Q&A section for the given questions."""
    if not asks:
        return ""
    parts = ["\n\n## 💬 Ask"]
    for q in asks:
        print(f"[//ask] answering: {q!r}")
        ans = answer_question(q, note_context)
        parts.append(f"\n> **Q:** {q}\n\n{ans}")
    return "\n".join(parts)

# ── //learn — learning accelerator (personal style, optional web) ─────────────
# Imported defensively: if the engine is missing/broken, the watcher must still
# process notes normally (just without //learn).
try:
    import learn_explain
except Exception as _e:                       # pragma: no cover
    learn_explain = None
    print(f"[//learn] engine unavailable: {_e}")


def build_learn_block(learns: list[str], note_context: str = "") -> str:
    """Build a 📚 Learn section: each topic explained in the user's learning style."""
    if not learns:
        return ""
    if learn_explain is None:
        return ("\n\n## 📚 Learn\n\n> _(learning engine unavailable — "
                "check src/learn_explain.py)_")
    parts = ["\n\n## 📚 Learn"]
    for topic in learns:
        print(f"[//learn] explaining: {topic!r}")
        body = learn_explain.explain(topic, note_context=note_context, use_web="auto")
        parts.append(f"\n> **Topic:** {topic}\n\n{body}")
    return "\n".join(parts)

# ── //plan — idea planner (goal → milestones + actionable steps) ───────────────
# Imported defensively, same as the learning engine: a missing/broken planner
# must never stop the watcher from processing notes normally.
try:
    import idea_plan
except Exception as _e:                       # pragma: no cover
    idea_plan = None
    print(f"[//plan] engine unavailable: {_e}")


def build_plan_block(plans: list[str], note_context: str = "") -> str:
    """Build a 🎯 Plan section: each goal turned into a structured plan with
    Obsidian-Tasks checkboxes, web-sourced resources, and suggested reminders."""
    if not plans:
        return ""
    if idea_plan is None:
        return ("\n\n## 🎯 Plan\n\n> _(idea planner unavailable — "
                "check src/idea_plan.py)_")
    parts = ["\n\n## 🎯 Plan"]
    for goal in plans:
        print(f"[//plan] planning: {goal!r}")
        body = idea_plan.plan(goal, note_context=note_context, use_web="auto")
        parts.append(f"\n> **Goal:** {goal}\n\n{body}")
    return "\n".join(parts)

# ── //remind — schedule a reminder (delivered to Discord via hermes cron) ──────
# Imported defensively like the other assistant engines. Reminders have a real
# side effect (they create cron jobs), so the engine dedups per note+spec+text
# so the same handwritten //remind isn't rescheduled on every resync.
try:
    import reminder_set
except Exception as _e:                       # pragma: no cover
    reminder_set = None
    print(f"[//remind] engine unavailable: {_e}")


def build_remind_block(reminds: list[tuple[str, str]], note_id: str = "") -> str:
    """Build a ⏰ Reminders section: schedule each //remind and report status."""
    if not reminds:
        return ""
    if reminder_set is None:
        return ("\n\n## ⏰ Reminders\n\n> _(reminder engine unavailable — "
                "check src/reminder_set.py)_")
    parts = ["\n\n## ⏰ Reminders"]
    for spec, text in reminds:
        if not text:
            parts.append(f"\n> ⚠️ `//remind@{spec}` needs text after the time "
                         f"(e.g. `//remind@{spec} water plants`).")
            continue
        try:
            schedule, recurring, human = reminder_set.parse_when(spec)
        except ValueError as e:
            parts.append(f"\n> ⚠️ Couldn't read the time `@{spec}`: {e}")
            continue
        key = f"{note_id}|{spec}|{text}"
        print(f"[//remind] scheduling {text!r} @ {schedule} (key={key!r})")
        status = reminder_set.make_reminder(text, schedule, recurring=recurring, key=key)
        parts.append(f"\n> **{text}** — {human}\n>\n> {status}")
    return "\n".join(parts)

# ── //book — search Libgen and list results (download via Discord/PC) ──────────
# Imported defensively like the other engines. //book only SEARCHES; it lists
# hits in the note and caches them (book_search_last.json), so the actual
# download — a real side effect — happens when the user picks a number through
# the agent on Discord/PC (the download_book tool reads the same cache).
try:
    import book_grabber
except Exception as _e:                       # pragma: no cover
    book_grabber = None
    print(f"[//book] engine unavailable: {_e}")


def build_book_block(books: list[tuple[str | None, str]]) -> str:
    """Build a 📚 Books section: search Libgen for each query and list hits."""
    if not books:
        return ""
    if book_grabber is None:
        return ("\n\n## 📚 Books\n\n> _(no book engine installed — "
                "plug one in as a `book_grabber` module)_")
    parts = ["\n\n## 📚 Books"]
    for ext, query in books:
        print(f"[//book] searching {query!r} ext={ext}")
        try:
            md = book_grabber.search_md(query, ext=ext)
        except Exception as e:                # pragma: no cover — defensive
            md = f"⚠️ Book search failed: {e}"
        label = f"{query} ({ext})" if ext else query
        parts.append(f"\n> **Search:** {label}\n\n{md}")
    parts.append("\n> _To download, tell Hermes on Discord/PC: “grab book N” "
                 "(N from the most recent list above)._")
    return "\n".join(parts)

# "Claude powers" ported to the local system: //recall (grounded QA over the
# vault, with citations), //quiz (active-recall study questions), //summarize.
# Optional engine, NOT shipped in this repo (it is tied to the author's own
# vault layout) — imported defensively, so these commands report
# "unavailable" until a claude_powers.py exposing recall/quiz/summarize exists.
try:
    import claude_powers
except Exception as _e:                       # pragma: no cover
    claude_powers = None
    print(f"[//powers] engine unavailable: {_e}")


# ── "Claude powers" blocks: //recall, //quiz, //summarize, //help ────────────

def build_recall_block(recalls: list[str]) -> str:
    """Build a 🧠 Recall section: answer each question from the user's own vault."""
    if not recalls:
        return ""
    if claude_powers is None:
        return ("\n\n## 🧠 Recall\n\n> _(recall unavailable — "
                "check the optional claude_powers module)_")
    parts = ["\n\n## 🧠 Recall"]
    for q in recalls:
        print(f"[//recall] {q!r}")
        try:
            ans = claude_powers.recall(q)
        except Exception as e:                    # pragma: no cover — defensive
            ans = f"⚠️ Recall failed: {e}"
        parts.append(f"\n> **Q:** {q}\n\n{ans}")
    return "\n".join(parts)


def build_quiz_block(do_quiz, note_context: str) -> str:
    """Build a 📝 Quiz section: active-recall questions from the note."""
    if not do_quiz:
        return ""
    if claude_powers is None:
        return ("\n\n## 📝 Quiz\n\n> _(quiz unavailable — "
                "check the optional claude_powers module)_")
    n = do_quiz if isinstance(do_quiz, int) and do_quiz > 0 else 5
    print(f"[//quiz] generating {n} questions")
    try:
        md = claude_powers.quiz(note_context, n=n)
    except Exception as e:                        # pragma: no cover — defensive
        md = f"⚠️ Quiz failed: {e}"
    return f"\n\n## 📝 Quiz\n\n{md}"


def build_summary_block(do_summary: bool, note_context: str) -> str:
    """Build a 📌 Summary section: TL;DR + key points of the note."""
    if not do_summary:
        return ""
    if claude_powers is None:
        return ("\n\n## 📌 Summary\n\n> _(summarizer unavailable — "
                "check the optional claude_powers module)_")
    print("[//summarize] summarizing note")
    try:
        md = claude_powers.summarize(note_context)
    except Exception as e:                        # pragma: no cover — defensive
        md = f"⚠️ Summarize failed: {e}"
    return f"\n\n## 📌 Summary\n\n{md}"


# One place listing every // power, surfaced by //help and reused in docs.
HELP_TEXT = """\

## 🤖 What Hermes can do

Write `//` then anything — Hermes infers what you mean. Known shortcuts:

**Assistant**
- `//ask <q>` — answer a question (local)
- `//recall <q>` — answer from YOUR notes, with [[citations]]
- `//learn <topic>` — explain a concept in your study style
- `//plan <goal>` — turn a goal into a structured plan

**Study**
- `//summarize` / `//tldr` — TL;DR + key points of this note
- `//quiz` — active-recall questions with hidden answers

**Actions**
- `//remind@<when> <text>` — schedule a reminder
- `//book <query>` — search for a book to download

**Note tools**
- `//md` — fully reformat this note
- `//title is X`, `//tag as X`, `//file under PATH`, `//link to [[Note]]`

Anything else after `//` is sent straight to Hermes — just write naturally.
"""


def build_help_block(do_help: bool) -> str:
    return f"\n\n{HELP_TEXT}" if do_help else ""

# ── // intent inference — "a direct line to Hermes" ──────────────────────────
# Any '//' line that isn't a known command is no longer dropped. It is treated
# as a free-form request to Hermes: the local LLM infers what was meant and
# routes it to the right engine (reminder / ask / learn / plan / book). This is
# the "he understands anything I need and texts me back" behaviour — pen
# shorthand → the right action, fault-tolerant of messy OCR.

INFER_MODEL = mp.TEXT_MODEL   # local; qwen3:8b fits 8GB VRAM


def infer_intent(cmd: str) -> dict:
    """Classify a free-form // command into a known action.

    Returns {"action": remind|ask|learn|plan|book, "spec": str, "text": str}.
    Always returns something usable — on any failure it falls back to 'ask' so
    the request is still answered rather than silently dropped.
    """
    fallback = {"action": "ask", "spec": "", "text": cmd}
    prompt = (
        "You are Hermes, a personal assistant. The user jotted a shorthand "
        "command to you on paper (it may have messy handwriting-OCR errors). "
        "Infer what they want and map it to ONE action. Reply with JSON only:\n"
        '{"action": "...", "spec": "...", "text": "..."}\n\n'
        "action is one of:\n"
        "  remind   — anything time-bound: a task, todo, appointment, 'remind me'.\n"
        "  book     — wants to find/download a book.\n"
        "  learn    — wants a concept/math/programming topic explained.\n"
        "  plan     — wants help structuring a goal or project.\n"
        "  recall   — wants to look something up in THEIR OWN notes/vault "
        "('what did I write about', 'do my notes say', 'find my note on').\n"
        "  quiz     — wants to be quizzed/tested on THIS note.\n"
        "  summarize— wants a TL;DR/summary of THIS note.\n"
        "  ask      — a direct question or anything else.\n\n"
        "For remind: spec = the time/date as a compact token (e.g. '11:30', "
        "'2026-06-10@9am', 'tomorrow@8'); text = the thing to be reminded of. "
        "If no clear time exists, do NOT use remind.\n"
        "For recall: text = the thing to look up. For quiz/summarize: text can "
        "be empty (they act on the current note).\n"
        "For the others: leave spec empty; text = the cleaned-up request.\n"
        "Fix obvious OCR typos in text. Reply with ONLY the JSON object.\n\n"
        f"Command: {cmd}"
    )
    try:
        r = requests.post(
            OLLAMA_GENERATE,
            json={
                "model": INFER_MODEL,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.1, "think": False},
                "prompt": prompt,
            },
            timeout=120,
        )
        raw = r.json().get("response", "").strip()
        data = json.loads(raw)
        action = str(data.get("action", "ask")).lower().strip()
        if action not in {"remind", "ask", "learn", "plan", "book",
                          "recall", "quiz", "summarize"}:
            action = "ask"
        spec = str(data.get("spec", "")).strip()
        text = str(data.get("text", "")).strip() or cmd
        if action == "remind" and not spec:
            action = "ask"          # no time → answer it instead of mis-scheduling
        print(f"[//infer] {cmd!r} → {action} spec={spec!r} text={text!r}")
        return {"action": action, "spec": spec, "text": text}
    except Exception as e:                       # pragma: no cover — defensive
        print(f"[//infer] failed ({e}); treating as a question")
        return fallback


def resolve_infers(commands: dict) -> None:
    """Expand inferred // commands into the concrete command lists in place."""
    for raw in commands.pop("infers", []):
        intent = infer_intent(raw)
        act, spec, text = intent["action"], intent["spec"], intent["text"]
        if act == "remind":
            commands.setdefault("reminds", []).append((spec, text))
        elif act == "learn":
            commands.setdefault("learns", []).append(text)
        elif act == "plan":
            commands.setdefault("plans", []).append(text)
        elif act == "book":
            commands.setdefault("books", []).append((None, text))
        elif act == "recall":
            commands.setdefault("recalls", []).append(text)
        elif act == "quiz":
            commands["quiz"] = commands.get("quiz") or 5
        elif act == "summarize":
            commands["summarize"] = True
        else:
            commands.setdefault("asks", []).append(text)


def build_command_blocks(commands: dict, note_context: str, note_id: str) -> str:
    """Build every // action block in one place (shared by the normal and the
    vision-OCR paths so commands are never silently lost on either). Inferred
    commands are resolved first, then all engines run."""
    resolve_infers(commands)
    out = ""
    out += build_ask_block(commands.get("asks", []), note_context=note_context)
    out += build_learn_block(commands.get("learns", []), note_context=note_context)
    out += build_plan_block(commands.get("plans", []), note_context=note_context)
    out += build_remind_block(commands.get("reminds", []), note_id=note_id)
    out += build_book_block(commands.get("books", []))
    out += build_recall_block(commands.get("recalls", []))
    out += build_summary_block(commands.get("summarize", False), note_context)
    out += build_quiz_block(commands.get("quiz", False), note_context)
    out += build_help_block(commands.get("help", False))
    return out

# ── Helpers ───────────────────────────────────────────────────────────────────

# Notifications go through sn_config.send_discord: the Hermes Agent CLI when it
# is installed (resolved to an absolute path — a systemd user service's minimal
# PATH omits ~/.local/bin, which once silently killed every ping), otherwise
# the Discord bot REST API.
print(f"[init] hermes CLI: {mp.hermes_bin() or 'not found — using Discord REST'}")


def _gif_suffix(mood: str | None) -> str:
    """Maybe append a situational reddit-style GIF (only SOME of the time).
    Defensive: GIFs are garnish — never break a notification over one."""
    if not mood:
        return ""
    try:
        import gif_picker
        url = gif_picker.maybe_gif(mood)
        return f"\n{url}" if url else ""
    except Exception:
        return ""


def send_discord(msg: str, note_path: str | None = None,
                 note_title: str | None = None,
                 mood: str | None = None) -> str | None:
    """
    Send a Discord message and return the message_id (= Discord thread_id).
    If note_path is given, saves a metadata file so the feedback loop can
    look up the exact note when the user replies in the thread.
    If `mood` is given, sometimes appends a situational GIF.
    """
    msg = msg + _gif_suffix(mood)
    try:
        message_id = mp.send_discord(msg)
        if message_id and note_path:
            meta = {
                "thread_id":  message_id,
                "note_path":  note_path,
                "note_title": note_title or "",
                "timestamp":  datetime.now().isoformat(),
            }
            (PENDING_DIR / f"{message_id}.json").write_text(json.dumps(meta, indent=2))
        return message_id
    except Exception as e:
        print(f"[Discord] {e}")
        return None


def notify_received(name: str, via: str = "") -> None:
    """First of the two pings: tell the user the note landed and is being worked on,
    so he can SEE the pipeline is alive (the second ping is the result summary).
    Fire-and-forget — never blocks or breaks processing."""
    suffix = f" _( {via} )_" if via else ""
    send_discord(f"📥 Got it — working on **{name}** now…{suffix}")


def _safe_read(p: Path) -> str:
    """Read a file's text, returning '' on any error (used for dupe detection)."""
    try:
        return p.read_text()
    except Exception:
        return ""


NOOP_SIMILARITY = 0.90   # ≥ this → treat re-processed text as the SAME note


def _text_similarity(a: str, b: str) -> float:
    """Normalized similarity (0..1) between two OCR texts. Used to detect a
    re-saved note whose handwriting is unchanged but whose OCR differs slightly —
    e.g. the device finishing recognition and producing a variant of the vision
    OCR (//quiz 3 → 'v qui-23'). Same ink, ~0.94 similar; different note ~0.27."""
    import difflib
    na = " ".join((a or "").lower().split())
    nb = " ".join((b or "").lower().split())
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def note_hash(note: Path) -> str:
    return hashlib.md5(note.read_bytes()).hexdigest()

# ── Sidecar ───────────────────────────────────────────────────────────────────

def sidecar(note: Path) -> Path:
    return note.with_suffix(".processed")


def _read_sidecar(note: Path) -> dict:
    """Return parsed sidecar data. Handles legacy plain-hash format."""
    sc = sidecar(note)
    if not sc.exists():
        return {}
    raw = sc.read_text().strip()
    if raw == "done":
        return {"hash": "done"}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        # Legacy format: just the hash string
        return {"hash": raw}


def is_done(note: Path) -> bool:
    data = _read_sidecar(note)
    if not data:
        return False
    h = data.get("hash", "")
    if h == "done":
        return True
    return h == note_hash(note)


def get_previous_out(note: Path) -> Path | None:
    """Return the Path of a previously written output note, if recorded."""
    data = _read_sidecar(note)
    rel = data.get("out")
    if not rel:
        return None
    p = VAULT / rel
    return p if p.exists() else None


def mark_in_progress(note: Path):
    """Write hash immediately so concurrent instances skip this note.
    Preserves existing 'out' and 'body_raw' so they survive the lock."""
    existing = _read_sidecar(note)
    data: dict = {"hash": note_hash(note)}
    for key in ("out", "body_raw"):
        if key in existing:
            data[key] = existing[key]
    sidecar(note).write_text(json.dumps(data))


def mark_done(note: Path, out: Path | None = None, body_raw: str | None = None,
              rough: Path | None = None, awaiting_format: bool | None = None):
    # Merge into existing sidecar data so fields we aren't given (e.g. body_raw
    # on the vision path, where _save_note calls mark_done(out=...) without it)
    # aren't wiped. A bare mark_done used to drop body_raw → the no-op guard had
    # nothing to compare against → re-process + re-ping on every device re-save.
    data: dict = _read_sidecar(note)
    data["hash"] = note_hash(note)
    if out is not None:
        try:
            data["out"] = str(out.relative_to(VAULT))
        except ValueError:
            data["out"] = str(out)
    if body_raw is not None:
        data["body_raw"] = body_raw
    if rough is not None:
        try:
            data["rough"] = str(rough.relative_to(VAULT))
        except ValueError:
            data["rough"] = str(rough)
    if awaiting_format is not None:
        data["awaiting_format"] = awaiting_format
    sidecar(note).write_text(json.dumps(data))


def get_previous_raw(note: Path) -> str | None:
    return _read_sidecar(note).get("body_raw")

# ── Fail counter (RECOGNSTATUS=1 edge cases) ──────────────────────────────────

def _fail_file(note: Path) -> Path:
    return FAIL_DIR / f"{note.stem}.fails"


def fail_count(note: Path) -> int:
    f = _fail_file(note)
    return int(f.read_text()) if f.exists() else 0


def inc_fail(note: Path) -> int:
    n = fail_count(note) + 1
    _fail_file(note).write_text(str(n))
    return n


def clear_fails(note: Path):
    _fail_file(note).unlink(missing_ok=True)

# ── supernotelib extraction ───────────────────────────────────────────────────

def _load(note: Path):
    from supernotelib.parser import load_notebook
    return load_notebook(str(note))


def _page_is_blank(p) -> bool:
    """A page with no stroke data (TOTALPATH=0) has no ink on it."""
    return str(p.metadata.get("TOTALPATH", "0")) in ("0", "", "None")


INK_RATIO_MIN = 0.0008   # ~0.08% of the page (a few strokes). Below this = effectively blank.


def _render_ink_ratio(nb) -> float:
    """Fraction of non-white pixels in the HANDWRITING layers, with the page
    TEMPLATE (BGLAYER grid/lines) hidden. This is the RELIABLE ink signal:
    TOTALPATH metadata lies — a real note was seen with TOTALPATH=0 while its
    MAINLAYER held strokes, and a blank page can carry a 227KB grid template
    that fools a byte-size check. So we look at what actually renders.
    Returns -1.0 if rendering fails (caller falls back to metadata)."""
    try:
        import numpy as np
        from supernotelib import converter as _conv
        from supernotelib.converter import ImageConverter, VisibilityOverlay
        ic = ImageConverter(nb)
        try:
            overlay = _conv.build_visibility_overlay(background=VisibilityOverlay.INVISIBLE)
        except Exception:
            overlay = None
        ratio = 0.0
        for i in range(len(nb.pages)):
            img = ic.convert(i, overlay) if overlay else ic.convert(i)
            a = np.asarray(img.convert("L"))
            ratio = max(ratio, float((a < 250).sum()) / a.size)
        return ratio
    except Exception as e:
        print(f"[Ink] render check failed: {e}")
        return -1.0


def _has_ink(nb) -> bool:
    """True if the note actually has handwriting. A note can claim
    RECOGNSTATUS=1 yet have text the device couldn't linearize (long division,
    diagrams, heavy math layout) — extract_text returns nothing but there IS
    ink, so it must go to vision OCR rather than be silently discarded.
    Render-based; falls back to TOTALPATH metadata only if rendering fails."""
    r = _render_ink_ratio(nb)
    if r < 0:
        return any(not _page_is_blank(p) for p in nb.pages)
    return r >= INK_RATIO_MIN


def all_pages_ready(nb) -> bool:
    """
    True when every page that has ink is recognized (RECOGNSTATUS=1).
    Blank trailing pages (no strokes) are ignored so they don't block —
    otherwise a single empty page forces the 300s vision-OCR timeout.
    """
    for p in nb.pages:
        if _page_is_blank(p):
            continue
        if p.metadata.get("RECOGNSTATUS") != "1":
            return False
    return True


def extract_text(nb) -> str | None:
    from supernotelib.converter import TextConverter
    tc = TextConverter(nb)
    parts = []
    for i in range(len(nb.pages)):
        t = tc.convert(i)
        if t and t.strip():
            parts.append(t.strip())
    return "\n".join(parts) or None


def render_png_bytes(nb) -> bytes | None:
    """Render first page to raw PNG bytes for vision OCR (not saved to disk)."""
    try:
        from supernotelib.converter import ImageConverter
        img = ImageConverter(nb).convert(0)
        if img is None:
            return None
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        print(f"[PNG] Render error: {e}")
        return None

# ── Vision OCR fallback ───────────────────────────────────────────────────────

def ocr_with_vision(png_bytes: bytes) -> str | None:
    """Vision OCR via VISION_MODEL (see the model notes at the top). Resizes to
    ≤700px wide before sending."""
    try:
        import base64
        from PIL import Image as _Image
        img = _Image.open(io.BytesIO(png_bytes)).convert("RGB")
        if img.width > 700:
            ratio = 700 / img.width
            img = img.resize((700, int(img.height * ratio)), _Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        img_b64 = base64.b64encode(buf.getvalue()).decode()
        r = requests.post(
            OLLAMA_GENERATE,
            json={
                "model": VISION_MODEL,
                "stream": False,
                # Cap output: without this the vision model can run away
                # generating until the context limit at vision speed (→ a
                # multi-minute hang / read timeout). A note page is well under
                # this; 1024 tokens is plenty.
                "options": {"temperature": 0.1, "num_predict": 1024},
                "prompt": (
                    "This is a handwritten note from an e-ink tablet. "
                    "Transcribe all the text exactly as written, preserving "
                    "structure and bullet points. "
                    "Output only the transcribed text, nothing else."
                ),
                "images": [img_b64],
            },
            timeout=VISION_TIMEOUT,
        )
        text = r.json().get("response", "").strip()
        return text or None
    except Exception as e:
        print(f"[vision] {e}")
    return None

# ── Tags ──────────────────────────────────────────────────────────────────────

MAX_TAGS = 4   # hard cap on tags per note
MAX_NEW  = 2   # at most this many brand-new (non-existing) tags per note

# Generic filler that should never become a tag (kills the sprawl).
TAG_STOPLIST = {
    "setup", "device", "devices", "app", "apps", "feature", "features",
    "test", "testing", "misc", "general", "note", "notes", "thing", "things",
    "stuff", "info", "information", "nativeapp", "topic", "content",
}


def _norm_tag(name: str) -> str:
    """Normalization key: lowercase, strip spaces/hyphens/punctuation.
    So 'Home Lab', 'HomeLab', 'home-lab' all collapse to one key."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def infer_tags(title: str, body: str) -> list[str]:
    """Suggest tags, strongly biased toward the existing vocabulary."""
    vocab = sorted(get_existing_tags().values())
    vocab_str = ", ".join(vocab)
    prompt = (
        "Choose Obsidian tags for the note below. Rules:\n"
        f"1. STRONGLY PREFER tags from this existing list: {vocab_str}\n"
        "2. Only invent a NEW tag if it names the note's MAIN SUBJECT and "
        "nothing in the list fits. New tags should come from the title.\n"
        "3. Pick 2-4 broad topical tags. Do NOT add generic filler "
        "(e.g. Setup, Devices, App, Feature, Note, Test).\n"
        "4. Use Title Case with spaces (e.g. 'Note Taking', 'Home Lab').\n"
        '5. Return ONLY a JSON array, e.g. ["Math","Academics"].\n\n'
        f"Title: {title}\n\nBody:\n{body[:600]}"
    )
    try:
        r = requests.post(
            OLLAMA_GENERATE,
            json={
                "model": mp.TEXT_MODEL,
                "stream": False,
                "options": {"temperature": 0.1, "think": False},
                "prompt": prompt,
            },
            timeout=60,
        )
        raw = r.json().get("response", "")
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
        m = re.search(r"\[.*?\]", raw, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception as e:
        print(f"[Tags] {e}")
    return []


def get_existing_tags() -> dict[str, str]:
    """Map normalized key → canonical existing tag name.
    On collision (e.g. 'Home Lab' vs 'HomeLab') prefer the spaced one."""
    out: dict[str, str] = {}
    for f in TAGS_DIR.glob("*.md"):
        key = _norm_tag(f.stem)
        if key not in out or (" " in f.stem and " " not in out[key]):
            out[key] = f.stem
    return out


def resolve_tags(suggested: list[str], title: str = "") -> tuple[list[str], list[str]]:
    """
    Split suggestions into (matched_existing, new).
    - Existing matches use the canonical stored spelling (dedupes case/spacing).
    - New tags are kept sparingly: filler is dropped, title-derived tags are
      prioritized, and at most MAX_NEW survive — enforcing "reuse, rarely add,
      but add from the title more readily".
    """
    ex = get_existing_tags()
    title_norm = _norm_tag(title)
    matched, new_in_title, new_other, seen = [], [], [], set()
    for t in suggested:
        key = _norm_tag(t)
        if not key or key in seen or key in TAG_STOPLIST:
            continue
        seen.add(key)
        if key in ex:
            matched.append(ex[key])
            continue
        name = t.strip().title() if t.islower() else t.strip()
        if key in title_norm or title_norm in key:
            new_in_title.append(name)   # subject of the note → preferred
        else:
            new_other.append(name)
    new = (new_in_title + new_other)[:MAX_NEW]
    return matched[:MAX_TAGS], new


def finalize_tags(matched: list[str], new_tags: list[str],
                  extra: list[str]) -> list[str]:
    """Combine tag sources, dedupe by normalized key, cap at MAX_TAGS.
    Explicit '// tag as' tags (extra) come first so they're never capped out."""
    out, seen = [], set()
    for t in list(extra) + matched + new_tags:
        k = _norm_tag(t)
        if k and k not in seen:
            seen.add(k)
            out.append(t)
    return out[:MAX_TAGS]


def make_tag(name: str):
    p = TAGS_DIR / f"{name}.md"
    if not p.exists():
        p.write_text("")
        print(f"[Tags] Created: {name}")

# ── Update helpers ────────────────────────────────────────────────────────────

def _merge_tags(existing: str, new_tags: list[str]) -> str:
    """Merge new_tags into the Tags: line of an existing note string."""
    if not new_tags:
        return existing
    m = re.search(r'^(Tags: )(.*?)$', existing, re.MULTILINE)
    if not m:
        return existing
    current_names = set(re.findall(r'\[\[([^\]]+)\]\]', m.group(2)))
    added = [t for t in new_tags if t not in current_names]
    if not added:
        return existing
    merged = m.group(2) + (", " if m.group(2).strip() else "") + \
             ", ".join(f"[[{t}]]" for t in added)
    return existing[:m.start(2)] + merged + existing[m.end(2):]


def _remove_tags(existing: str, drop: list[str]) -> str:
    """Remove tags (by normalized key) from the Tags: line of a note string."""
    if not drop:
        return existing
    m = re.search(r'^(Tags: )(.*?)$', existing, re.MULTILINE)
    if not m:
        return existing
    drop_keys = {_norm_tag(d) for d in drop}
    kept = [t for t in re.findall(r'\[\[([^\]]+)\]\]', m.group(2))
            if _norm_tag(t) not in drop_keys]
    new_line = ", ".join(f"[[{t}]]" for t in kept)
    return existing[:m.start(2)] + new_line + existing[m.end(2):]


def _sidecar_for_out(note_rel: str) -> Path | None:
    """Find the .processed sidecar whose 'out' points at note_rel (vault-relative)."""
    for sc in WATCH_DIR.rglob("*.processed"):
        try:
            if json.loads(sc.read_text()).get("out") == note_rel:
                return sc
        except Exception:
            continue
    return None


def _lines_match(a: list[str], b: list[str]) -> bool:
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        xn = re.sub(r'[^\w\s]', '', x.lower()).strip()
        yn = re.sub(r'[^\w\s]', '', y.lower()).strip()
        if xn != yn:
            return False
    return True


def extract_new_text(current: str, previous: str) -> str:
    """Return only the lines in current that come after previous content ends."""
    if not previous:
        return current
    prev_lines = [l.rstrip() for l in previous.strip().splitlines() if l.strip()]
    curr_lines = [l.rstrip() for l in current.strip().splitlines() if l.strip()]
    if not prev_lines:
        return current
    n = min(3, len(prev_lines))
    anchor = prev_lines[-n:]
    for i in range(len(curr_lines) - n, -1, -1):
        if _lines_match(curr_lines[i:i+n], anchor):
            new_lines = curr_lines[i+n:]
            return '\n'.join(new_lines)
    return current  # fallback: can't locate anchor, return everything


def update_note(out: Path, new_body: str, new_tags: list[str], prev_raw: str | None):
    """
    Update an existing Supernote-generated note.

    Strategy (in order of preference):
    1. SN_END marker present → replace the Supernote body section before the
       marker with the new body; preserve everything after (computer edits).
    2. body_raw known → diff current vs previous, append only new text.
    3. Fallback → append full new body with a timestamp separator.
    """
    existing = out.read_text()
    existing = _merge_tags(existing, new_tags)

    if SN_END in existing:
        before, after = existing.split(SN_END, 1)
        # Replace the supernote section; keep computer edits (after marker)
        # Only keep the header block from before (up to and including the wikilink line)
        header_end = before.find(RAW_LINK)
        if header_end == -1:
            # Can't find wikilink anchor; replace whole before section
            new_content = before.rstrip() + "\n\n" + new_body + "\n" + SN_END + after
        else:
            # Include the wikilink line itself
            wikilink_end = before.find("\n", header_end)
            header = before[:wikilink_end + 1] if wikilink_end != -1 else before
            new_content = header.rstrip() + "\n\n" + new_body + "\n" + SN_END + after
        out.write_text(new_content)
        return

    # No marker: note was created before this fix. Diff or append.
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    sep = "\n\n" + "─" * 40 + "\n"

    if prev_raw:
        new_text = extract_new_text(new_body, prev_raw)
        if not new_text.strip():
            out.write_text(existing)
            return
        out.write_text(existing.rstrip() + sep + f"*{now}*\n\n" + new_text + "\n")
    else:
        # No marker, no prev_raw: safest fallback is to append the whole body.
        # This may duplicate the original supernote content, but preserves
        # any computer edits the user added.
        out.write_text(existing.rstrip() + sep + f"*{now}*\n\n" + new_body + "\n")


# ── Note builder ──────────────────────────────────────────────────────────────

SN_END = "<!-- sn:end -->"   # marks where Supernote body ends; computer edits go after


def _parse_created(text: str) -> str | None:
    """Pull an existing created stamp out of a note so it's preserved when the watcher
    reprocesses/overwrites it. Matches the new frontmatter `created:` line, the old
    `Created:` header, and the legacy bare-date-between-separators format."""
    m = re.search(r'(?im)^\s*created:\s*([0-9]{4}-[0-9]{2}-[0-9]{2}[0-9: ]*)', text)
    if m:
        return m.group(1).strip()
    # legacy header: bare date line between the first two separators
    m = re.search(r'_{20,}\n\s*([0-9]{4}-[0-9]{2}-[0-9]{2}[0-9: ]*)\n_{20,}', text)
    return m.group(1).strip() if m else None


def build_note(title: str, body: str, tags: list[str], note_stem: str,
               created: str | None = None) -> str:
    """Emit a supernote note in Obsidian PROPERTIES format (YAML frontmatter), not the
    legacy underscore-divider header. `type: supernote` lets the janitor/router treat it
    like any typed note; topic tags become linked `topics::` so the graph still connects;
    #supernote/#hermes stay real tags. The `<!-- sn:end -->` sentinel is preserved so the
    reprocess/computer-edit split keeps working."""
    now      = datetime.now().strftime("%Y-%m-%d %H:%M")
    created  = created or now
    raw_link = f"{RAW_LINK}{note_stem}.note]]"
    fm = ["---", "type: supernote",
          f"created: {created}", f"modified: {now}",
          "status: captured",
          "tags:", "  - supernote", "  - hermes"]
    if tags:
        fm.append("topics:")
        fm.extend(f'  - "[[{t}]]"' for t in tags)
    fm.append(f'source: "{raw_link}"')
    fm.append("---")
    heading = f"# {title}\n\n" if title else ""
    return "\n".join(fm) + f"\n{heading}{body}\n{SN_END}\n"


_GENERIC_NAME = re.compile(
    r"^(note|notebook|notes|untitled|new\s*note|page|document|doc|quicknote|"
    r"scratch)\s*[-_ ]*\d*$", re.I)


def _is_generic_name(stem: str) -> bool:
    """True for Supernote default/placeholder notebook names that shouldn't be
    used as a title (so we fall back to the first written line instead)."""
    s = stem.strip()
    if not s or _GENERIC_NAME.match(s):
        return True
    if re.fullmatch(r"[\d\-_ :.]+", s):   # pure date / number / timestamp
        return True
    return False


def derive_title(note: Path, text: str, commands: dict) -> tuple[str, str]:
    """Return (title, body_src). Priority:
    //title command  >  meaningful notebook filename  >  first written line  >  date.
    When the filename supplies the title, the first line stays in the body."""
    lines = text.strip().splitlines()
    first = lines[0].strip() if lines else ""
    first_clean = re.sub(r"^[-*#+>\s]+", "", first).strip()
    if commands.get("title"):
        return commands["title"], text.strip()
    stem = note.stem.strip()
    if not _is_generic_name(stem):
        # Named notebook → it's the title; keep ALL handwriting as the body.
        title = stem.title() if stem.islower() else stem   # preserve acronyms (e.g. "NASA Notes")
        return title, text.strip()
    if first_clean:
        return first_clean.title(), ("\n".join(lines[1:]).strip() if len(lines) > 1 else text)
    return f"Supernote {datetime.now().strftime('%Y-%m-%d %H%M')}", text


# Auto-route new notes into a vault area by tag. First matching group wins
# (order = priority); no match → flat NOTES_DIR (Inbox, triage via links). An
# explicit //file_under always overrides this (see callers).
# Routes come from config (`tag_routes`) so a vault re-organisation is a config
# edit, and a route can never point at a folder name that doesn't exist.
_TAG_ROUTES: list[tuple[str, set[str]]] = mp.tag_routes()


def _route_by_tags(tags: list[str]) -> str | None:
    """Pick a vault area folder from a note's tags, or None for flat (Inbox)."""
    low = {t.lower() for t in (tags or [])}
    for folder, keys in _TAG_ROUTES:
        if low & keys:
            return folder
    return None


def _dest_dir(file_under: str | None) -> Path:
    if not file_under:
        return NOTES_DIR
    p = VAULT / file_under
    p.mkdir(parents=True, exist_ok=True)
    return p


def _save_note(title: str, body: str, tags: list[str], note: Path,
               dest_dir: Path | None = None) -> Path:
    safe    = re.sub(r'[<>:"/\\|?*]', "", title).strip()
    out_dir = dest_dir or NOTES_DIR

    # If a previous version of this note exists (e.g. a pending stub),
    # overwrite it in-place instead of creating a new file — and preserve its
    # original Created date so only Modified advances on reprocessing.
    prev = get_previous_out(note)
    prev_created = _parse_created(_safe_read(prev)) if prev is not None else None
    content = build_note(title, body, tags, note.stem, created=prev_created)
    backlink = f"{RAW_LINK}{note.stem}.note]]"
    if prev is not None:
        out = prev
    else:
        out = out_dir / f"{safe}.md"
        # Defense-in-depth against duplicates: if the sidecar link was lost but a
        # same-titled note already exists AND it carries THIS source note's
        # backlink, it's the same note — reuse it in-place instead of making a
        # dated dupe. Only if it's a DIFFERENT note do we find a unique filename
        # so two distinct notes with the same title never overwrite each other.
        if not (out.exists() and backlink in _safe_read(out)):
            if out.exists():
                date_str = datetime.now().strftime("%Y-%m-%d")
                out = out_dir / f"{safe} {date_str}.md"
            if out.exists():
                stamp = datetime.now().strftime("%Y-%m-%d %H%M")
                out = out_dir / f"{safe} {stamp}.md"
            n = 2
            while out.exists():
                out = out_dir / f"{safe} {stamp}-{n}.md"
                n += 1

    out.write_text(content)
    mark_done(note, out=out)
    return out

# ── Rough Notes: readable handwriting-image copy (the reliable default) ────────

def render_pages_png(nb, stem: str) -> list[str]:
    """Render every inked page of the note to a PNG under Images/Supernote/ and
    return the vault-relative paths (for ![[...]] embeds), in page order.
    Renders WITH the page template (grid/lines) so it looks like the device.
    Skips genuinely-blank pages (bg-hidden ink below INK_RATIO_MIN) but always
    keeps at least one page so a note never renders to nothing."""
    try:
        import numpy as np
        from supernotelib import converter as _conv
        from supernotelib.converter import ImageConverter, VisibilityOverlay
    except Exception as e:
        print(f"[Rough] render import failed: {e}")
        return []
    SN_IMG_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[<>:"/\\|?*]', "", stem).strip() or "note"
    ic = ImageConverter(nb)
    try:
        bg_off = _conv.build_visibility_overlay(background=VisibilityOverlay.INVISIBLE)
    except Exception:
        bg_off = None
    rels: list[str] = []
    n = len(nb.pages)
    for i in range(n):
        try:
            full = ic.convert(i)                       # with template
            ink_img = ic.convert(i, bg_off) if bg_off else full
            a = np.asarray(ink_img.convert("L"))
            ink = float((a < 250).sum()) / a.size
        except Exception as e:
            print(f"[Rough] page {i} render error: {e}")
            continue
        if ink < INK_RATIO_MIN and n > 1:
            continue                                   # blank page in a multi-page note
        name = f"{safe}.png" if n == 1 else f"{safe}-p{len(rels)+1}.png"
        out = SN_IMG_DIR / name
        full.convert("RGB").save(out)
        rels.append(str(out.relative_to(VAULT)))
    return rels


def save_rough_note(title: str, png_rels: list[str], transcript: str | None,
                    note: Path) -> Path | None:
    """Write/overwrite a readable "rough" note that embeds the page image(s)
    and tucks the raw OCR transcript in a collapsible. This is the always-made,
    no-formatting-needed copy the user reads."""
    if not png_rels:
        return None
    ROUGH_DIR.mkdir(parents=True, exist_ok=True)
    safe   = re.sub(r'[<>:"/\\|?*]', "", title).strip() or "note"
    out    = ROUGH_DIR / f"{safe}.md"
    stamp  = datetime.now().strftime("%Y-%m-%d %H:%M")
    pages  = len(png_rels)
    lines  = [
        f"*Handwritten · {stamp} · {pages} page{'s' if pages != 1 else ''}*  "
        f"{RAW_LINK}{note.stem}.note]]",
        "",
    ]
    lines += [f"![[{rel}]]" for rel in png_rels]
    if transcript and transcript.strip():
        lines += ["", "> [!note]- Raw transcript"]
        lines += [f"> {ln}" for ln in transcript.strip().splitlines()]
    lines.append("")
    out.write_text("\n".join(lines))
    return out


def get_previous_rough(note: Path) -> Path | None:
    rel = _read_sidecar(note).get("rough")
    if not rel:
        return None
    p = VAULT / rel
    return p if p.exists() else None


def _wants_format(commands: dict) -> bool:
    """An explicit // command (other than bare formatting helpers) signals the
    user wants the note formatted — skip the y/n ask in that case."""
    if not commands:
        return False
    meaningful = {"ask", "learn", "plan", "remind", "book", "recall", "quiz",
                  "summarize", "infers", "title", "file_under", "extra_tags",
                  "links", "md"}
    return any(commands.get(k) for k in meaningful)


def _write_full_note(note: Path, text: str, commands: dict,
                     title: str, body_src: str) -> tuple[Path, str]:
    """Format `body_src` and write the polished (formatted) note.
    Shared by the pipeline (when a // command implies intent) and the Discord
    'format' reply. Returns (out_path, tag summary string)."""
    fmt  = format_body_rich if commands.get("md") else format_body
    body = fmt(body_src)
    if commands.get("links"):
        body += "\n\n" + "  ".join(commands["links"])
    body += build_command_blocks(commands, note_context=body_src, note_id=title)
    suggested         = infer_tags(title, body)
    matched, new_tags = resolve_tags(suggested, title)
    extra             = commands.get("extra_tags", [])
    all_tags          = finalize_tags(matched, new_tags, extra)
    for t in all_tags:
        make_tag(t)
    dest    = _dest_dir(commands.get("file_under") or _route_by_tags(all_tags))
    out     = _save_note(title, body, all_tags, note, dest)
    tag_str = ", ".join(f"[[{t}]]" for t in all_tags) or "none"
    print(f"[Done] {out.name} (formatted)")
    return out, tag_str


def _sidecar_by_rough(rel: str) -> Path | None:
    """Find the .processed sidecar whose 'rough' points at rel (vault-relative)."""
    for sc in WATCH_DIR.rglob("*.processed"):
        try:
            if json.loads(sc.read_text()).get("rough") == rel:
                return sc
        except Exception:
            continue
    return None


def format_from_rough(rough_rel: str) -> str:
    """Promote a Rough-Notes image copy into a formatted Full Notes entry, on
    demand (Discord 'format' reply). Returns a short human summary."""
    sc = _sidecar_by_rough(rough_rel)
    if sc is None:
        return "⚠️ couldn't find the source note for that rough copy"
    data = json.loads(sc.read_text())
    text = (data.get("body_raw") or "").strip()
    if not text:
        return "⚠️ no transcript to format (try writing it out or //format on the device)"
    note  = WATCH_DIR / f"{sc.stem}.note"
    title = (VAULT / rough_rel).stem
    text2, commands = parse_commands(text)
    out, tag_str = _write_full_note(note, text2, commands, title, text2)
    rough = VAULT / rough_rel
    mark_done(note, out=out, body_raw=text,
              rough=rough if rough.exists() else None, awaiting_format=False)
    rel = out.relative_to(VAULT) if out.is_relative_to(VAULT) else out
    return f"✅ formatted → {rel}\n🏷️ {tag_str}"

# ── Vision OCR stub (RECOGNSTATUS timeout path) ───────────────────────────────

def _process_png_stub(nb, note: Path) -> bool:
    """
    When RECOGNSTATUS is stuck past RECOGN_TIMEOUT, run vision OCR and
    save a full note.  The hash sidecar means it's automatically replaced
    when the device eventually syncs RECOGNSTATUS=1.
    """
    prev_out = get_previous_out(note)
    prev_raw = get_previous_raw(note)
    png_bytes = render_png_bytes(nb)

    text: str | None = None
    if png_bytes:
        print("[Stub] Running vision OCR…")
        text = ocr_with_vision(png_bytes)
        if text:
            print(f"[vision] {len(text)} chars")

    commands: dict = {}
    if text:
        text, commands = parse_commands(text)
        # No-op guard (vision path): a stuck note re-saved with the same
        # handwriting shouldn't re-OCR-overwrite or re-ping. Vision OCR is
        # non-deterministic, so compare by similarity, not equality.
        if prev_out and prev_raw is not None and \
                _text_similarity(text, prev_raw) >= NOOP_SIMILARITY:
            print(f"[NoOp] {note.name} re-saved (vision), text unchanged — keeping note")
            mark_done(note, out=prev_out, body_raw=prev_raw)
            _clear_seen(note)
            return False
    # ping only once we know there's real new content to work on
    notify_received(note.stem, via="reading your handwriting")  # ping 1 of 2
    if text:
        title, body_raw = derive_title(note, text, commands)
        fmt      = format_body_rich if commands.get("md") else format_body
        body     = fmt(body_raw)

        if commands.get("links"):
            body += "\n\n" + "  ".join(commands["links"])
        # Run // commands on the vision path too (previously dropped here).
        body += build_command_blocks(commands, note_context=body_raw, note_id=title)

        suggested         = infer_tags(title, body)
        matched, new_tags = resolve_tags(suggested, title)
        extra             = commands.get("extra_tags", [])
        all_tags          = finalize_tags(matched, new_tags, extra)
        for t in all_tags:
            make_tag(t)
        via      = "vision OCR"
    else:
        title    = note.stem.title()
        body     = "_Recognition pending — will be replaced when device syncs._"
        all_tags = []
        via      = "image only"

    dest = _dest_dir(commands.get("file_under") or _route_by_tags(all_tags))
    out  = _save_note(title, body, all_tags, note, dest)
    # Record body_raw so the no-op guard can recognize a later device re-save of
    # the SAME handwriting (vision OCR ≈ device OCR) and not re-ping / overwrite.
    if text:
        mark_done(note, out=out, body_raw=text)
    _clear_seen(note)
    print(f"[Stub] {out.name} ({via})")

    rel = out.relative_to(VAULT) if out.is_relative_to(VAULT) else out
    tag_str = ", ".join(f"[[{t}]]" for t in all_tags) or "none"
    send_discord(
        f"⏳ Note: {title}\n"
        f"🏷️ Tags: {tag_str}\n"
        f"📁 Filed: {rel} ({via} — recognition pending)",
        note_path=str(rel),
        note_title=title,
        mood=("done" if via == "vision OCR" else None),
    )
    return True

# ── First-seen tracker (for RECOGNSTATUS timeout) ─────────────────────────────

_first_seen: dict[str, float] = {}


def _note_wait_seconds(note: Path) -> float:
    key = str(note)
    _first_seen.setdefault(key, time.time())
    return time.time() - _first_seen[key]


def _clear_seen(note: Path):
    _first_seen.pop(str(note), None)

# ── Core processor ────────────────────────────────────────────────────────────

def process(note: Path) -> bool:
    print(f"[Process] {note.name}")
    nb = _load(note)

    # Re-check is_done after load (another instance may have locked it)
    if is_done(note):
        print(f"[Skip] {note.name} done by another instance")
        return False

    # Wait for the device to finish recognition on all pages
    if not all_pages_ready(nb):
        waited   = _note_wait_seconds(note)
        statuses = [p.metadata.get("RECOGNSTATUS", "0") for p in nb.pages]
        if waited < RECOGN_TIMEOUT:
            print(f"[Skip] RECOGNSTATUS={statuses} — waited {waited:.0f}s/{RECOGN_TIMEOUT}s")
            return False
        print(f"[Timeout] {note.name} stuck at {statuses} for {waited:.0f}s — vision OCR")
        mark_in_progress(note)   # lock before slow OCR
        return _process_png_stub(nb, note)

    _clear_seen(note)

    text = extract_text(nb)
    if not text:
        n = inc_fail(note)
        if n >= MAX_FAILS:
            # RECOGNSTATUS=1 but the device gave us no text. If there's still
            # INK on the page and we haven't already converted this note, the
            # device just couldn't linearize it (long division, diagrams, dense
            # math) — fall back to vision OCR instead of silently dropping it.
            # That silent drop is exactly why "wrote long division, nothing
            # happened" was once reported. Only give up for real if the page is blank.
            if _has_ink(nb) and not get_previous_out(note):
                print(f"[GiveUp→Vision] RECOGNSTATUS=1 but no text after {n} tries, "
                      f"ink present — falling back to vision OCR")
                clear_fails(note)
                mark_in_progress(note)   # lock before slow OCR
                return _process_png_stub(nb, note)
            print(f"[GiveUp] no text and no ink after {n} tries — note synced empty")
            # The note arrived with NO handwriting (only the page template) —
            # Syncthing grabbed it before the device flushed ink to the saved
            # file. One-time heads-up so an empty sync isn't a silent black hole
            # (this is exactly the "wrote long division, nothing happened"
            # case). Dedup via a sidecar flag so we don't ping on every re-save.
            sc = _read_sidecar(note)
            notified = False
            if not get_previous_out(note) and not sc.get("noink_notified"):
                send_discord(
                    f"📭 Got **{note.stem}** but it synced with no ink yet — "
                    f"close the notebook (or turn the page) on your Supernote to "
                    f"flush the writing, and I'll process it automatically.")
                notified = True
            # PRESERVE any prior output link + raw text. A bare mark_done(note)
            # here erased them, so a later real re-sync saw get_previous_out()==None
            # and created a dated DUPLICATE instead of updating the existing note.
            mark_done(note, out=get_previous_out(note),
                      body_raw=get_previous_raw(note))
            if notified:
                d = _read_sidecar(note); d["noink_notified"] = True
                sidecar(note).write_text(json.dumps(d))
            clear_fails(note)
        else:
            print(f"[Retry] RECOGNSTATUS=1 but no text (attempt {n}/{MAX_FAILS})")
        return False

    clear_fails(note)
    print(f"[Text] {len(text)} chars")

    # Parse // commands (and legacy Hermes: commands)
    text, commands = parse_commands(text)
    if commands:
        print(f"[//] {commands}")

    prev_out   = get_previous_out(note)     # Full Notes entry (only after formatting)
    prev_rough = get_previous_rough(note)   # Rough Notes image copy (the default)
    prev_raw   = get_previous_raw(note)

    # No-op re-save guard: the device (or Syncthing) re-saved an existing
    # notebook whose HANDWRITING is unchanged — the raw .note bytes changed (so
    # is_done was False) but the recognized text is the same or only differs in
    # OCR noise. This covers: (1) identical re-save (an old notebook re-pinging
    # when a new note was written); (2) the device finishing recognition AFTER a vision-OCR
    # run and producing a near-identical variant. Applies whether the note is a
    # formatted Full Note OR just a Rough image copy — either way, don't re-ping.
    if (prev_out or prev_rough) and prev_raw is not None:
        sim = _text_similarity(text, prev_raw)
        if sim >= NOOP_SIMILARITY:
            print(f"[NoOp] {note.name} re-saved, text {sim:.0%} same — keeping existing")
            mark_done(note, body_raw=prev_raw)   # just refresh the hash
            return False

    mark_in_progress(note)   # lock before slow AI calls
    notify_received(note.stem)   # ping 1 of 2: "received, working on it"

    # Determine title: //title > meaningful filename > first line > date
    title, body_src = derive_title(note, text, commands)

    if prev_out is not None:
        # UPDATE PATH — note already exists; preserve computer edits.
        # If the note has the SN_END marker, the section before it is fully
        # managed by the pipeline: reformat the entire current handwriting and
        # replace that section (computer edits after the marker are untouched).
        # Legacy notes (no marker) can't have their boundary identified, so we
        # diff against the previous raw text and append only what's new.
        existing   = prev_out.read_text()
        has_marker = SN_END in existing

        if has_marker:
            section_src = body_src                                   # full handwriting
        else:
            section_src = extract_new_text(text, prev_raw) if prev_raw else body_src

        fmt      = format_body_rich if commands.get("md") else format_body
        new_body = fmt(section_src) if section_src.strip() else ""
        if commands.get("links"):
            new_body += "\n\n" + "  ".join(commands["links"])
        new_body += build_command_blocks(commands, note_context=section_src,
                                         note_id=prev_out.stem)

        suggested         = infer_tags(prev_out.stem, new_body) if new_body else []
        matched, new_tags = resolve_tags(suggested, prev_out.stem)
        extra             = commands.get("extra_tags", [])
        all_new_tags      = finalize_tags(matched, new_tags, extra)
        for t in all_new_tags:
            make_tag(t)

        if new_body.strip():
            update_note(prev_out, new_body, all_new_tags, prev_raw)
            label = "✅ Note (updated)"
            tag_str = ", ".join(f"[[{t}]]" for t in all_new_tags) or "none"
            print(f"[Done] {prev_out.name} (updated)")
        else:
            label = "✅ Note (no new text)"
            tag_str = "none"
            print(f"[Done] {prev_out.name} (no new text)")

        out = prev_out
        # keep the Rough image copy in sync with the latest handwriting too
        new_rough = save_rough_note(title, render_pages_png(nb, note.stem), text, note)
        mark_done(note, out=out, body_raw=text, rough=new_rough or prev_rough)

        _clear_seen(note)
        rel = out.relative_to(VAULT) if out.is_relative_to(VAULT) else out
        send_discord(
            f"{label}: {title}\n🏷️ Tags: {tag_str}\n📁 Filed: {rel}",
            note_path=str(rel), note_title=title,
            mood=("updated" if "updated" in label else "done"),
        )
        return True

    # ── FIRST TIME ────────────────────────────────────────────────────────────
    # Default: always save a READABLE image copy to Rough Notes (reliable — no
    # OCR needed). Formatting into Full Notes is opt-in: only auto-run when a //
    # command signals intent; otherwise ask on Discord ("format? reply y").
    png_rels = render_pages_png(nb, note.stem)
    rough    = save_rough_note(title, png_rels, text, note)

    if _wants_format(commands) or rough is None:
        # explicit command (or rendering failed → don't lose the note): format now
        out, tag_str = _write_full_note(note, text, commands, title, body_src)
        mark_done(note, out=out, body_raw=text, rough=rough, awaiting_format=False)
        _clear_seen(note)
        rel = out.relative_to(VAULT) if out.is_relative_to(VAULT) else out
        send_discord(
            f"✅ Note: {title}\n🏷️ Tags: {tag_str}\n📁 Filed: {rel}",
            note_path=str(rel), note_title=title, mood="done",
        )
        return True

    # default path → rough copy saved, ask whether to format
    mark_done(note, body_raw=text, rough=rough, awaiting_format=True)
    _clear_seen(note)
    rrel  = rough.relative_to(VAULT)
    npages = len(png_rels)
    hint   = "" if len((text or "").split()) >= 12 else " _(looks sparse/visual — maybe leave it?)_"
    send_discord(
        f"📝 Saved **{title}** to Rough Notes ({npages} page{'s' if npages != 1 else ''}, "
        f"readable as-is).{hint}\nFormat it into Full Notes? reply **format** (or **y**).",
        note_path=str(rrel), note_title=title, mood=None,
    )
    return True

# ── Discord reply feedback loop ───────────────────────────────────────────────
# Reply to a note's Discord notification to refine it: change tags, retitle,
# teach a style rule, save a memory, or append a thought. Only DISCORD_ALLOWED_USERS
# can issue feedback; replies must reference one of our notification messages.

FEEDBACK_SEEN  = mp.state_path("feedback_seen.json")
DISCORD_API    = mp.DISCORD_API
FEEDBACK_INTERVAL = 30   # seconds between Discord reply polls (separate from POLL_INTERVAL)

_last_feedback_poll = 0.0


def _discord_creds() -> dict:
    """Token, home channel and allowed user IDs — from env / .env via sn_config."""
    return mp.discord_creds()


def _discord_headers() -> dict:
    return mp.discord_headers()


def _load_seen() -> set:
    if FEEDBACK_SEEN.exists():
        try:
            return set(json.loads(FEEDBACK_SEEN.read_text()))
        except Exception:
            return set()
    return set()


def _save_seen(seen: set):
    # keep it bounded
    FEEDBACK_SEEN.write_text(json.dumps(sorted(seen)[-2000:]))


def _pending_by_msgid() -> dict:
    """Map notification message_id → pending metadata dict."""
    out = {}
    for f in PENDING_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text())
            mid = d.get("thread_id") or f.stem
            out[str(mid)] = d
        except Exception:
            continue
    return out


def _discord_react(msg_id: str, emoji: str = "✅"):
    chan = _discord_creds()["channel"]
    try:
        requests.put(f"{DISCORD_API}/channels/{chan}/messages/{msg_id}/reactions/{emoji}/@me",
                     headers=_discord_headers(), timeout=10)
    except Exception as e:
        print(f"[Feedback] react failed: {e}")


def _discord_reply(reply_to_id: str, text: str):
    chan = _discord_creds()["channel"]
    try:
        requests.post(f"{DISCORD_API}/channels/{chan}/messages",
                      headers=_discord_headers(),
                      json={"content": text[:1900],
                            "message_reference": {"message_id": reply_to_id},
                            "allowed_mentions": {"parse": []}},
                      timeout=10)
    except Exception as e:
        print(f"[Feedback] reply failed: {e}")


def _interpret_feedback_llm(text: str, current_tags: list[str]) -> dict | None:
    """Use the local LLM to turn a natural-language reply into structured actions.
    Returns a dict with keys add_tags/del_tags/retitle/file_under/style_rules/
    memories/append, or None if interpretation fails."""
    prompt = (
        "You convert a short Discord reply about a note into a JSON action list. "
        "The reply is feedback from the note's owner about how to fix/refine the note.\n"
        f"The note currently has these tags: {current_tags}\n\n"
        "Return ONLY a JSON object with these optional keys:\n"
        '  "add_tags": [tag names to add],\n'
        '  "del_tags": [tag names to remove],\n'
        '  "retitle": "new title" (only if they clearly want a rename),\n'
        '  "file_under": "vault/path" (only if they say to file/move it),\n'
        '  "style_rules": [formatting preferences to apply to FUTURE notes],\n'
        '  "memories": [durable facts about the user worth remembering],\n'
        '  "append": "a thought to append to this note" (use for general comments).\n'
        "Omit keys that don't apply. Tag names use Title Case. "
        "Interpret natural language: 'gardening can be removed' -> del_tags:['Gardening']; "
        "'add a food tag' -> add_tags:['Food']; 'looks good' -> {}.\n\n"
        f"Reply: {text}"
    )
    try:
        r = requests.post(
            OLLAMA_GENERATE,
            json={"model": ASK_MODEL, "stream": False,
                  "options": {"temperature": 0.1, "think": False}, "prompt": prompt},
            timeout=120,
        )
        raw = r.json().get("response", "")
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return None
        data = json.loads(m.group())
        return data if isinstance(data, dict) else None
    except Exception as e:
        print(f"[Feedback] LLM interpret failed: {e}")
        return None


def apply_feedback(text: str, note_rel: str, title: str) -> str:
    """Apply a (natural-language) reply to the note / style file / memory.
    Returns a short human summary of what was done."""
    out_path = VAULT / note_rel
    if not out_path.exists():
        return f"⚠️ note not found: {note_rel}"

    # strip leading @mentions (Discord adds these when replying to the bot)
    text = re.sub(r"<@!?\d+>", "", text).strip()

    # Opt-in formatting: if this is a Rough Notes copy awaiting a y/n answer,
    # interpret a yes/no reply as "format into Full Notes" / "leave it".
    rough_sc = _sidecar_by_rough(note_rel)
    if rough_sc is not None:
        try:
            awaiting = json.loads(rough_sc.read_text()).get("awaiting_format")
        except Exception:
            awaiting = False
        if awaiting:
            low = text.lower().strip(" .!?")
            if re.fullmatch(r"(y|yes|ya|yeah|yep|format|do it|fmt|sure|please(?: format)?|go|ok|okay)", low):
                return format_from_rough(note_rel)
            if re.fullmatch(r"(n|no|nope|nah|skip|leave it|don'?t|no thanks)", low):
                d = json.loads(rough_sc.read_text()); d["awaiting_format"] = False
                rough_sc.write_text(json.dumps(d))
                return "👍 left it as a Rough note (no formatting)"
            # otherwise fall through: treat as normal feedback on the rough note

    content_now = out_path.read_text()
    current_tags = re.findall(r'\[\[([^\]]+)\]\]',
                              re.search(r'^Tags:(.*)$', content_now, re.M).group(1)
                              if re.search(r'^Tags:(.*)$', content_now, re.M) else "")

    add_tags, del_tags = [], []
    new_title = None
    file_under = None
    style_rules, memories, freeform = [], [], []

    # Primary: LLM interprets natural language into structured actions
    data = _interpret_feedback_llm(text, current_tags)
    if data:
        add_tags   = [t.strip() for t in data.get("add_tags", []) if t.strip()]
        del_tags   = [t.strip() for t in data.get("del_tags", []) if t.strip()]
        new_title  = (data.get("retitle") or "").strip() or None
        file_under = (data.get("file_under") or "").strip() or None
        style_rules = [s.strip() for s in data.get("style_rules", []) if s.strip()]
        memories    = [s.strip() for s in data.get("memories", []) if s.strip()]
        ap = (data.get("append") or "").strip()
        if ap:
            freeform = [ap]
    else:
        # Fallback: strict line-based command grammar
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if (mm := re.match(r"(?:tags?\s*(?:as|:)|\+tag\s*:?)\s*(.+)", line, re.I)):
                add_tags += [t.strip() for t in re.split(r"[,;]", mm.group(1)) if t.strip()]
            elif (mm := re.match(r"(?:untag|-tag|remove tag)\s*:?\s*(.+)", line, re.I)):
                del_tags += [t.strip() for t in re.split(r"[,;]", mm.group(1)) if t.strip()]
            elif (mm := re.match(r"(?:retitle|title)\s*:?\s*(.+)", line, re.I)):
                new_title = mm.group(1).strip()
            elif (mm := re.match(r"file under\s*:?\s*(.+)", line, re.I)):
                file_under = mm.group(1).strip()
            elif (mm := re.match(r"style\s*:?\s*(.+)", line, re.I)):
                style_rules.append(mm.group(1).strip())
            elif (mm := re.match(r"(?:remember|memory|note to self)\s*:?\s*(.+)", line, re.I)):
                memories.append(mm.group(1).strip())
            elif (mm := re.match(r"(?:note|add)\s*:?\s*(.+)", line, re.I)):
                freeform.append(mm.group(1).strip())
            else:
                freeform.append(line)

    done = []
    content = out_path.read_text()

    # tags
    for t in add_tags:
        make_tag(t)
    if add_tags:
        content = _merge_tags(content, add_tags); done.append(f"+tags {', '.join(add_tags)}")
    if del_tags:
        content = _remove_tags(content, del_tags); done.append(f"-tags {', '.join(del_tags)}")

    # freeform thought → append after the marker (computer-edit zone, survives resync)
    if freeform:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        block = f"\n\n> 💬 *Discord note ({stamp}):* " + " ".join(freeform)
        if SN_END in content:
            head, tail = content.split(SN_END, 1)
            content = head + SN_END + tail.rstrip() + block + "\n"
        else:
            content = content.rstrip() + block + "\n"
        done.append("appended note")

    out_path.write_text(content)

    # retitle → rename file + fix sidecar + pending refs
    if new_title:
        safe = re.sub(r'[<>:"/\\|?*]', "", new_title).strip()
        new_path = out_path.with_name(safe + ".md")
        if new_path != out_path and not new_path.exists():
            out_path.rename(new_path)
            new_rel = str(new_path.relative_to(VAULT))
            sc = _sidecar_for_out(note_rel)
            if sc:
                d = json.loads(sc.read_text()); d["out"] = new_rel; sc.write_text(json.dumps(d))
            note_rel = new_rel
            done.append(f"retitled → {safe}")

    # move
    if file_under:
        dest_dir = VAULT / file_under
        dest_dir.mkdir(parents=True, exist_ok=True)
        cur = VAULT / note_rel
        dest = dest_dir / cur.name
        if cur.exists() and dest != cur and not dest.exists():
            cur.rename(dest)
            new_rel = str(dest.relative_to(VAULT))
            sc = _sidecar_for_out(note_rel)
            if sc:
                d = json.loads(sc.read_text()); d["out"] = new_rel; sc.write_text(json.dumps(d))
            done.append(f"filed under {file_under}")

    # style learning → append rule to the style file (influences future formatting)
    if style_rules:
        cur = STYLE_FILE.read_text() if STYLE_FILE.exists() else _DEFAULT_STYLE
        cur = cur.rstrip() + "\n" + "\n".join(f"- {r}" for r in style_rules) + "\n"
        STYLE_FILE.write_text(cur)
        done.append(f"learned {len(style_rules)} style rule(s)")

    # memory learning → append to the agent's user-memory file (config: state.memory_file;
    # point it at an agent's own memory, e.g. Hermes' memories/USER.md, to share it)
    if memories:
        user_md = mp.memory_file()
        existing = user_md.read_text().rstrip() if user_md.exists() else ""
        for fact in memories:
            existing += ("\n§\n" if existing else "") + fact
        user_md.parent.mkdir(parents=True, exist_ok=True)
        user_md.write_text(existing + "\n")
        done.append(f"remembered {len(memories)} fact(s)")

    return "✅ " + "; ".join(done) if done else "🤔 nothing actionable found"


def poll_discord_feedback():
    """Check the home channel for replies to note notifications and apply them."""
    global _last_feedback_poll
    now = time.time()
    if now - _last_feedback_poll < FEEDBACK_INTERVAL:
        return
    _last_feedback_poll = now

    creds = _discord_creds()
    if not (creds["token"] and creds["channel"]):
        return

    pending = _pending_by_msgid()
    if not pending:
        return

    try:
        r = requests.get(f"{DISCORD_API}/channels/{creds['channel']}/messages?limit=50",
                         headers=_discord_headers(), timeout=15)
        msgs = r.json()
        if not isinstance(msgs, list):
            return
    except Exception as e:
        print(f"[Feedback] fetch failed: {e}")
        return

    seen = _load_seen()
    changed = False
    for m in msgs:
        mid = str(m.get("id"))
        ref = m.get("message_reference") or {}
        ref_id = str(ref.get("message_id", ""))
        author = m.get("author", {})
        if mid in seen:
            continue
        if not ref_id or ref_id not in pending:
            continue
        if author.get("bot"):
            continue
        # security: only allowed users may steer notes
        if creds["allowed"] and str(author.get("id")) not in creds["allowed"]:
            print(f"[Feedback] ignoring reply from non-allowed user {author.get('username')}")
            seen.add(mid); changed = True
            continue
        meta = pending[ref_id]
        note_rel = meta.get("note_path", "")
        title = meta.get("note_title", "")
        content = m.get("content", "")
        print(f"[Feedback] reply on {note_rel!r}: {content[:60]!r}")
        summary = apply_feedback(content, note_rel, title)
        print(f"[Feedback] {summary}")
        _discord_react(mid, "✅")
        _discord_reply(mid, summary)
        seen.add(mid); changed = True

    if changed:
        _save_seen(seen)


# ── PDF mirror poll ───────────────────────────────────────────────────────────

PDF_OUTPUT   = mp.pdf_output()
# Which notes become PDFs + their flat output dir is decided by pdf_out_subdir()
# in vault_to_pdf.py (single source of truth, imported in poll_vault_pdfs).
PDF_MAX_SRC_BYTES = 300_000   # skip transcript/session-log dumps — real notes are a few KB
PDF_COOLDOWN  = 600           # min seconds between reconversions of the SAME file
_pdf_last_conv = {}           # rel-path str -> last conversion epoch (regeneration backoff)


def poll_vault_pdfs():
    """Convert new/modified .md files to PDFs for Supernote reading."""
    sys.path.insert(0, str(Path(__file__).parent))
    try:
        from vault_to_pdf import convert_file, pdf_out_subdir
    except ImportError as e:
        print(f"[PDFs] Cannot import vault_to_pdf: {e}")
        return

    now = time.time()
    for md in sorted(VAULT.rglob("*.md")):
        rel = md.relative_to(VAULT)
        sub = pdf_out_subdir(rel)
        if sub is None:
            continue
        st = md.stat()
        if st.st_size < 50:
            continue
        # skip transcript/session-log dumps — they thrash the converter and
        # aren't something you'd stylus-annotate on the Supernote anyway
        if st.st_size > PDF_MAX_SRC_BYTES:
            continue
        # skip files still being written
        if now - st.st_mtime < STABLE_AGE:
            continue
        out_dir = PDF_OUTPUT / sub
        out_pdf = out_dir / (md.stem + ".pdf")
        # skip if PDF is current
        if out_pdf.exists() and out_pdf.stat().st_mtime >= st.st_mtime:
            continue
        # regeneration backoff: a file being actively edited (Obsidian autosave,
        # Syncthing re-touch) advances mtime every cycle — don't reconvert it
        # more than once per PDF_COOLDOWN even though it "changed"
        key = str(rel)
        if now - _pdf_last_conv.get(key, 0) < PDF_COOLDOWN:
            continue
        print(f"[PDFs] {rel}")
        try:
            convert_file(md, out_dir)
            _pdf_last_conv[key] = now
        except Exception as e:
            print(f"[PDFs] ✗ {rel}: {e}")
            _pdf_last_conv[key] = now   # back off on failure too — no tight retry loop

# ── Poll loop ─────────────────────────────────────────────────────────────────

def _all_notes() -> list[Path]:
    """Every .note under Raw, INCLUDING subfolders — the device can sync notes
    into category subfolders (e.g. 'Programming'), and those must be processed
    too, not silently ignored. Skips Syncthing internals."""
    return sorted(n for n in WATCH_DIR.rglob("*.note")
                  if not any(part.startswith(".st") for part in n.parts))


def poll():
    now = time.time()
    for note in _all_notes():
        if is_done(note):
            continue
        if note.stat().st_size < 200:
            continue
        age = now - note.stat().st_mtime
        if age < STABLE_AGE:
            print(f"[Poll] {note.name} still syncing ({age:.0f}s < {STABLE_AGE}s)")
            continue
        print(f"\n[Poll] {note.name} (age {age:.0f}s)")
        try:
            process(note)
        except Exception as e:
            print(f"[Poll] Error on {note.name}: {e}")


def wait_for_network(timeout: int = 60):
    """Block until DNS resolves (or timeout). systemd USER services don't have a
    real network-online.target, so on boot the watcher can start before the
    system network is up — the first Discord notification/feedback fetch then
    dies with 'Failed to resolve discord.com'. A short startup gate fixes it."""
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.getaddrinfo("discord.com", 443)
            return True
        except OSError:
            time.sleep(2)
    print(f"[Watcher] network not ready after {timeout}s — starting anyway")
    return False


def main():
    print(f"[Watcher] {WATCH_DIR}")
    print(f"[Watcher] poll={POLL_INTERVAL}s  stable={STABLE_AGE}s\n")
    wait_for_network()  # avoid boot-time DNS race (user services lack network-online)
    load_style()  # ensure style file exists on startup
    while True:
        try:
            poll()
            poll_discord_feedback()
            poll_vault_pdfs()
        except Exception as e:
            print(f"[Main] {e}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
