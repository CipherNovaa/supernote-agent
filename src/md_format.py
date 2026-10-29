#!/usr/bin/env python3
"""
md_format.py — shared note-formatting engine.

Single source of truth for the two markdown formatters used across the system:

  * format_body(text)       — conservative: fix OCR, nudge headings, preserve
                              everything, add NO structure. Run on every note.
  * format_body_rich(text)  — opinionated `//md` reformat: restructure the note
                              the way the author would type it (headings,
                              sections, bullet/numbered lists, emphasis), while
                              never inventing content. Falls back to format_body
                              if it would drop too much text.

Imported by the Supernote watcher (and usable from an agent plugin or the
CLI), so the formatting logic lives in exactly one place. Fully local
(qwen3:8b via Ollama by default).

Both prompts are assembled from three learned/derived inputs:
  * the style file (style rules appended over time by the Discord feedback loop)
  * two random few-shot examples drawn from the user's own well-formatted notes
  * the OCR-correction glossary (ocr_corrections.py), relevance-ranked

CLI:
    md_format.py [--rich] "raw note text"     # default = conservative
    echo "raw text" | md_format.py --rich
"""

import re
import sys
import random
import requests
from pathlib import Path

# ── Config (paths from the single source of truth: sn_config) ─────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sn_config as mp

VAULT      = mp.vault_root()
NOTES_DIR  = mp.examples_dir()   # few-shot example source
STYLE_FILE = mp.style_file()
RAW_LINK   = f"[[{mp.rel('supernote_raw')}/"

OLLAMA_URL = mp.OLLAMA_GENERATE
MODEL      = mp.TEXT_MODEL   # qwen3:8b fits 8GB VRAM

# Learned OCR-correction glossary (the user's handwriting quirks). Imported
# defensively: if the engine is missing/broken, formatting must still work,
# just without the learned hints. The path insert lets it resolve even when
# md_format is imported from another working dir (e.g. an agent plugin).
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import ocr_corrections
except Exception as _e:                       # pragma: no cover
    ocr_corrections = None
    print(f"[format] correction glossary unavailable: {_e}")


def _glossary(text: str) -> str:
    """Return a formatted glossary block for the prompt, or '' if unavailable."""
    if ocr_corrections is None:
        return ""
    try:
        g = ocr_corrections.glossary_prompt(text)
    except Exception as e:                    # pragma: no cover — defensive
        print(f"[format] glossary error: {e}")
        return ""
    return f"\n{g}\n" if g else ""

# Short fallback only — the watcher writes the full default on startup, so this
# is used solely if the style file is somehow missing entirely.
_FALLBACK_STYLE = """\
# Supernote Note Style Rules
- Use bullet points with `-` not `*`
- Use ==highlight== for key terms and definitions
- Keep the author's voice — clean up OCR artefacts but don't rewrite
- Preserve all equations and structure in math notes
"""


def load_style() -> str:
    if not STYLE_FILE.exists():
        return _FALLBACK_STYLE.strip()
    return STYLE_FILE.read_text().strip()


def _example_body(text: str) -> str:
    """Extract just the clean body prose from a note for use as a style example.
    Strips the header block, the wikilink, the SN_END marker, and any
    post-marker computer edits — so none of that can leak into AI output."""
    text = text.split("<!-- sn:end -->", 1)[0]
    m = re.search(re.escape(RAW_LINK) + r"[^\]]+\]\]\s*", text)
    if m:
        text = text[m.end():]
    else:
        text = re.sub(r"^(?:_{20,}\n.*?\n)+", "", text, flags=re.DOTALL)
    return text.strip()


def sample_example_notes(n: int = 2) -> str:
    """Pick n random notes from the examples dir as few-shot formatting examples."""
    if not NOTES_DIR.exists():
        return ""
    notes = list(NOTES_DIR.rglob("*.md"))
    if not notes:
        return ""
    chosen = random.sample(notes, min(n, len(notes)))
    parts = []
    for p in chosen:
        body = _example_body(p.read_text())[:600]
        if body:
            parts.append(f"=== {p.stem} ===\n{body}")
    return "\n\n".join(parts)


def _wc(s: str) -> int:
    """Rough word count for the content-loss guard."""
    return len(re.findall(r"\w+", s))


def format_body(text: str) -> str:
    """
    Format raw transcribed text using qwen3:8b.
    The style file provides background reference; few-shot examples show
    the target markdown style.  The model must not add or invent content.
    """
    style    = load_style()
    examples = sample_example_notes(2)

    prompt = (
        "You are a note formatter. Reformat the raw handwritten-note "
        "transcription below for an Obsidian vault.\n\n"
        "Rules (follow exactly):\n"
        "1. Fix OCR errors: wrong spacing, misread characters, broken line breaks.\n"
        "2. A line starting with `#` → change to `## Heading` (add one `#`).\n"
        "3. Lines starting with `-` → keep as markdown list items.\n"
        "4. Preserve ALL existing markdown EXACTLY as written — do NOT add, "
        "remove, or alter these: headings (`#`), **bold**, *italics*, "
        "==highlight==, ~~strikethrough~~, checkboxes (`- [ ]`, `- [x]`), "
        "blockquotes (`>`), dividers (`***`), inline code, math (`$...$` and "
        "`$$...$$`). Leave LaTeX inside dollar signs untouched.\n"
        "5. Do NOT add Tags, metadata, headings, or any content not in the original.\n"
        "6. Return ONLY the formatted body text. No explanation, no preamble.\n"
    )
    if examples:
        prompt += f"\nStyle reference (target markdown style — do not copy content):\n{examples}\n"
    prompt += _glossary(text)
    prompt += f"\nRaw text to format:\n{text}"

    try:
        r = requests.post(
            OLLAMA_URL,
            json={
                "model": MODEL,
                "stream": False,
                "options": {"temperature": 0.1, "think": False},
                "prompt": prompt,
            },
            timeout=180,
        )
        raw = r.json().get("response", "").strip()
        # Strip any leftover thinking blocks and spurious metadata lines
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
        raw = re.sub(r"\s*/think\s*$", "", raw, flags=re.MULTILINE).strip()
        raw = re.sub(r"\n+Tags:.*$", "", raw, flags=re.DOTALL).strip()
        # Defensive: strip any marker or header separators the model may have
        # copied from few-shot examples, so they can't corrupt the note.
        raw = raw.replace("<!-- sn:end -->", "")
        raw = re.sub(r"^_{20,}$", "", raw, flags=re.MULTILINE).strip()
        return raw or text
    except Exception as e:
        print(f"[Format] {e}")
        return text


