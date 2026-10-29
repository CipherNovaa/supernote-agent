#!/usr/bin/env python3
"""Convert Obsidian vault .md files to PDF for Supernote reading."""

import argparse
import base64
import re
import sys
from io import BytesIO
from pathlib import Path

# Single source of truth for vault structure — no hardcoded folder names.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sn_config as mp

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import markdown
from weasyprint import HTML, CSS

VAULT = mp.vault_root()
OUTPUT = mp.pdf_output()

# Which folders become PDFs (and their flat output subdir) is defined ONCE in
# config.toml -> pdf_areas, read via sn_config. Renaming a vault
# folder is now a one-line edit there; this script can no longer silently map
# everything to "skip" and produce zero PDFs (the old PDF_CATEGORIES bug).

_EXCLUDE_PARTS = {"_Archive", "Archive", ".trash"}


def pdf_out_subdir(rel) -> str | None:
    """Flat output subdir (a pdf_area's 'out' name) for a note's VAULT-RELATIVE path,
    or None if the note is outside every configured PDF area / in an excluded folder
    (→ skip). Reverse of main()'s area walk; both read the single source of truth
    mp.pdf_areas(). Imported by the Supernote watcher (poll_vault_pdfs)."""
    rel = Path(rel)
    if _EXCLUDE_PARTS & set(rel.parts):
        return None
    rp = rel.as_posix()
    best_len, best_out = -1, None
    for src, out in mp.pdf_areas():
        s = Path(src).as_posix().rstrip("/")
        if (rp == s or rp.startswith(s + "/")) and len(s) > best_len:
            best_len, best_out = len(s), out
    return best_out


def _alert(msg: str) -> None:
    """Best-effort loud failure: ping Discord; never raise."""
    try:
        mp.send_discord(msg)
    except Exception:
        pass

CSS_STYLE = CSS(string="""
    @page {
        size: A5;
        margin: 12mm 10mm 12mm 10mm;
    }
    body {
        font-family: 'Iosevka Nerd Font Propo', 'Iosevka Nerd Font', 'DejaVu Sans', sans-serif;
        font-size: 13pt;
        line-height: 1.55;
        color: #000;
        background: #fff;
    }
    h1 { font-size: 18pt; margin: 1.2em 0 0.4em; border-bottom: 1.5px solid #000; padding-bottom: 2px; }
    h2 { font-size: 15pt; margin: 1em 0 0.3em; }
    h3 { font-size: 13pt; margin: 0.8em 0 0.2em; font-style: italic; }
    p { margin: 0.5em 0; }
    ul, ol { margin: 0.4em 0 0.4em 1.4em; padding: 0; }
    li { margin: 0.2em 0; }
    code {
        font-family: 'Iosevka Nerd Font Mono', 'Iosevka NFM', 'DejaVu Sans Mono', monospace;
        font-size: 10.5pt;
        background: #f0f0f0;
        padding: 1px 4px;
        border-radius: 2px;
    }
    pre {
        font-family: 'Iosevka Nerd Font Mono', 'Iosevka NFM', 'DejaVu Sans Mono', monospace;
        font-size: 10pt;
        background: #f0f0f0;
        padding: 8px 10px;
        border-left: 3px solid #555;
        overflow-x: auto;
        white-space: pre-wrap;
        word-break: break-all;
    }
    pre code { background: none; padding: 0; }
    blockquote {
        border-left: 3px solid #555;
        margin: 0.5em 0 0.5em 0;
        padding: 2px 10px;
        color: #333;
    }
    hr { border: none; border-top: 1px solid #aaa; margin: 1em 0; }
    strong { font-weight: bold; }
    em { font-style: italic; }
    table { border-collapse: collapse; width: 100%; margin: 0.5em 0; }
    th, td { border: 1px solid #888; padding: 4px 8px; text-align: left; }
    th { background: #eee; font-weight: bold; }
    .note-header {
        font-size: 10pt;
        color: #444;
        border-bottom: 1px solid #ccc;
        margin-bottom: 1em;
        padding-bottom: 6px;
    }
""")


