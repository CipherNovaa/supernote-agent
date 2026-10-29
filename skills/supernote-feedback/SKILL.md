---
name: supernote-feedback
description: "Handle feedback replies on Supernote-converted Obsidian notes. Activates when the user replies in a Discord thread whose original message is a Supernote conversion notification (starting with Note: or a pending note marker). Updates style memory and/or corrects the specific note."
platforms: [linux, macos, windows]
---

# Supernote Feedback Handler

> An agent skill (Hermes Agent `SKILL.md` format) that lets a general chat agent handle
> note feedback when it, rather than `supernote_feedback.py`, sees the reply. Replace
> `<VAULT>` and `<STATE_DIR>` with the values from your `config.toml`.

## When this skill applies

This skill activates when you receive a message **in a Discord thread** whose earliest bot message matches the Supernote notification format:

```
✅ Note: {title}        ← successful conversion
🏷️ Tags: [[Tag1]], ...
📁 Filed: {vault-relative path}
```
or
```
⏳ Note: {title}        ← pending/stub conversion
...
📁 Filed: {vault-relative path}
```

The user is giving feedback on that specific converted note or on how future notes should be formatted.

---

## Step 1 — Parse the notification thread

Read the thread history. Find the earliest message starting with `✅ Note:` or `⏳ Note:` and extract:

| Field | From line | Example |
|-------|-----------|---------|
| `title` | `Note: {title}` | `Sourdough Starter` |
| `rel_path` | `📁 Filed: {path}` | `Inbox/Sourdough Starter.md` |
| `full_path` | `<VAULT>/{rel_path}` | `<VAULT>/Inbox/Sourdough Starter.md` |

**Vault root:** `<VAULT>`  
**Style file:** `<STATE_DIR>/style.md`  
**Tags dir:** `<VAULT>/Tags/`

Also check the metadata lookup file (if it exists):
`<STATE_DIR>/pending/{message_id}.json`

---

## Step 2 — Also check the metadata file

A JSON metadata file may exist at:
```
<STATE_DIR>/pending/{thread_id}.json
```

If it exists, read it. It contains:
```json
{
  "thread_id": "...",
  "note_path": "Inbox/Sourdough Starter.md",
  "note_title": "Sourdough Starter",
  "timestamp": "2026-06-01T14:50:00"
}
```

This gives you the authoritative note path even if the thread history is ambiguous.

---

## Step 3 — Classify the user's feedback

### Style rule (affects ALL future notes)
The user describes a general formatting preference:
- "gaming notes should always have #Gaming tag"
- "don't use bold for definitions, use ==highlight== instead"
- "hardware idea notes always need an [[Ideas]] tag"
- "don't number duplicate titles, use the date instead"

### Note correction (affects THIS note only)
The user wants to fix the specific note that was just converted:
- "the title should be 'Sourdough Starter v2'"
- "remove the [[HasMeetS]] tag, that's an OCR error"
- "add #Gaming tag to this one"
- "reformat the second paragraph as a list"

### Both
Some feedback is both (fix the current note AND add a rule):
- "the tag [[HasMeetS]] is wrong — OCR errors shouldn't become tags"
  → Remove the tag from the note AND add a style rule about OCR artifacts

**When in doubt:** Apply it both ways.

---

## Step 4 — Apply the feedback

### For style rules

Use `terminal` to APPEND one bullet to the style file — never rewrite it.
The bullet MUST start with `- ` (dash-space), not `*`:

```bash
echo '- rule in imperative form' >> <STATE_DIR>/style.md
```

Keep it short — one bullet. Use the user's own words where possible.

**Examples:**
- "gaming notes should always have #Gaming tag" → `echo '- Gaming notes always get a [[Gaming]] tag' >> <STATE_DIR>/style.md`
- "don't use * for bullets, use -" → `echo '- Use - not * for bullet lists' >> <STATE_DIR>/style.md`

CRITICAL: Use `echo '...' >>` (append redirect). NEVER `write_file` or `echo >` (single `>` overwrites). The style file has many rules; destroying them is permanent.

### For note corrections

1. Read the note at `full_path` using `read_file`.
2. Apply the change:
   - **Tag change**: edit the `topics:` list in the frontmatter
   - **Title rename**: rename the file and the `# {title}` heading; leave `source:` alone
   - **Content edit**: use `patch` for targeted edits, `write_file` for full rewrites
3. Write it back.

**Note format reminder** (preserve exactly — the watcher writes YAML properties):
```
---
type: supernote
created: 2026-06-01 14:50
modified: 2026-06-01 14:50
status: captured
tags:
  - supernote
  - hermes
topics:
  - "[[Tag1]]"
  - "[[Tag2]]"
source: "[[Supernote/Raw/{stem}.note]]"
---
# {title}

{body}
<!-- sn:end -->
{anything below the marker is a computer edit and survives re-syncs}
```

### For new tags (from style rules)

If a style rule creates a tag that doesn't exist yet, create the tag file:
```
write_file(<VAULT>/Tags/{TagName}.md, "")
```

---

## Step 5 — Reply with confirmation

Keep it short. One line.

| Action taken | Reply |
|---|---|
| Style rule added | `Got it — added to style memory ✅` |
| Note corrected | `Fixed ✅` |
| Both | `Fixed + added to style memory ✅` |
| Ambiguous/both by default | `Got it — fixed the note and added to style memory ✅` |

Do **not** echo the rule back. Do **not** explain what you did in detail. Just confirm.

---

## Important notes

- Vault paths contain spaces — always use file tools (`read_file`, `write_file`, `patch`), never bare shell commands with unquoted paths
- If the note file no longer exists at `full_path` (e.g. it was renamed), search `Inbox/` for a note with a matching title
- If the user's feedback is clearly unrelated to Supernote (e.g. they're asking a general question), skip this skill and respond normally
- After applying a style rule, do NOT re-convert the note — the rule applies to future notes only (unless the user also requests a note correction)
