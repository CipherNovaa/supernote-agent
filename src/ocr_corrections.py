#!/usr/bin/env python3
"""
OCR / handwriting correction glossary — learns the writer's transcription quirks.

Stored at <state dir>/corrections.json (see sn_config) as a list of entries:
    {"wrong": str, "right": str, "n": int, "note": str}

These are used as *LLM hints* (NOT blind regex substitution) injected into the
Supernote formatter prompt: the model is told "this handwriting often OCRs
'p53' as 'PS3'" and decides, in context, whether to apply it. That keeps it
safe — a hint never forces a wrong change.

The glossary grows over time from Discord corrections ("read p53 as PS3"),
captured by the feedback loop, so the pipeline gets better at *this* handwriting
the more it is used.

CLI:
    python ocr_corrections.py list
    python ocr_corrections.py add "p53" "PS3" ["optional note"]
    python ocr_corrections.py prompt "some text to rank against"
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sn_config as _cfg

STORE       = _cfg.state_path("corrections.json")
MAX_ENTRIES = 200          # cap; least-used are dropped when exceeded

# A small, high-confidence seed so the glossary is useful on day one. These are
# deliberately conservative (real quirks seen in practice + unambiguous
# domain terms). Everything else is learned from the user's corrections.
_SEED: list[dict] = [
    {"wrong": "p53",  "right": "PS3",  "n": 1, "note": "PlayStation 3"},
    {"wrong": "p54",  "right": "PS4",  "n": 1, "note": "PlayStation 4"},
    {"wrong": "p55",  "right": "PS5",  "n": 1, "note": "PlayStation 5"},
    {"wrong": "rnosfet", "right": "MOSFET", "n": 1, "note": ""},
    {"wrong": "c+t", "right": "C++", "n": 1, "note": ""},
    {"wrong": "tt-a.gs", "right": "Tags", "n": 1, "note": "header line"},
]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def load() -> list[dict]:
    if STORE.exists():
        try:
            data = json.loads(STORE.read_text())
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return [dict(e) for e in _SEED]


def save(items: list[dict]) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(items, indent=2, ensure_ascii=False))


def add_correction(wrong: str, right: str, note: str = "") -> bool:
    """Record that the OCR token `wrong` should read as `right`. Dedupes
    case-insensitively (bumping a frequency count) and caps the store."""
    wrong, right, note = _norm(wrong), _norm(right), _norm(note)
    if not wrong or not right or wrong.lower() == right.lower():
        return False
    items = load()
    for it in items:
        if it.get("wrong", "").lower() == wrong.lower():
            it["right"] = right
            it["n"] = int(it.get("n", 1)) + 1
            if note:
                it["note"] = note
            save(items)
            print(f"[corrections] reinforced: {wrong!r} → {right!r} (n={it['n']})")
            return True
    items.append({"wrong": wrong, "right": right, "n": 1, "note": note})
    items.sort(key=lambda x: -int(x.get("n", 1)))
    save(items[:MAX_ENTRIES])
    print(f"[corrections] learned: {wrong!r} → {right!r}")
    return True


def glossary_prompt(text: str = "", max_n: int = 15) -> str:
    """Return a prompt snippet of the most relevant corrections. Entries whose
    `wrong` token actually appears in `text` are surfaced first, then the rest
    by frequency. Empty string if the glossary is empty."""
    items = load()
    if not items:
        return ""
    low = (text or "").lower()

    def score(it: dict):
        present = it.get("wrong", "").lower() in low
        return (1 if present else 0, int(it.get("n", 1)))

    ranked = sorted(items, key=score, reverse=True)[:max_n]
    lines = []
    for it in ranked:
        note = f'  ({it["note"]})' if it.get("note") else ""
        lines.append(f'- "{it["wrong"]}" usually means "{it["right"]}"{note}')
    return ("Known transcription quirks in this handwriting — apply ONLY when "
            "the surrounding context clearly fits; never force a change:\n"
            + "\n".join(lines))


# ── CLI ───────────────────────────────────────────────────────────────────────

def _main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "list":
        items = load()
        print(f"{len(items)} correction(s):")
        for it in sorted(items, key=lambda x: -int(x.get("n", 1))):
            note = f'  ({it["note"]})' if it.get("note") else ""
            print(f'  {it.get("n",1):>3}×  {it["wrong"]!r} → {it["right"]!r}{note}')
        return 0
    if cmd == "add":
        if len(argv) < 3:
            print("usage: ocr_corrections.py add WRONG RIGHT [NOTE]")
            return 2
        add_correction(argv[1], argv[2], argv[3] if len(argv) > 3 else "")
        return 0
    if cmd == "prompt":
        print(glossary_prompt(argv[1] if len(argv) > 1 else ""))
        return 0
    print(f"unknown command: {cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
