#!/usr/bin/env python3
"""
book_grabber.py — Hermes "book grabber" engine.

Searches Library Genesis for a book, shows the user a ranked list of editions/
formats, and — once they pick one — downloads it straight into the right folder
under the vault's Source Material tree so it syncs to the Supernote and is
ready to read. Use it only for material you are entitled to download.

Flow (matches the program-installer pattern):
  * search(query)      — READ-ONLY. Queries Libgen across several mirrors with
                         retry/backoff (the mirrors share one overloaded DB and
                         intermittently return HTTP 500), parses the results
                         table, returns ranked Book candidates.
  * search_md(query)   — the same, rendered as a numbered markdown list for
                         Discord / the Supernote. The list is cached so a later
                         "download number 3" can be resolved without re-search.
  * download(...)      — resolves a book's real file URL (ads.php -> get.php?key)
                         and streams it to the correct Source Material subfolder.

Folder choice ("prefer a folder that is already there"):
  Existing subfolders of Source Material are matched first (so every C++
  book lands in "Programming (C++)", art books in "Art Books", etc.). Only when
  nothing matches is a new, topic-named folder created. An explicit `topic` can
  override the guess.

Safety / honesty:
  * Search is read-only. Download only ever WRITES a file into the vault — it
    never runs shell commands or touches anything else.
  * Every download is verified (magic bytes match the claimed extension) before
    it is kept; partial/aborted downloads are cleaned up.
  * Already-present books are detected and skipped rather than re-downloaded.

Usage (CLI):
    book_grabber.py search "stroustrup c++"
    book_grabber.py search "linux kernel" --ext pdf
    book_grabber.py download --index 3                 # from the last search
    book_grabber.py download --md5 <md5> --ext epub --title "..." \
        --author "..." --year 2021 [--topic "Programming (C++)"]

Importable:
    import book_grabber
    md = book_grabber.search_md("calculus")
    out = book_grabber.download(index=2)
"""

from __future__ import annotations

import re
import sys
import json
import time
import argparse
from pathlib import Path
from dataclasses import dataclass, asdict, field

import requests
from bs4 import BeautifulSoup

# ── Config ──────────────────────────────────────────────────────────────────

# Source Material comes from sn_config (single source of truth), so a vault
# re-organisation never silently sends books to a dead folder.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sn_config as _mp

VAULT          = _mp.vault_root()
SOURCE_DIR     = _mp.d("source_material")
CACHE_DIR      = _mp.state_path("cache")
LAST_SEARCH    = CACHE_DIR / "book_search_last.json"

# Mirrors of the libgen.li fork (identical HTML + ads.php/get.php scheme).
# Tried in order for both search and download; we fail over on error/overload.
MIRRORS        = ("https://libgen.bz", "https://libgen.li", "https://libgen.gs",
                  "https://libgen.vg", "https://libgen.la")

UA             = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
                                "Gecko/20100101 Firefox/128.0"}
SEARCH_TIMEOUT = 25
DL_PAGE_TIMEOUT = 25
DOWNLOAD_TIMEOUT = 1800        # large art/manga PDFs can be hundreds of MB
MAX_RESULTS    = 12
RETRIES        = 4             # per mirror sweep, for transient DB-overload 500s

# Formats that read well on the Supernote, best first.
EXT_RANK       = {"pdf": 0, "epub": 1, "djvu": 2, "mobi": 3, "azw3": 4, "cbz": 5}

# A libgen DB overload looks like a 500 carrying this phrase — worth retrying.
_OVERLOAD_HINT = "max_user_connections"

# Map of subject keywords -> the category a book belongs to. The category is
# then resolved against the *actual* existing folders (so "c++" reuses the
# existing "Programming (C++)" folder). Order matters: first match wins.
KEYWORD_CATEGORY: list[tuple[tuple[str, ...], str]] = [
    (("c++", "cpp", "c plus plus", "stroustrup"),                  "Programming (C++)"),
    (("calculus", "algebra", "geometry", "mathematic", "trigonometr",
      "number theory", "probability", "statistic", "topology"),   "Math"),
    (("linux", "unix", "kernel", "bash", "shell scripting",
      "arch linux", "debian", "ubuntu"),                          "Linux"),
    (("manga", "comic", "graphic novel"),                         "Manga"),
    (("art of", "artwork", "art book", "concept art", "illustration",
      "drawing", "painting", "sketch"),                           "Art Books"),
    (("manual", "instruction", "datasheet", "user guide",
      "handbook", "reference guide"),                             "Instruction Manuals"),
    (("python", "javascript", "rust", "golang", "haskell", "compiler",
      "algorithm", "data structure", "operating system",
      "programming", "software"),                                 "Programming"),
]


