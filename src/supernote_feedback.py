#!/usr/bin/env python3
"""
Supernote Discord Feedback Listener

Polls the Discord API for replies to note notifications.
Parses feedback deterministically and applies it without LLM involvement
(the LLM → JSON-actions interpreter lives in supernote_watcher.apply_feedback):
  - Style rules        → appended to the style file (shapes future formatting)
  - Tag removal/add    → edits the note's frontmatter `topics:` list
  - OCR corrections    → "read p53 as PS3" grows the handwriting glossary
  - "format" / "y"/"n" → answers a "format this rough note?" prompt
  - photos captioned "scan <name>" → collected into a book for scan_to_book.py
Sends a one-line Discord confirmation reply.

Pending metadata files written by supernote_watcher.py:
  <state dir>/pending/{message_id}.json
  {"thread_id": "...", "note_path": "Inbox/X.md", ...}
"""

import json
import re
import sys
import time
import requests
from pathlib import Path
from datetime import datetime, timezone

# Learn OCR/handwriting corrections from replies. Imported defensively so the
# feedback loop keeps working (style rules, tags) even if the engine is missing.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import ocr_corrections
except Exception as _e:                       # pragma: no cover
    ocr_corrections = None
    print(f"[Feedback] correction glossary unavailable: {_e}")

# ── Config ────────────────────────────────────────────────────────────────────

import sn_config as mp

VAULT        = mp.vault_root()
STYLE_FILE   = mp.style_file()
PENDING_DIR  = mp.state_path("pending")
PENDING_DIR.mkdir(parents=True, exist_ok=True)
TAGS_DIR     = mp.tags()

POLL_INTERVAL = 15   # seconds between Discord API polls

# Bot token, home channel and allowed user IDs come from the environment / .env
_creds     = mp.discord_creds()
BOT_TOKEN  = _creds["token"]
HOME_CHAN  = _creds["channel"]
ALLOWED    = _creds["allowed"]

if not BOT_TOKEN:
    raise SystemExit("DISCORD_BOT_TOKEN not set (environment or the .env next to config.toml)")

HEADERS = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}

# ── Discord helpers ───────────────────────────────────────────────────────────

def discord_get(path: str) -> dict | list | None:
    try:
        r = requests.get(f"https://discord.com/api/v10{path}", headers=HEADERS, timeout=10)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            retry = float(r.json().get("retry_after", 5))
            print(f"[Rate limit] sleeping {retry:.1f}s")
            time.sleep(retry)
        return None
    except Exception as e:
        print(f"[Discord GET] {e}")
        return None


def discord_reply(channel_id: str, content: str) -> None:
    try:
        requests.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers=HEADERS,
            json={"content": content},
            timeout=10,
        )
    except Exception as e:
        print(f"[Discord reply] {e}")

# ── Feedback parser ───────────────────────────────────────────────────────────