def wikilink_to_text(text: str) -> str:
    """Convert [[Note|alias]] → alias, [[Note]] → Note."""
    def replace(m):
        inner = m.group(1)
        if '|' in inner:
            return inner.split('|', 1)[1]
        return inner
    return re.sub(r'\[\[([^\]]+)\]\]', replace, text)


def render_callouts(text: str) -> str:
    """Flatten Obsidian callouts (`> [!tip]- Title`) into a normal blockquote with
    a bold title line, so the python-markdown converter renders them as readable
    quotes instead of leaving raw `[!tip]` syntax on the page."""
    def repl(m):
        typ = m.group(1).lower()
        title = (m.group(3) or '').strip()
        if typ in ('quote', 'cite') and not title:
            return '>'                       # plain blockquote, no noisy label
        if title:
            return f'> **{title}**'
        return f'> **{typ.capitalize()}**'
    return re.sub(r'^>\s*\[!(\w+)\]([-+]?)\s*(.*)$', repl, text, flags=re.MULTILINE)


# Decorative color emoji don't exist in the Iosevka/DejaVu fallback fonts, so on the
# (monochrome) Supernote they render as missing-glyph boxes. Map the ones used in the
# notes to clean text/symbols, and drop the purely decorative ones.
_EMOJI_MAP = {
    '✅': '✓', '❌': '✗', '⚠️': '⚠', '☑️': '✓',
}
_EMOJI_STRIP = '🌅🎯🧱🆘🌙🔗☀️🌱🧠📝📌🚀🔥😂😆😄😎🙂😅💡🧩📊🗂️🗃️📁'


def clean_emoji_for_eink(text: str) -> str:
    for k, v in _EMOJI_MAP.items():
        text = text.replace(k, v)
    for ch in _EMOJI_STRIP:
        text = text.replace(ch, '')
    text = text.replace('️', '')        # stray variation selectors
    return text


def _latex_to_img_tag(latex: str, display: bool) -> str:
    """Render a LaTeX expression to a base64 PNG img tag via matplotlib mathtext."""
    fontsize = 16 if display else 14
    expr = f'${latex}$'
    fig = None
    try:
        fig = plt.figure(figsize=(0.01, 0.01))
        fig.text(0.5, 0.5, expr, fontsize=fontsize,
                 ha='center', va='center', color='black')
        buf = BytesIO()
        fig.savefig(buf, format='png', bbox_inches='tight',
                    transparent=True, dpi=180, pad_inches=0.04)
        buf.seek(0)
        b64 = base64.b64encode(buf.read()).decode('ascii')
        if display:
            return (f'<div style="text-align:center;margin:0.7em 0">'
                    f'<img src="data:image/png;base64,{b64}" '
                    f'style="max-width:100%;vertical-align:middle"/></div>')
        else:
            return (f'<img src="data:image/png;base64,{b64}" '
                    f'style="vertical-align:middle;max-height:1.8em"/>')
    except Exception:
        return f'<code>{latex}</code>'
    finally:
        # Always release the figure — the old code only closed on the success
        # path, so every failed LaTeX render leaked one (→ "More than 20
        # figures opened" warning and growing memory).
        if fig is not None:
            plt.close(fig)


def render_latex(text: str) -> str:
    """Convert LaTeX math blocks to inline PNG images. Handles $$...$$ and $...$."""
    text = re.sub(r'\$\$(.*?)\$\$',
                  lambda m: _latex_to_img_tag(m.group(1).strip(), display=True),
                  text, flags=re.DOTALL)
    text = re.sub(r'(?<!\$)\$(?!\$)(.*?)(?<!\$)\$(?!\$)',
                  lambda m: _latex_to_img_tag(m.group(1).strip(), display=False),
                  text)
    return text


def strip_obsidian_header(text: str) -> str:
    """Pull date/status from the dashed-separator header into a small header div."""
    lines = text.split('\n')
    sep = '________________________________________________________________________'
    if lines and lines[0].strip() == sep:
        header_parts = []
        content_start = 0
        i = 0
        while i < len(lines) and i < 20:
            if lines[i].strip() == sep:
                i += 1
                if i < len(lines) and lines[i].strip() != sep:
                    val = lines[i].strip()
                    if val:
                        header_parts.append(val)
                    i += 1
            else:
                content_start = i
                break
        if header_parts:
            header_html = '<div class="note-header">' + ' &nbsp;|&nbsp; '.join(
                h.replace('[[', '').replace(']]', '') for h in header_parts
            ) + '</div>\n'
            return header_html, '\n'.join(lines[content_start:])
    return '', text