# ── HTTP helpers ────────────────────────────────────────────────────────────

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(UA)
    return s


def _get(s: requests.Session, url: str, timeout: int, **kw):
    """GET that returns the Response or None (never raises)."""
    try:
        return s.get(url, timeout=timeout, **kw)
    except Exception:
        return None


def _overloaded(r) -> bool:
    return r is not None and r.status_code == 500 and _OVERLOAD_HINT in (r.text or "")


# ── Candidate model ─────────────────────────────────────────────────────────

@dataclass
class Book:
    title: str
    authors: str = ""
    publisher: str = ""
    year: str = ""
    language: str = ""
    pages: str = ""
    size: str = ""
    ext: str = ""
    md5: str = ""
    mirror: str = ""          # which mirror surfaced this row

    def rank_key(self, query: str) -> tuple:
        q = query.lower()
        title = (self.title or "").lower()
        starts = title.startswith(q)
        contains = q in title
        lang_en = 0 if self.language.lower().startswith("en") else 1
        ext_rank = EXT_RANK.get(self.ext.lower(), 9)
        return (not starts, not contains, lang_en, ext_rank, title)


# ── Search ──────────────────────────────────────────────────────────────────

def _parse_results(html: str, mirror: str) -> list[Book]:
    """Parse a libgen.li-fork results page into Book rows."""
    soup = BeautifulSoup(html, "lxml")
    books: list[Book] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        header = " ".join(c.get_text(" ", strip=True).lower()
                          for c in rows[0].find_all(["td", "th"]))
        # The results table is the one whose header has these columns.
        if "author" not in header or "mirror" not in header:
            continue
        for tr in rows[1:]:
            cells = tr.find_all("td")
            if len(cells) < 9:
                continue
            # md5 lives in the mirror cell's ads.php?md5=... (or any md5= link).
            md5 = ""
            for a in tr.find_all("a", href=True):
                m = re.search(r"md5=([0-9A-Fa-f]{32})", a["href"])
                if m:
                    md5 = m.group(1).lower()
                    break
            if not md5:
                continue
            # Title: the cell has three edition.php anchors — an empty icon (in
            # the <b> series name), the real title, then a green ISBN list. Take
            # the first non-empty anchor whose text isn't a pure ISBN/number run.
            title_cell = cells[0]
            title = ""
            for a in title_cell.find_all("a", href=re.compile(r"edition\.php")):
                txt = a.get_text(" ", strip=True)
                if txt and not re.fullmatch(r"[\d;\sXx\-]+", txt):
                    title = txt
                    break
            if not title:
                title = title_cell.get_text(" ", strip=True)
            books.append(Book(
                title=title,
                authors=cells[1].get_text(" ", strip=True),
                publisher=cells[2].get_text(" ", strip=True),
                year=cells[3].get_text(" ", strip=True),
                language=cells[4].get_text(" ", strip=True),
                pages=cells[5].get_text(" ", strip=True),
                size=cells[6].get_text(" ", strip=True),
                ext=cells[7].get_text(" ", strip=True).lower(),
                md5=md5,
                mirror=mirror,
            ))
        break
    return books


def search(query: str, ext: str | None = None,
           max_results: int = MAX_RESULTS) -> list[Book]:
    """Search Libgen across mirrors (with retry) and return ranked Books."""
    query = (query or "").strip()
    if not query:
        return []
    s = _session()
    books: list[Book] = []
    for attempt in range(RETRIES):
        for base in MIRRORS:
            url = f"{base}/index.php?req={requests.utils.quote(query)}"
            r = _get(s, url, SEARCH_TIMEOUT)
            if r is None or _overloaded(r):
                continue
            if r.status_code == 200:
                parsed = _parse_results(r.text, base)
                if parsed:
                    books = parsed
                    break
        if books:
            break
        time.sleep(2 * (attempt + 1))   # back off; the DB is likely overloaded

    if ext:
        ext = ext.lower().lstrip(".")
        books = [b for b in books if b.ext == ext]

    # De-dup by md5, then rank.
    seen: set[str] = set()
    uniq: list[Book] = []
    for b in books:
        if b.md5 in seen:
            continue
        seen.add(b.md5)
        uniq.append(b)
    uniq.sort(key=lambda b: b.rank_key(query))
    return uniq[:max_results]