def parse_feedback(text: str) -> dict:
    """
    Classify feedback text into actions. Returns a dict with keys:
      style_rule : str | None  — bullet to append to style file
      add_tags   : list[str]   — wikilink tags to add  ([[Tag]])
      remove_tags: list[str]   — wikilink tags to remove
    """
    text_lower = text.lower()
    result = {"style_rule": None, "add_tags": [], "remove_tags": [],
              "corrections": []}

    # ── OCR/handwriting corrections: teach how a misread token should read.
    #   "read p53 as PS3"  /  "fix 'rnosfet' to MOSFET"  /  "correct teh to the"
    # Only explicit teaching verbs are matched, so ordinary feedback (style
    # rules, tag edits) is never mistaken for a correction. Quotes optional; a
    # bare token is one word, or quote a multi-word phrase.
    _q = r"""["'‘’“”]"""   # straight/smart quotes
    _tok = rf"(?:{_q}([^\"'‘’“”]+?){_q}|(\S+))"
    corr_patterns = [
        rf"\bread\s+{_tok}\s+as\s+{_tok}",
        rf"\b(?:fix|change|correct|replace)\s+{_tok}\s+(?:to|into|with|→|->)\s+{_tok}",
    ]
    for pat in corr_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if not m:
            continue
        wrong = (m.group(1) or m.group(2) or "").strip().strip(".,;:!?")
        right = (m.group(3) or m.group(4) or "").strip().strip(".,;:!?")
        if wrong and right and wrong.lower() != right.lower():
            result["corrections"].append((wrong, right))
        break

    # ── Tag removal: "remove [[X]]", "remove X tag", "[[X]] can be removed", "X can be removed"
    for m in re.finditer(
        r"(?:remove|delete|strip|drop)\s+(?:\[\[)?([A-Za-z][\w\s]*)(?:\]\])?\s*tag?"
        r"|\[\[([^\]]+)\]\]\s+(?:can\s+be\s+|should\s+be\s+)?(?:removed|deleted)"
        r"|([A-Za-z][\w]+)\s+can\s+be\s+removed",
        text, re.IGNORECASE
    ):
        tag = (m.group(1) or m.group(2) or m.group(3) or "").strip().strip("[]")
        if tag:
            result["remove_tags"].append(tag)

    # ── Tag addition: "add [[X]]", "add X tag", "should get [[X]] tag", "always [[X]]"
    for m in re.finditer(
        r"(?:add|include|give\s+it)\s+(?:\[\[)?([A-Za-z][\w]*)(?:\]\])?\s*tag?"
        r"|always\s+(?:get\s+(?:a\s+)?)?\[\[([^\]]+)\]\]",
        text, re.IGNORECASE
    ):
        tag = (m.group(1) or m.group(2) or "").strip()
        if tag:
            result["add_tags"].append(tag)

    # ── Style rules: "X should always ...", "... notes get [[Y]] tag"
    style_patterns = [
        r"(\w[\w\s]+notes?\s+(?:should\s+)?always\s+(?:get|have|include|use)\s+.+)",
        r"(always\s+(?:tag|add|use|include)\s+.+)",
        r"(\w[\w\s]+should\s+always\s+.+)",
        r"(never\s+.+(?:tag|note|title|format).+)",
        r"(don'?t\s+.+(?:tag|note|title|format).+)",
    ]
    for pat in style_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            rule = m.group(1).strip().rstrip(".,;")
            # Strip leading conjunctions ("and food notes..." → "food notes...")
            rule = re.sub(r"^(?:and|but|also|plus)[,\s]+", "", rule, flags=re.IGNORECASE).strip()
            result["style_rule"] = rule[0].upper() + rule[1:]
            break

    return result


# ── Apply actions ─────────────────────────────────────────────────────────────

def append_style_rule(rule: str) -> bool:
    if not STYLE_FILE.exists():
        return False
    current = STYLE_FILE.read_text()
    if rule.lower() in current.lower():
        return False  # already there
    with STYLE_FILE.open("a") as f:
        f.write(f"- {rule}\n")
    print(f"[Style] Added: {rule}")
    return True


