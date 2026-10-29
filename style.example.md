# Supernote Note Style Rules

<!-- The formatter reads this file on every note. Copy it to the style_file path
     in config.toml (default ~/.local/state/supernote-agent/style.md).
     The Discord feedback loop APPENDS new "- rule" lines at the bottom, so it
     grows as you correct notes; edit or prune it by hand any time. -->

- Use bullet points with `-` not `*`
- Use ==highlight== for key terms and definitions
- Lines starting with `#` in raw text become `##` headings in output
- Keep the author's voice — clean up OCR artefacts but don't rewrite
- If a note is about math, preserve all equations and structure
- Recipe notes list ingredients as a bullet list before the steps
- Book, film and game titles always get their own tag e.g. [[Title Name]]
- Notes about ideas or inventions get an [[Ideas]] tag

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

## Learned from feedback
- Gardening notes always get a [[Gardening]] tag
- Never turn a single-line note into a heading