def _cache_search(query: str, books: list[Book]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        LAST_SEARCH.write_text(json.dumps(
            {"query": query, "ts": time.time(),
             "results": [asdict(b) for b in books]}, indent=2))
    except Exception:
        pass


def _load_cached() -> list[Book]:
    try:
        data = json.loads(LAST_SEARCH.read_text())
        return [Book(**b) for b in data.get("results", [])]
    except Exception:
        return []


def search_md(query: str, ext: str | None = None) -> str:
    """Numbered markdown list of search hits (and cache them for selection)."""
    books = search(query, ext=ext)
    _cache_search(query, books)
    if not books:
        extra = f" in **{ext}**" if ext else ""
        return (f"No books found for **{query}**{extra} on Libgen right now. "
                f"The mirrors are sometimes overloaded — try again, reword the "
                f"title/author, or drop the format filter.")
    lines = [f"Found **{len(books)}** result(s) for **{query}** — "
             f"reply with the number you want and I'll download it:\n"]
    for i, b in enumerate(books, 1):
        meta = " · ".join(x for x in (
            b.ext.upper(), b.size, b.year, b.language) if x)
        author = f" — {b.authors}" if b.authors else ""
        lines.append(f"{i}. **{b.title}**{author}\n   {meta}")
    lines.append("\n_(Say e.g. “number 2” or “the epub”. I’ll file it under "
                 "Source Material, reusing the matching topic folder.)_")
    return "\n".join(lines)


# ── Cover image → title (local vision model) ──────────────────────────────────
# The whole point of sending a cover photo is to NOT type the title. The text model
# can't see, and OCR fails on photographed covers — so we read the cover with a local
# vision model (llava:7b: ~4.6GB, fits the 8GB GPU, ~15s; qwen2.5vl:7b needs 13GB →
# CPU → too slow). Images the agent receives land in its image cache.

_OLLAMA = _mp.OLLAMA_URL
_VISION_MODEL = _mp.COVER_MODEL
_IMG_CACHE = Path(_mp.env("SUPERNOTE_IMAGE_CACHE") or str(_mp.state_path("image_cache")))
_IMG_EXT = {".jpeg", ".jpg", ".png", ".webp"}


def _latest_image() -> Path | None:
    if not _IMG_CACHE.is_dir():
        return None
    imgs = [p for p in _IMG_CACHE.iterdir()
            if p.is_file() and p.suffix.lower() in _IMG_EXT]
    return max(imgs, key=lambda p: p.stat().st_mtime) if imgs else None


def _resized_bytes(p: Path, max_px: int = 1100) -> bytes:
    """Downscale for faster vision inference; fall back to the raw bytes."""
    try:
        import subprocess, tempfile
        out = Path(tempfile.mkstemp(suffix=".jpg")[1])
        r = subprocess.run(["convert", str(p), "-resize", f"{max_px}x{max_px}>",
                            "-quality", "85", str(out)], capture_output=True, timeout=20)
        if r.returncode == 0 and out.exists() and out.stat().st_size > 0:
            data = out.read_bytes()
            out.unlink(missing_ok=True)
            return data
    except Exception:
        pass
    return p.read_bytes()


_COVER_PROMPT = (
    "Look at this book cover. Reply in EXACTLY this format, nothing else:\n"
    "TITLE: <title>\nAUTHOR: <author>\nGENRE: <one of: Programming, Mathematics, "
    "Science, Psychology, Self-Help, Philosophy, History, Business, Fiction, Fantasy, "
    "Science Fiction, Manga, Art, Biography, Health, Reference, Religion, Politics>"
)

# Freeform genre (from llava / the text model) → a CANONICAL shelf folder, matched by
# keyword so "Biographies & Memoirs", "Autobiography", "Memoir" all land in "Biography"
# (stops shelves splintering). First keyword hit wins; unmatched → Title-cased as-is.
_SHELF_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("game design", "game dev", "gamedev", "video game"), "Game Design"),
    (("program", "coding", "software", "computer", "technolog", "computing",
      "hacking", "security", "networking", "devops"),    "Programming"),
    (("math",),                                          "Mathematics"),
    (("psycholog",),                                     "Psychology"),
    (("self-help", "self help", "personal development"), "Self-Help"),
    (("philosoph",),                                     "Philosophy"),
    (("biograph", "memoir", "autobiograph"),             "Biography"),
    (("histor",),                                        "History"),
    (("business", "econom", "finance", "money"),         "Business"),
    (("sci-fi", "science fiction"),                      "Science Fiction"),
    (("fantasy",),                                       "Fantasy"),
    (("fiction", "novel", "literature"),                 "Fiction"),
    (("manga", "comic", "graphic novel"),                "Manga"),
    (("art", "design", "illustration"),                  "Art Books"),
    (("health", "fitness", "medical", "nutrition"),      "Health"),
    (("reference", "manual", "guide", "handbook", "textbook"), "Instruction Manuals"),
    (("religio", "spiritual", "theolog"),                "Religion"),
    (("politic", "government"),                          "Politics"),
    (("science", "physics", "biology", "chemistry"),     "Science"),
]