def edit_note_tags(note_path: Path, add: list[str], remove: list[str]) -> bool:
    if not note_path.exists():
        print(f"[Tags] Note not found: {note_path}")
        return False

    content = note_path.read_text()
    # Topic tags live in the note's YAML frontmatter as a `topics:` list of "[[X]]"
    # links (the properties format). Edit that block in place.
    if not content.startswith("---"):
        print(f"[Tags] No frontmatter to edit tags in: {note_path.name}")
        return False
    fm_end = content.find("\n---", 3)
    if fm_end < 0:
        return False
    head, tail = content[:fm_end], content[fm_end:]      # head="---\n...", tail="\n---\n<body>"
    lines = head.split("\n")

    # Find the `topics:` key and the "  - " item lines directly under it.
    start = next((i for i, l in enumerate(lines) if re.match(r"^topics:\s*$", l)), None)
    tags, block_end = [], None
    if start is not None:
        j = start + 1
        while j < len(lines) and re.match(r"^\s+-\s", lines[j]):
            mm = re.search(r"\[\[([^\]]+)\]\]", lines[j])
            if mm:
                tags.append(mm.group(1).strip())
            j += 1
        block_end = j

    tags = [t for t in tags if t not in remove]
    for t in add:
        if t not in tags:
            tags.append(t)
            _make_tag(t)

    new_block = (["topics:"] + [f'  - "[[{t}]]"' for t in tags]) if tags else []
    if start is not None:
        lines = lines[:start] + new_block + lines[block_end:]
    elif new_block:
        # No topics block yet — insert it before `source:` (or at frontmatter end).
        src = next((i for i, l in enumerate(lines) if l.startswith("source:")), len(lines))
        lines = lines[:src] + new_block + lines[src:]

    note_path.write_text("\n".join(lines) + tail)
    print(f"[Tags] Updated {note_path.name}: removed={remove} added={add}")
    return True


def _make_tag(name: str):
    p = TAGS_DIR / f"{name}.md"
    if not p.exists():
        p.write_text("")
        print(f"[Tags] Created tag file: {name}")

# ── Opt-in formatting (y/n reply to a "format this rough note?" prompt) ───────

_RAW_DIR = mp.supernote_raw()
_YES_RE  = re.compile(r"^(y|yes|ya|yeah|yep|format|do it|fmt|sure|please(?: format)?|go|ok|okay)$", re.I)
_NO_RE   = re.compile(r"^(n|no|nope|nah|skip|leave it|don'?t|no thanks)$", re.I)


def _sidecar_by_rough(rel: str):
    for sc in _RAW_DIR.rglob("*.processed"):
        try:
            if json.loads(sc.read_text()).get("rough") == rel:
                return sc
        except Exception:
            continue
    return None


def maybe_handle_format_optin(note_rel: str, text: str):
    """If `note_rel` is a Rough copy awaiting a format y/n and `text` answers it,
    do the action and return a reply string; else return None (fall through)."""
    sc = _sidecar_by_rough(note_rel)
    if sc is None:
        return None
    try:
        if not json.loads(sc.read_text()).get("awaiting_format"):
            return None
    except Exception:
        return None
    low = text.strip(" .!?")
    if _YES_RE.match(low):
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import supernote_watcher
            return supernote_watcher.format_from_rough(note_rel)
        except Exception as e:                    # pragma: no cover — defensive
            print(f"[Feedback] format failed: {e}")
            return "⚠️ couldn't format that one"
    if _NO_RE.match(low):
        try:
            d = json.loads(sc.read_text()); d["awaiting_format"] = False
            sc.write_text(json.dumps(d))
        except Exception:
            pass
        return "👍 left it as a Rough note"
    return None

# ── Photo → scan inbox ────────────────────────────────────────────────────────

_SCAN_INBOX = mp.scan_inbox()
_IMG_CTYPES = ("image/jpeg", "image/png", "image/webp", "image/heic", "image/heif")
_IMG_EXTS   = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif")


