#!/usr/bin/env python3
"""
scan_to_book.py — turn phone photos of pages into a clean, annotatable PDF that
lands where the Supernote can open and write on it ("work in the book").

Two modes:
  • CLI:    scan_to_book.py "Calculus Ch3" page1.jpg page2.jpg ...
            → one multi-page PDF "Calculus Ch3.pdf"
  • Inbox:  scan_to_book.py --inbox
            → each SUBFOLDER of the scan inbox becomes one book PDF (all images
              in it, in sorted order); loose images each become a 1-page PDF.

Output goes to  <pdf_output>/Scans/<name>.pdf  — that folder already
mirrors to the Supernote, so the book shows up on the device to read + annotate.

Cleanup is deliberately gentle (auto-orient, autocontrast, light sharpen, cap
resolution) so diagrams/grey shading survive — it is NOT hard black/white
thresholding. opencv/img2pdf are optional; everything works with PIL + numpy.
"""
from __future__ import annotations

import sys
import re
from pathlib import Path
from datetime import datetime

from PIL import Image, ImageOps, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sn_config as mp

# Optional HEIC (iPhone) support — register if present; phone JP/PNG always work.
try:
    import pillow_heif  # type: ignore
    pillow_heif.register_heif_opener()
except Exception:
    pass

VAULT      = mp.vault_root()
OUT_DIR    = mp.pdf_output() / "Scans"
INBOX      = mp.scan_inbox()
IMG_EXTS   = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".bmp", ".tif", ".tiff"}
MAX_EDGE   = 2200     # cap long edge (px) — keeps PDFs small, stays sharp on e-ink
DPI        = 150
STABLE_SECS = 90      # wait this long after the last new image before building a book
                      # (so a batch still being added isn't processed half-finished)


def _safe(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "", name).strip() or "scan"


def clean_page(path: Path) -> Image.Image:
    """Load one photo and make it a clean, readable page image."""
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)          # honour phone rotation
    img = img.convert("RGB")
    # downscale very large phone shots (12MP+) to keep the book light
    w, h = img.size
    scale = MAX_EDGE / max(w, h)
    if scale < 1:
        img = img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
    img = ImageOps.autocontrast(img, cutoff=1)  # lift faded pencil / even lighting
    img = img.filter(ImageFilter.UnsharpMask(radius=1.5, percent=80, threshold=2))
    return img


def build_book(images: list[Path], title: str) -> Path | None:
    """Combine page photos (in given order) into one multi-page PDF."""
    pages = []
    for p in images:
        try:
            pages.append(clean_page(p))
        except Exception as e:
            print(f"[scan] skip {p.name}: {e}")
    if not pages:
        print("[scan] no usable images")
        return None
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{_safe(title)}.pdf"
    pages[0].save(out, "PDF", resolution=DPI, save_all=True,
                  append_images=pages[1:])
    print(f"[scan] {out.name} — {len(pages)} page(s) → {out}")
    return out


def _sorted_images(folder: Path) -> list[Path]:
    return sorted([p for p in folder.iterdir()
                   if p.is_file() and p.suffix.lower() in IMG_EXTS],
                  key=lambda p: p.name.lower())


def _stable(paths: list[Path]) -> bool:
    """True once nothing in the set was modified within STABLE_SECS — i.e. the
    upload/copy has finished. Avoids building a book mid-batch."""
    import time as _t
    if not paths:
        return False
    newest = max(p.stat().st_mtime for p in paths)
    return (_t.time() - newest) >= STABLE_SECS


def process_inbox() -> list[Path]:
    """Each subfolder of the scan inbox → one book; loose images → 1-page books.
    Processed images are moved to <scan done>/<stamp>/ so the inbox stays clean."""
    made: list[Path] = []
    if not INBOX.exists():
        INBOX.mkdir(parents=True, exist_ok=True)
        print(f"[scan] created inbox {INBOX} — drop page photos here")
        return made
    done = mp.scan_done() / datetime.now().strftime("%Y%m%d-%H%M%S")
    # subfolders = multi-page books
    for sub in sorted([d for d in INBOX.iterdir() if d.is_dir()]):
        imgs = _sorted_images(sub)
        if not imgs or not _stable(imgs):
            continue
        out = build_book(imgs, sub.name)
        if out:
            made.append(out)
            dest = done / sub.name
            dest.mkdir(parents=True, exist_ok=True)
            for p in imgs:
                p.rename(dest / p.name)
    # loose images = single-page books
    loose = _sorted_images(INBOX)
    if loose and _stable(loose):
        for img in loose:
            out = build_book([img], img.stem)
            if out:
                made.append(out)
                done.mkdir(parents=True, exist_ok=True)
                img.rename(done / img.name)
    for out in made:
        _notify(out)
    return made


def _notify(pdf: Path):
    """Ping Discord that a scanned book is ready on the Supernote."""
    try:
        rel = pdf.relative_to(VAULT)
    except ValueError:
        rel = pdf
    msg = (f"📚 Scanned book ready: **{pdf.stem}** — it's in your Supernote PDFs "
           f"(`{rel}`), open it to write in it.")
    if mp.send_discord(msg) is None:
        print("[scan] discord notify failed")


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--inbox":
        made = process_inbox()
        print(f"[scan] {len(made)} book(s) made" if made else "[scan] inbox empty")
        return 0
    title, imgs = argv[0], [Path(a) for a in argv[1:]]
    if not imgs:
        print("usage: scan_to_book.py <title> <image>...   |   scan_to_book.py --inbox")
        return 2
    out = build_book(imgs, title)
    return 0 if out else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