# Editions that aren't the real book — never auto-pick these.
_JUNK_TITLE = re.compile(
    r"\b(summary|workbook|study guide|abridged|analysis|companion|quicklet|"
    r"sparknotes|cliffnotes|notes on|key takeaways|conversation starters)\b", re.I)


def read_cover(image: str | None = None) -> tuple[str, str, str]:
    """Vision-read a cover photo → (title, author, genre). Empty title on failure."""
    import base64
    p = Path(image).expanduser() if image else _latest_image()
    if not p or not p.exists():
        return "", "", ""
    b64 = base64.b64encode(_resized_bytes(p)).decode()
    try:
        r = requests.post(f"{_OLLAMA}/api/generate", timeout=180, json={
            "model": _VISION_MODEL, "prompt": _COVER_PROMPT, "images": [b64],
            "stream": False, "keep_alive": "30s"})
        raw = (r.json().get("response") or "").strip() if r.status_code == 200 else ""
    except Exception:
        raw = ""
    title = author = genre = ""
    for line in raw.splitlines():
        m = re.match(r"\s*(TITLE|AUTHOR|GENRE)\s*:\s*(.+)", line, re.I)
        if not m:
            continue
        key, val = m.group(1).upper(), m.group(2).strip()
        if key == "TITLE":
            title = val
        elif key == "AUTHOR":
            author = val
        else:
            genre = val
    if not title and raw:                      # model ignored the format — take line 1
        title = re.split(r"\s+[—–|]\s+|\bby\b", raw.splitlines()[0])[0].strip()
    return title, author, genre


def _shelf_for_genre(genre: str) -> str | None:
    g = (genre or "").strip().lower()
    if not g:
        return None
    for keys, shelf in _SHELF_KEYWORDS:
        if any(k in g for k in keys):
            return shelf
    return genre.strip().title()


def _size_mb(s: str) -> float:
    m = re.search(r"([\d.]+)\s*(kb|mb|gb)", (s or "").lower())
    if not m:
        return 0.0
    v, u = float(m.group(1)), m.group(2)
    return v / 1024 if u == "kb" else (v * 1024 if u == "gb" else v)


def _best_pick(books: list[Book], prefer_ext: str = "epub",
               want_author: str = "") -> Book | None:
    """Auto-select the best REAL edition. Scores: format match, AUTHOR match against
    the cover's author (strongest signal — kills wrong-author knockoffs), and a real
    (non-tiny) file size (a 0.4 MB copy of a 400-page memoir is a summary, not the book)."""
    prefer_ext = (prefer_ext or "epub").lower()
    cand = [b for b in books if not _JUNK_TITLE.search(b.title or "")] or list(books)
    surnames = {w for w in re.findall(r"[a-z]{3,}", (want_author or "").lower())}

    def score(b: Book) -> float:
        s = 0.0
        if (b.ext or "").lower() == prefer_ext:
            s += 3
        if surnames and surnames & set(re.findall(r"[a-z]{3,}", (b.authors or "").lower())):
            s += 6                       # author match — the real edition
        mb = _size_mb(b.size)
        if mb >= 1:
            s += 1
        if 0 < mb < 0.4:
            s -= 4                       # suspiciously tiny → likely a summary/fake
        return s

    cand.sort(key=score, reverse=True)
    return cand[0] if cand else None


_GENRES = ["Programming", "Mathematics", "Science", "Psychology", "Self-Help",
           "Philosophy", "History", "Business", "Fiction", "Fantasy",
           "Science Fiction", "Manga", "Art", "Biography", "Health", "Reference",
           "Religion", "Politics", "Economics"]


def _infer_genre(title: str, author: str = "") -> str:
    """Ask the local text model for a genre shelf when the cover read didn't give one
    (llava reads text well but is inconsistent at classifying). Returns '' on failure."""
    who = f" by {author}" if author else ""
    prompt = (f'Which single genre shelf best fits the book "{title}"{who}? '
              f'Answer with EXACTLY ONE of: {", ".join(_GENRES)}. One word or phrase, nothing else. /no_think')
    try:
        r = requests.post(f"{_OLLAMA}/api/generate", timeout=60, json={
            "model": _mp.GENRE_MODEL, "prompt": prompt, "stream": False,
            "keep_alive": "30s", "options": {"temperature": 0}})
        out = (r.json().get("response") or "") if r.status_code == 200 else ""
        for g in sorted(_GENRES, key=len, reverse=True):
            if g.lower() in out.lower():
                return g
    except Exception:
        pass
    return ""