def ingest_scan_photos(msg: dict):
    """If a message carries image attachments, download them into the scan inbox
    (one folder per book, named by the caption). The scan-inbox timer then turns
    the folder into an annotatable Supernote PDF. Returns a reply string if it
    handled photos, else None. Same caption across messages = same book."""
    # Only real page photos — NOT gifs (reaction/meme gifs aren't document pages).
    atts = [a for a in (msg.get("attachments") or [])
            if (a.get("content_type") or "").lower() in _IMG_CTYPES
            or str(a.get("filename", "")).lower().endswith(_IMG_EXTS)]
    if not atts:
        return None
    caption = re.sub(r"<@!?\d+>", "", msg.get("content", "")).strip()

    # Gate on EXPLICIT scan intent so a lone book-cover / receipt / random photo
    # is NOT auto-turned into a Scan PDF (design rule: never auto-act on an image —
    # let the agent classify + confirm it instead). Three cases:
    #   1. caption "scan [name]" / "//scan [name]"  → start/continue that book
    #   2. bare image (no caption) + an ACTIVE pending scan folder → next page of it
    #   3. anything else (bare with no active scan, or a non-"scan" caption) → skip,
    #      returning None so another handler (e.g. an agent gateway) can take it.
    m = re.match(r"^(?://)?scan\b[:\s]*(.*)$", caption, re.I)
    if m:
        name = re.sub(r'[<>:"/\\|?*]', "", m.group(1)).strip()
        book = name or datetime.now().strftime("Scan %Y-%m-%d")
        folder = _SCAN_INBOX / book
    elif not caption:
        pending = ([d for d in _SCAN_INBOX.iterdir() if d.is_dir()]
                   if _SCAN_INBOX.exists() else [])
        if not pending:
            return None   # no active scan session → not a scan; leave it for the agent
        folder = max(pending, key=lambda d: d.stat().st_mtime)   # newest open book
        book = folder.name
    else:
        return None       # a real caption that isn't "scan …" → not a scan request
    folder.mkdir(parents=True, exist_ok=True)
    # Only acknowledge ONCE per book (when the folder is still empty) — a phone
    # sends each photo as its own message, so acking every one floods the chat.
    first_for_book = not any(folder.iterdir())
    saved = 0
    for a in atts:
        url = a.get("url")
        fn  = re.sub(r'[<>:"/\\|?*]', "", a.get("filename", "page")) or "page.jpg"
        if not url:
            continue
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            # epoch-ms prefix preserves send order across messages → page order
            (folder / f"{int(time.time()*1000)}_{saved:02d}_{fn}").write_bytes(r.content)
            saved += 1
        except Exception as e:
            print(f"[Scan] download failed {fn}: {e}")
    if not saved:
        return None
    print(f"[Scan] {saved} photo(s) → inbox/{book}")
    if not first_for_book:
        return ""   # already acked this book — stay quiet, just collect pages
    return (f"📥 Collecting pages for **{book}** — keep sending photos (they add "
            f"as pages). I'll post the book when you're done (~2 min idle).")

# ── Persistent state ─────────────────────────────────────────────────────────

_STATE_FILE = PENDING_DIR / "_feedback_state.json"

def _load_state() -> dict:
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text())
        except Exception:
            pass
    return {}

def _save_state(state: dict):
    try:
        _STATE_FILE.write_text(json.dumps(state))
    except Exception as e:
        print(f"[State] save error: {e}")

_seen: dict[str, dict] = _load_state()


def _load_pending() -> dict[str, dict]:
    """Return {thread_id: metadata} for all known pending notification threads."""
    result = {}
    for f in PENDING_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            tid = data.get("thread_id")
            if tid:
                result[tid] = data
        except Exception:
            pass
    return result