def format_body_rich(text: str) -> str:
    """
    Opinionated reformat (the `//md` command). Unlike format_body (which only
    fixes OCR and nudges headings), this actively *restructures* the note the
    way the author would if they'd typed it on a computer: promote natural headings,
    group related lines under sections, turn run-on lists into real bullet
    lists, emphasise key terms, tidy paragraphs. It still must NOT invent any
    content — only re-present what was written. Learns the author's style over
    time from the style file + few-shot examples of their real notes.

    Safety: if the model returns far less text than it was given (likely
    dropped content), fall back to the conservative formatter so nothing is
    silently lost.
    """
    style    = load_style()
    examples = sample_example_notes(2)

    prompt = (
        "You are the author's note editor. Rewrite the raw handwritten-note "
        "transcription below into clean, well-structured Obsidian markdown — "
        "exactly as the author would format it if they had typed it on a computer.\n\n"
        "You MAY restructure for clarity:\n"
        "- Promote natural section titles to `## Headings` (and `###` subheads).\n"
        "- Group related lines under the heading they belong to.\n"
        "- Turn run-on or dash-separated items into proper `-` bullet lists; "
        "use `1.` numbered lists for ordered steps.\n"
        "- Use **bold** for key terms and wrap the single most important "
        "takeaway in ==double equals== so Obsidian highlights it (e.g. "
        "==full sun==). Never write the literal word 'highlight'.\n"
        "- Tidy paragraphs, spacing, and line breaks; fix OCR errors.\n\n"
        "Hard rules (never break):\n"
        "1. Do NOT invent, add, or remove any facts, ideas, or content. Only "
        "re-present what the author actually wrote. Do NOT append explanatory "
        "phrases, benefits, rationales, or descriptions they did not write "
        "(e.g. do not turn 'saves water' into 'efficient for hydration'). "
        "If unsure, keep their original wording.\n"
        "2. Preserve ALL math (`$...$`, `$$...$$`), code, checkboxes "
        "(`- [ ]`/`- [x]`), and existing emphasis exactly.\n"
        "3. Do NOT add a Tags line, frontmatter, metadata, a date, "
        "[[wikilinks]], or any commentary about what you changed (tagging and "
        "linking are handled elsewhere).\n"
        "4. Return ONLY the formatted note body. No preamble, no explanation.\n"
    )
    prompt += f"\nThe author's formatting style (reference):\n{style[:1500]}\n"
    if examples:
        prompt += (f"\nExamples of the author's real notes (target style — do NOT copy "
                   f"their content):\n{examples}\n")
    prompt += _glossary(text)
    prompt += f"\nRaw note to reformat:\n{text}"

    try:
        r = requests.post(
            OLLAMA_URL,
            json={
                "model": MODEL,
                "stream": False,
                "options": {"temperature": 0.15, "think": False},
                "prompt": prompt,
            },
            timeout=240,
        )
        raw = r.json().get("response", "").strip()
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
        raw = re.sub(r"\s*/think\s*$", "", raw, flags=re.MULTILINE).strip()
        raw = re.sub(r"\n+Tags:.*$", "", raw, flags=re.DOTALL).strip()
        raw = raw.replace("<!-- sn:end -->", "")
        raw = re.sub(r"^_{20,}$", "", raw, flags=re.MULTILINE).strip()
        # qwen sometimes emits the literal placeholder ==highlight== instead of
        # wrapping a real term; drop those stray tokens so they don't corrupt
        # the note (a bare one, or a same-line pair around content).
        raw = re.sub(r"==highlight==\s*(.*?)\s*==highlight==", r"\1", raw)
        raw = re.sub(r"^\s*==highlight==\s*$", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"==highlight==", "", raw).strip()
        # Strip trailing line(s) that are only invented [[wikilinks]] — real
        # links are added via the //link command after formatting, not here.
        raw = re.sub(r"(?:\n\s*(?:\[\[[^\]]+\]\]\s*)+)+\s*$", "", raw).strip()
        # Content-loss guard: a heavy reformat should re-present, not delete.
        # If it dropped more than ~40% of the words, distrust it and fall back.
        if raw and _wc(raw) < 0.6 * _wc(text):
            print(f"[//md] reformat dropped too much text "
                  f"({_wc(text)}→{_wc(raw)} words); using conservative formatter")
            return format_body(text)
        return raw or format_body(text)
    except Exception as e:
        print(f"[//md] {e}; using conservative formatter")
        return format_body(text)


if __name__ == "__main__":
    args = sys.argv[1:]
    rich = False
    if args and args[0] in ("--rich", "-r"):
        rich = True
        args = args[1:]
    text = " ".join(args).strip() if args else sys.stdin.read().strip()
    if not text:
        print("usage: md_format.py [--rich] \"raw note text\"  (or pipe via stdin)",
              file=sys.stderr)
        sys.exit(1)
    print((format_body_rich if rich else format_body)(text))