def find_from_cover(image: str | None = None, ext: str = "epub",
                    auto: bool = True) -> str:
    """Read a cover photo → search → (auto) download the best edition to its genre
    shelf. The whole 'send a cover, get the book' path — no typing, no number-picking."""
    ext = ext or "epub"                        # handler may pass ext=None → don't break _best_pick
    title, author, genre = read_cover(image)
    if not title:
        return ("I couldn't find or read a cover image just now — send the cover photo "
                "again, or just tell me the title.")
    books = search(title)
    _cache_search(title, books)
    if not books:
        return (f"📖 Cover reads as **{title}**"
                f"{(' — ' + author) if author else ''}, but I found no download on "
                f"Libgen right now (mirrors may be down). Try again shortly.")
    if not auto:
        return (f"📖 Cover reads as **{title}**.\n\n" + search_md(title, ext=ext))
    best = _best_pick(books, prefer_ext=ext, want_author=author)
    if best is None:
        return f"📖 Cover reads as **{title}**.\n\n" + search_md(title, ext=ext)
    genre = genre or _infer_genre(title, author)          # text-model fallback
    shelf = _shelf_for_genre(genre) or "General"
    head = f"📖 Cover: **{title}**{(' — ' + author) if author else ''} · {genre or shelf}\n"
    return head + download(md5=best.md5, ext=best.ext, title=best.title,
                           author=best.authors or author, year=best.year, topic=shelf)


# ── Folder categorisation ───────────────────────────────────────────────────

def _existing_folders() -> list[str]:
    if not SOURCE_DIR.is_dir():
        return []
    return sorted(p.name for p in SOURCE_DIR.iterdir() if p.is_dir())


def _resolve_category(category: str, existing: list[str],
                      fuzzy: bool = False) -> str:
    """Map a desired category to an existing folder name if one matches.

    Exact (case-insensitive) match always wins. Loose substring matching is
    only used for an explicit user-supplied topic (``fuzzy=True``): for the
    auto-inferred category we keep it exact so a generic fallback like "Books"
    can't accidentally land inside "Art Books", nor a Python book inside
    "Programming (C++)".
    """
    cl = category.lower()
    for name in existing:                       # exact match first
        if name.lower() == cl:
            return name
    if fuzzy:
        for name in existing:
            nl = name.lower()
            if cl in nl or nl in cl:
                return name
    return category


def choose_folder(book: Book, topic: str | None = None) -> Path:
    """Pick (and create) the Source Material subfolder for a book."""
    existing = _existing_folders()
    if topic:
        folder = _resolve_category(topic.strip(), existing, fuzzy=True)
    else:
        hay = f"{book.title} {book.authors} {book.publisher}".lower()
        category = "Books"            # neutral fallback
        for keywords, cat in KEYWORD_CATEGORY:
            if any(k in hay for k in keywords):
                category = cat
                break
        folder = _resolve_category(category, existing)
    target = SOURCE_DIR / _sanitize(folder)
    target.mkdir(parents=True, exist_ok=True)
    return target


# ── Filename ────────────────────────────────────────────────────────────────

_BAD_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize(name: str, limit: int = 180) -> str:
    name = _BAD_FS.sub("", name).strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    return name[:limit].strip() or "untitled"


def _filename(book: Book) -> str:
    parts = [book.title.strip()]
    if book.authors:
        # keep it short — first author/segment is enough for a filename
        author = re.split(r"[;,]", book.authors)[0].strip()
        if author:
            parts.append(author)
    base = " -- ".join(parts)
    if book.year:
        base += f" ({book.year})"
    ext = book.ext.lower().lstrip(".") or "bin"
    return f"{_sanitize(base)}.{ext}"


# ── Magic-byte verification ─────────────────────────────────────────────────

def _verify(path: Path, ext: str) -> bool:
    """Cheap sanity check that the bytes match the claimed extension."""
    try:
        with path.open("rb") as f:
            head = f.read(8)
    except Exception:
        return False
    ext = ext.lower()
    if ext == "pdf":
        return head.startswith(b"%PDF")
    if ext in ("epub", "cbz", "azw3"):     # all ZIP containers
        return head.startswith(b"PK\x03\x04")
    if ext == "djvu":
        return head.startswith(b"AT&T") or head[:4] == b"FORM"
    if ext == "mobi":
        return True                         # MOBI magic is at offset 60; trust it
    return len(head) > 0                     # unknown ext: just require non-empty


# ── Download-link resolution ────────────────────────────────────────────────

def _resolve_download_url(s: requests.Session, md5: str) -> tuple[str, str] | None:
    """Return (mirror, get.php-url) for an md5, trying mirrors with backoff."""
    for attempt in range(RETRIES):
        for base in MIRRORS:
            r = _get(s, f"{base}/ads.php?md5={md5}", DL_PAGE_TIMEOUT,
                     headers={"Referer": f"{base}/"})
            if r is None or _overloaded(r) or r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "lxml")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "get.php" in href and "key=" in href:
                    if href.startswith("http"):
                        return base, href
                    return base, f"{base}/{href.lstrip('/')}"
        time.sleep(2 * (attempt + 1))
    return None