def poll_threads():
    pending = _load_pending()
    if not pending:
        return

    # Build a map of notification_message_id → metadata
    notif_ids = {meta["thread_id"]: meta for meta in pending.values() if "thread_id" in meta}
    if not notif_ids:
        return

    state = _seen.setdefault("main", {"last_id": None})

    # Fetch recent messages from the home channel
    url = f"/channels/{HOME_CHAN}/messages?limit=20"
    if state["last_id"]:
        url += f"&after={state['last_id']}"
    msgs = discord_get(url)
    if not msgs or not isinstance(msgs, list):
        return

    bot_id = _get_bot_id()
    for msg in reversed(msgs):  # oldest first
        msg_id = msg.get("id", "")
        state["last_id"] = msg_id

        # Skip bot messages and non-allowed users
        author_id = msg.get("author", {}).get("id", "")
        if author_id == bot_id:
            continue
        if ALLOWED and author_id not in ALLOWED:
            continue

        # Photos sent to the bot → scan inbox → annotatable Supernote PDF.
        photo_reply = ingest_scan_photos(msg)
        if photo_reply is not None:
            if photo_reply:                       # "" = already acked this book
                discord_reply(HOME_CHAN, photo_reply)
            _save_state(_seen)
            continue

        # Is this a reply to one of our notification messages?
        ref = msg.get("referenced_message") or {}
        ref_id = ref.get("id") or (msg.get("message_reference") or {}).get("message_id")
        meta = notif_ids.get(ref_id)
        if not meta:
            continue  # not a reply to a note notification

        note_rel  = meta.get("note_path", "")
        note_path = VAULT / note_rel if note_rel else None
        text      = msg.get("content", "").strip()

        # Strip @mention of bot
        text = re.sub(r"<@!?\d+>\s*", "", text).strip()
        if not text:
            continue

        print(f"[Feedback] reply to notif={ref_id}: {text!r}")

        # Opt-in formatting: y/n answer to a "format this rough note?" prompt
        fmt_reply = maybe_handle_format_optin(note_rel, text)
        if fmt_reply is not None:
            discord_reply(HOME_CHAN, fmt_reply)
            _save_state(_seen)
            continue

        fb = parse_feedback(text)

        actions_taken = []

        if fb["style_rule"]:
            if append_style_rule(fb["style_rule"]):
                actions_taken.append("style")

        if (fb["add_tags"] or fb["remove_tags"]) and note_path:
            if edit_note_tags(note_path, fb["add_tags"], fb["remove_tags"]):
                actions_taken.append("note")

        if fb.get("corrections") and ocr_corrections is not None:
            learned = 0
            for wrong, right in fb["corrections"]:
                try:
                    if ocr_corrections.add_correction(wrong, right):
                        learned += 1
                except Exception as e:        # pragma: no cover — defensive
                    print(f"[Feedback] correction error: {e}")
            if learned:
                actions_taken.append("learn")

        if not actions_taken:
            # No pattern matched — still acknowledge
            pass

        labels = {
            frozenset(["style"]):         "Got it — added to style memory ✅",
            frozenset(["note"]):          "Fixed ✅",
            frozenset(["learn"]):         "Learned that — I'll read it right next time ✅",
            frozenset(["style", "note"]): "Fixed + added to style memory ✅",
            frozenset(["note", "learn"]): "Fixed + learned the correction ✅",
        }
        reply = labels.get(frozenset(actions_taken), "Got it ✅")
        discord_reply(HOME_CHAN, reply)
        _save_state(_seen)


_bot_id_cache: str | None = None

def _get_bot_id() -> str:
    global _bot_id_cache
    if _bot_id_cache is None:
        data = discord_get("/users/@me")
        _bot_id_cache = data["id"] if data else ""
    return _bot_id_cache


# ── Main ──────────────────────────────────────────────────────────────────────

def _init_cursor():
    """On first start (no persisted cursor), anchor to the current newest message
    so we only react to replies that arrive after this process starts."""
    state = _seen.setdefault("main", {"last_id": None})
    if state["last_id"] is not None:
        print(f"[Feedback] Resuming from message {state['last_id']}")
        return
    msgs = discord_get(f"/channels/{HOME_CHAN}/messages?limit=1")
    if msgs and isinstance(msgs, list):
        state["last_id"] = msgs[0]["id"]
        _save_state(_seen)
        print(f"[Feedback] Anchored cursor to {state['last_id']}")
    else:
        print("[Feedback] Could not anchor cursor — will process recent history")


def main():
    print(f"[Feedback] Supernote Discord feedback listener started")
    print(f"[Feedback] Polling every {POLL_INTERVAL}s")
    _init_cursor()
    while True:
        try:
            poll_threads()
        except Exception as e:
            print(f"[Feedback] {e}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