def md_to_html(md_text: str, title: str) -> str:
    header_html, body = strip_obsidian_header(md_text)
    body = wikilink_to_text(body)
    body = render_callouts(body)
    body = clean_emoji_for_eink(body)
    body = render_latex(body)

    md = markdown.Markdown(extensions=['tables', 'fenced_code'])
    body_html = md.convert(body)

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>{title}</title></head>
<body>
<h1>{title}</h1>
{header_html}{body_html}
</body>
</html>"""


def convert_file(md_path: Path, out_dir: Path) -> bool:
    text = md_path.read_text(encoding='utf-8')
    if not text.strip():
        return False  # skip empty files

    title = md_path.stem
    html = md_to_html(text, title)

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (md_path.stem + '.pdf')
    HTML(string=html, base_url=str(md_path.parent)).write_pdf(
        out_path, stylesheets=[CSS_STYLE]
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description='Convert Obsidian vault to PDFs for Supernote')
    parser.add_argument('--vault', type=Path, default=VAULT)
    parser.add_argument('--output', type=Path, default=OUTPUT)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--force', action='store_true',
                        help='re-render every note even if its PDF is already current')
    args = parser.parse_args()

    vault = args.vault.expanduser()
    output = args.output.expanduser()

    # Build the work list from the configured PDF areas (sn_config source of truth).
    work = []            # (md_path, out_dir)
    empty_areas = []
    for src, out in mp.pdf_areas():
        src_dir = vault / src
        if not src_dir.exists():
            empty_areas.append(f"{src} (MISSING)")
            continue
        found = 0
        for md in sorted(src_dir.rglob('*.md')):
            if _EXCLUDE_PARTS & set(md.relative_to(vault).parts):
                continue
            work.append((md, output / out))
            found += 1
        if found == 0:
            empty_areas.append(src)

    print(f"Found {len(work)} .md files across {len(mp.pdf_areas())} areas  (layout: {mp.layout()})")
    print(f"Output: {output}")
    if empty_areas:
        print(f"⚠ empty/missing areas: {', '.join(empty_areas)}", file=sys.stderr)

    # LOUD FAILURE — never silently produce nothing (the old breakage).
    if not work:
        msg = ("❌ vault_to_pdf: 0 notes matched the configured PDF areas — a folder was "
               "probably renamed. Fix pdf_areas in config.toml. No PDFs written.")
        print(msg, file=sys.stderr)
        if not args.dry_run:        # dry-run is a test; don't spam Discord
            _alert(msg)
        return 2

    if args.dry_run:
        for md, out_dir in work:
            print(f"  {md.relative_to(vault)}  ->  {out_dir.name}/")
        return 0

    converted = skipped = unchanged = errors = 0
    for md, out_dir in work:
        rel = md.relative_to(vault)
        # Incremental: skip when the PDF already exists and is newer than its
        # source .md — keeps the daily export cheap and stops Syncthing from
        # re-shipping every PDF each morning. --force overrides.
        if not args.force:
            out_path = out_dir / (md.stem + '.pdf')
            try:
                if out_path.exists() and out_path.stat().st_mtime >= md.stat().st_mtime:
                    unchanged += 1
                    continue
            except OSError:
                pass
        try:
            ok = convert_file(md, out_dir)
            if ok:
                converted += 1
                print(f"  ✓ {rel}")
            else:
                skipped += 1
                print(f"  - {rel} (empty, skipped)")
        except Exception as e:
            errors += 1
            print(f"  ✗ {rel}: {e}", file=sys.stderr)

    print(f"\nDone: {converted} converted, {unchanged} unchanged, {skipped} empty, {errors} errors")

    # Loud failure if literally everything errored (another silent-zero case).
    if converted == 0 and errors:
        _alert(f"❌ vault_to_pdf: all {errors} note(s) failed to convert — check the logs.")
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