# ── Download ────────────────────────────────────────────────────────────────

def download(index: int | None = None, md5: str | None = None,
             ext: str = "", title: str = "", author: str = "",
             year: str = "", topic: str | None = None,
             auto: bool = False) -> str:
    """Download a chosen book into Source Material. Returns markdown status.

    Pass ``index`` (1-based, from the most recent search_md), an explicit ``md5``
    + metadata, or ``auto=True`` to pick the best real edition of the last search.
    """
    # Resolve the target Book — from the cached search by index/auto, or from args.
    book: Book | None = None
    if auto and index is None and not md5:
        cached = _load_cached()
        if not cached:
            return ("I don't have a recent search to auto-pick from. Search for the "
                    "book first.")
        book = _best_pick(cached, prefer_ext=(ext or "epub"))
        if book is None:
            return "Couldn't pick a good edition from the last search."
    elif index is not None:
        cached = _load_cached()
        if not cached:
            return ("I don't have a recent search to pick from. Search for the "
                    "book first, then tell me the number.")
        if index < 1 or index > len(cached):
            return (f"That number is out of range — the last search had "
                    f"{len(cached)} result(s). Pick 1–{len(cached)}.")
        book = cached[index - 1]
    elif md5:
        if not re.fullmatch(r"[0-9A-Fa-f]{32}", md5):
            return f"Error: '{md5}' is not a valid md5."
        book = Book(title=title or md5, authors=author, year=year,
                    ext=(ext or "").lower().lstrip("."), md5=md5.lower())
    else:
        return "Error: need either an index (from a search) or an md5."

    if not book.ext:
        book.ext = "bin"

    folder = choose_folder(book, topic=topic)
    dest = folder / _filename(book)
    rel = dest.relative_to(VAULT)

    if dest.exists() and dest.stat().st_size > 0:
        _write_resource_note(book, dest)          # ensure it's catalogued
        return (f"📚 **{book.title}** is already in your library:\n"
                f"`{rel}` — nothing to download (it's in Library).")

    s = _session()
    resolved = _resolve_download_url(s, book.md5)
    if not resolved:
        return (f"Couldn't get a download link for **{book.title}** — the Libgen "
                f"mirrors are overloaded or the file is gone. Try again shortly.")
    mirror, dl_url = resolved

    tmp = dest.with_suffix(dest.suffix + ".part")
    # Libgen mirrors intermittently drop a stream mid-file (IncompleteRead) or
    # rate-limit. Retry a few times, RESUMING from the bytes already on disk via a
    # Range request so a near-complete download isn't thrown away and refetched.
    attempts, total, err = 4, 0, None
    for attempt in range(1, attempts + 1):
        have = tmp.stat().st_size if tmp.exists() else 0
        hdrs = {"Referer": f"{mirror}/"}
        if have:
            hdrs["Range"] = f"bytes={have}-"
        try:
            with s.get(dl_url, timeout=DOWNLOAD_TIMEOUT, stream=True, headers=hdrs) as r:
                if r.status_code == 416:                 # Range past EOF → already complete
                    err = None
                    total = tmp.stat().st_size
                    break
                if r.status_code not in (200, 206):
                    err = f"HTTP {r.status_code}"
                    time.sleep(2 * attempt); continue
                if "text/html" in r.headers.get("Content-Type", ""):
                    return (f"The mirror returned a web page instead of a file for "
                            f"**{book.title}** — it's likely rate-limiting. Retry soon.")
                # If we asked to resume but the server ignored Range (sent 200, not
                # 206), start over from scratch instead of appending to a partial.
                mode = "ab" if (have and r.status_code == 206) else "wb"
                if mode == "wb":
                    tmp.unlink(missing_ok=True)
                want = None                              # expected FINAL size, if known
                cr = r.headers.get("Content-Range", "")
                if cr and "/" in cr and cr.rsplit("/", 1)[1].isdigit():
                    want = int(cr.rsplit("/", 1)[1])
                elif r.headers.get("Content-Length", "").isdigit():
                    want = (have if mode == "ab" else 0) + int(r.headers["Content-Length"])
                with tmp.open(mode) as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        if chunk:
                            f.write(chunk)
            total = tmp.stat().st_size
            if want and total < want:                    # short read → resume next loop
                err = f"incomplete ({total}/{want} bytes)"
                time.sleep(2 * attempt); continue
            err = None
            break
        except Exception as e:                           # keep the .part for resume
            err = str(e)
            time.sleep(2 * attempt)
            continue
    if err:
        tmp.unlink(missing_ok=True)
        return (f"Download of **{book.title}** kept failing after {attempts} tries "
                f"({err}). That mirror's flaky right now — try again or pick another edition.")

    if total == 0:
        tmp.unlink(missing_ok=True)
        return f"Got an empty file for **{book.title}** — mirror issue. Retry."

    if not _verify(tmp, book.ext):
        bad = tmp.with_suffix(tmp.suffix + ".badformat")
        tmp.rename(bad)
        return (f"⚠️ Downloaded **{book.title}** but it doesn't look like a valid "
                f"`{book.ext}` (saved as `{bad.relative_to(VAULT)}` for inspection). "
                f"Try another edition.")

    tmp.rename(dest)
    # Catalog it so it appears in Library (the binary alone is invisible to Dataview),
    # and VERIFY both artifacts exist before we claim success.
    note = _write_resource_note(book, dest)
    if not dest.exists() or not note.exists():
        return (f"⚠️ Downloaded **{book.title}** but couldn't verify it on disk "
                f"(file or catalog note missing). Check `{rel}`.")
    mb = total / (1024 * 1024)
    return (f"✅ Downloaded **{book.title}**"
            f"{(' — ' + book.authors) if book.authors else ''}\n"
            f"`{rel}` ({mb:.1f} MB, {book.ext}) — added to Library. "
            f"It'll sync to your Supernote.")


# ── Library resource note ─────────────────────────────────────────────────────
# A downloaded binary is INVISIBLE to Library.md, which Dataview-queries
# `type == "resource"` notes from Source Material. So every book needs a companion
# .md note carrying the fields the Library reads (author/genre/status/rating) plus a
# `file:` field naming the real binary. The note is named by the clean book TITLE
# (not the long libgen filename) so the Library table stays readable, and the
# `file:` field keeps the note↔binary pairing robust even though names now differ.

_BOOK_EXT = {".pdf", ".epub", ".djvu", ".mobi", ".azw3", ".cbz"}

# Physical shelf (folder) → a real-library genre/subject label. `genre` is a plain
# frontmatter field, so any single book's genre can be overridden by hand.
_FOLDER_GENRE = {
    "Programming (C++)": "Computer Science",
    "Programming": "Computer Science",
    "Linux": "Computer Science",
    "Math": "Mathematics",
    "Manga": "Graphic Novels & Manga",
    "Art Books": "Art & Design",
    "Instruction Manuals": "Reference & Manuals",
}


def _genre_for(folder_name: str) -> str:
    return _FOLDER_GENRE.get(folder_name, folder_name)


def _yaml_scalar(val: str) -> str:
    """Quote a value for a YAML frontmatter scalar when it needs it (book titles
    are full of colons/brackets that would otherwise break the frontmatter)."""
    val = (val or "").strip()
    if not val:
        return ""
    if re.search(r'[:#\[\]{}",&*!|>%@`]', val) or val[0] in "-?":
        return '"' + val.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return val


def _clean_title(stem: str) -> str:
    """Best-effort clean book title from a messy libgen filename: drop a trailing
    (Year), cut at the first author/publisher separator, drop `; …` / `[ …` tails."""
    s = stem
    m = re.search(r"\s*\((\d{4})\)\s*$", s)
    if m:
        s = s[: m.start()]
    for sep in (" -- ", " - "):
        if sep in s:
            s = s.split(sep, 1)[0]
            break
    s = re.split(r"\s*[;\[]", s)[0]
    return s.strip().strip("_ ").strip() or stem


def _note_file_field(note: Path) -> str:
    """The `file:` binary name a resource note points at (''/none if absent)."""
    try:
        for ln in note.read_text(errors="ignore").splitlines()[:20]:
            m = re.match(r'file:\s*"?(.+?)"?\s*$', ln)
            if m:
                return m.group(1).strip()
    except Exception:
        pass
    return ""


def _existing_note_for_file(dest: Path) -> Path | None:
    """Find a resource note in dest's folder that already points at this binary."""
    for note in dest.parent.glob("*.md"):
        if _note_file_field(note) == dest.name:
            return note
    return None


def _resource_note_path(dest: Path, title: str) -> Path:
    """Path for a book's note, named by clean TITLE, de-duped against a same-title
    note that points at a DIFFERENT file."""
    folder = dest.parent
    base = _sanitize(title) or dest.stem
    cand = folder / f"{base}.md"
    n = 2
    while cand.exists() and _note_file_field(cand) not in ("", dest.name):
        cand = folder / f"{base} ({n}).md"
        n += 1
    return cand


def _write_resource_note(book: Book, dest: Path) -> Path:
    """Create the companion catalog note if one doesn't already point at this file.
    Idempotent — never clobbers an existing note (status/rating/genre edits survive)."""
    existing = _existing_note_for_file(dest)
    if existing:
        return existing
    note = _resource_note_path(dest, book.title)
    genre = _genre_for(dest.parent.name)
    today = time.strftime("%Y-%m-%d")
    author = re.split(r"[;,]", book.authors or "")[0].strip()
    fm = [
        "---",
        "type: resource",
        'index: "[[Library]]"',
        f"title: {_yaml_scalar(book.title)}",
        f"author: {_yaml_scalar(author)}",
        f"year: {_yaml_scalar(book.year)}",
        f"format: {book.ext or ''}",
        f"genre: {_yaml_scalar(genre)}",
        f"file: {_yaml_scalar(dest.name)}",
        "status: unread",
        "rating: ",
        "source: libgen",
        f"date_clipped: {today}",
        "tags: [book]",
        "---",
        "",
        f"# {book.title}",
        "",
        _STATUS_WIDGET,
        "",
        f"📖 **File:** [[{dest.stem}]]",
        "",
        "## Notes",
        "- ",
        "",
    ]
    note.write_text("\n".join(fm))
    return note


# One-press status buttons as a self-contained DataviewJS widget: it patches THIS
# note's own frontmatter directly (no Buttons/Templater/QuickAdd wiring to break).
# Renders three buttons in reading/live-preview; needs Dataview JS queries enabled.
_STATUS_WIDGET = """```dataviewjs
const path = dv.current().file.path;
const set = async (status) => {
  const f = app.vault.getAbstractFileByPath(path);
  await app.fileManager.processFrontMatter(f, (fm) => {
    const today = window.moment().format("YYYY-MM-DD");
    fm.status = status;
    if (status === "reading") { fm.started = today; delete fm.finished; }
    else if (status === "read") { fm.finished = today; }
    else { delete fm.started; delete fm.finished; }
  });
  new Notice("Status \\u2192 " + status);
};
for (const [label, st] of [["\\ud83d\\udcd6 Reading","reading"],["\\u2705 Read","read"],["\\u25cb Unread","unread"]]) {
  const b = dv.container.createEl("button", { text: label });
  b.style.marginRight = "8px";
  b.onclick = () => set(st);
}
```"""


def _book_from_filename(f: Path) -> Book:
    """Reverse the `Title -- Author (Year).ext` filename convention into a Book,
    for backfilling notes over books downloaded before this existed."""
    stem = f.stem
    year = ""
    m = re.search(r"\((\d{4})\)\s*$", stem)
    if m:
        year = m.group(1)
    _, sep, rest = stem.partition(" -- ")
    author = re.split(r"\s+--\s+", rest)[0].strip() if sep else ""  # drop isbn/md5 tails
    author = re.sub(r"\s*\(\d{4}\)\s*$", "", author)               # strip trailing (Year)
    author = re.sub(r"^by\s+", "", author, flags=re.I).strip()      # strip leading "by "
    return Book(title=_clean_title(stem), authors=author, year=year,
                ext=f.suffix.lower().lstrip("."))


def backfill_library() -> str:
    """One-time: give every existing Source Material book file a resource note so
    it shows up in Library. Only ever WRITES new .md notes (reversible)."""
    if not SOURCE_DIR.is_dir():
        return f"Source Material folder not found: {SOURCE_DIR}"
    made: list[str] = []
    had = 0
    for f in sorted(SOURCE_DIR.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in _BOOK_EXT:
            continue
        if _existing_note_for_file(f):
            had += 1
            continue
        note = _write_resource_note(_book_from_filename(f), f)
        made.append(str(note.relative_to(VAULT)))
    lines = [f"📚 Library backfill: {len(made)} note(s) created, {had} already present."]
    lines += [f"  + {m}" for m in made]
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────

def _main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Book grabber (Libgen)")
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("search")
    ps.add_argument("query", nargs="+")
    ps.add_argument("--ext", default=None, help="filter by format, e.g. pdf/epub")

    sub.add_parser("backfill", help="give existing Source Material books a Library note")

    pd = sub.add_parser("download")
    pd.add_argument("--index", type=int, default=None)
    pd.add_argument("--md5", default=None)
    pd.add_argument("--ext", default="")
    pd.add_argument("--title", default="")
    pd.add_argument("--author", default="")
    pd.add_argument("--year", default="")
    pd.add_argument("--topic", default=None)

    args = p.parse_args(argv)
    if args.cmd == "backfill":
        print(backfill_library())
    elif args.cmd == "search":
        print(search_md(" ".join(args.query), ext=args.ext))
    elif args.cmd == "download":
        print(download(index=args.index, md5=args.md5, ext=args.ext,
                       title=args.title, author=args.author, year=args.year,
                       topic=args.topic))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
