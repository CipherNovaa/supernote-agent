#!/usr/bin/env python3
"""
reminder_set.py — Hermes reminder engine, backed by `hermes cron`.

Turns a plain-text reminder + a schedule into a real scheduled job that pings
the user on Discord at the right time. Built on the cron "no-agent script" mode:
each reminder gets a tiny generated script under ~/.hermes/scripts/ that simply
prints the reminder text, and a cron job delivers that stdout verbatim. No LLM
runs when the reminder fires — it's deterministic, fully local, and the exact
text is what gets sent (no rewording).

This is the actuator behind the idea-planner's "⏰ Suggested reminders": the
planner proposes them; this turns a chosen one into a scheduled job.

Schedule formats (passed straight to `hermes cron`, which validates them):
  * relative  : '30m', '2h', '1d'                 → fire once, that far out
  * cron 5-fld: '0 9 * * *' (9am daily),
                '0 10 * * 1' (10am every Monday),
                '0 9 10 6 *' (9am on Jun 10)       → recurring unless one-shot
A one-shot reminder (recurring=False) adds `--repeat 1` so it fires once.

Usage:
    reminder_set.py "drink water" "2h"
    reminder_set.py --recurring "weekly review" "0 10 * * 1"
    reminder_set.py --list
    reminder_set.py --cancel reminder-weekly-review-a1b2
"""

import os
import re
import sys
import json
import shutil
import hashlib
import secrets
import argparse
import subprocess
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
SCRIPTS_DIR = HERMES_HOME / "scripts"
SEEN_FILE   = HERMES_HOME / "cron" / "remind_seen.json"
DELIVER     = "discord"          # where reminders are sent
PREFIX      = "⏰ Reminder:"      # leading marker on every reminder
MAX_TEXT    = 500
_SLUG_RE    = re.compile(r"[^a-z0-9]+")


def _hermes_bin() -> str:
    """Resolve the hermes CLI (the plugin/gateway run from various cwds)."""
    return (
        shutil.which("hermes")
        or str(Path.home() / ".local/bin/hermes")
    )


def _slug(text: str) -> str:
    s = _SLUG_RE.sub("-", text.lower()).strip("-")
    return (s[:32] or "note").strip("-")


def _run(argv: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _job_exists(name: str) -> bool:
    try:
        cp = _run([_hermes_bin(), "cron", "list"])
        return name in (cp.stdout or "")
    except Exception:
        return False


def _load_seen() -> set[str]:
    try:
        return set(json.loads(SEEN_FILE.read_text()))
    except Exception:
        return set()


def _save_seen(seen: set[str]) -> None:
    try:
        SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        SEEN_FILE.write_text(json.dumps(sorted(seen)))
    except Exception:
        pass

# ── Natural time/date → cron schedule (for the Supernote //remind command) ─────

def _parse_time(s: str) -> tuple[int, int]:
    """'8:30', '8:30pm', '8pm', '20:30', '08:30 AM' → (hour24, minute)."""
    m = re.match(r"^\s*(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?\s*$", s, re.I)
    if not m:
        raise ValueError(f"unrecognised time {s!r}")
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    ampm = (m.group(3) or "").lower().replace(".", "")
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"time out of range {s!r}")
    return hour, minute


def _parse_date(s: str) -> tuple[int, int]:
    """'2026-06-10', '06-10', '6/10' → (month, day)."""
    s = s.strip()
    m = re.match(r"^\d{4}-(\d{1,2})-(\d{1,2})$", s)
    if not m:
        m = re.match(r"^(\d{1,2})[-/](\d{1,2})$", s)
    if not m:
        raise ValueError(f"unrecognised date {s!r} (use YYYY-MM-DD, MM-DD, or M/D)")
    month, day = int(m.group(1)), int(m.group(2))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise ValueError(f"date out of range {s!r}")
    return month, day


def parse_when(spec: str) -> tuple[str, bool, str]:
    """Parse a //remind spec into (cron_schedule, recurring, human_label).

    Forms (date and time, '@'-joined so the text can follow with a space):
        '8:30'                → next time it is 8:30 (today or tomorrow)
        '8:30pm'              → next 20:30
        '2026-06-10@9:00'     → 09:00 on Jun 10
        '06-10@9am'           → 09:00 on Jun 10
    Always one-shot (recurring=False); cron's --repeat 1 fires it once.
    """
    spec = (spec or "").strip().lstrip("@")
    parts = spec.split("@")
    if len(parts) == 1:
        date_str, time_str = None, parts[0]
    elif len(parts) == 2:
        date_str, time_str = parts[0], parts[1]
    else:
        raise ValueError("too many '@' in the time spec")

    hour, minute = _parse_time(time_str)
    if date_str:
        month, day = _parse_date(date_str)
        sched = f"{minute} {hour} {day} {month} *"
        human = f"{month:02d}-{day:02d} at {hour:02d}:{minute:02d}"
    else:
        sched = f"{minute} {hour} * * *"
        human = f"{hour:02d}:{minute:02d} (next occurrence)"
    return sched, False, human

# ── Create ────────────────────────────────────────────────────────────────────

def make_reminder(text: str, schedule: str, recurring: bool = False,
                  key: str | None = None) -> str:
    """Schedule a reminder. Returns a short markdown confirmation (or error).

    If ``key`` is given (used by the Supernote //remind path, which re-runs on
    every resync), the reminder is deduplicated: a deterministic name is derived
    from the key and it is scheduled at most once — repeat syncs are no-ops.
    """
    text = (text or "").strip()
    schedule = (schedule or "").strip()
    if not text:
        return "Error: reminder text is empty."
    if len(text) > MAX_TEXT:
        return f"Error: reminder text too long (>{MAX_TEXT} chars)."
    if not schedule:
        return "Error: no schedule given (e.g. '2h', '0 9 * * *')."

    hermes = _hermes_bin()
    if not Path(hermes).exists():
        return f"Error: hermes CLI not found at {hermes}."

    seen = _load_seen() if key else set()
    if key and key in seen:
        return "ℹ️ Already scheduled — not duplicating on resync."

    SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    token = hashlib.sha1(key.encode()).hexdigest()[:6] if key else secrets.token_hex(2)
    slug = _slug(text)
    name = f"reminder-{slug}-{token}"
    script_name = f"reminder_{slug.replace('-', '_')}_{token}.py"
    script_path = SCRIPTS_DIR / script_name

    if key and _job_exists(name):
        seen.add(key)
        _save_seen(seen)
        return "ℹ️ Already scheduled — not duplicating on resync."

    # The generated job script: print the reminder verbatim. json.dumps yields a
    # safe, fully-escaped Python string literal, so the text can't break out.
    body = json.dumps(f"{PREFIX} {text}")
    script_path.write_text(f"print({body})\n")

    argv = [
        hermes, "cron", "create", schedule,
        "--no-agent", "--script", script_name,
        "--deliver", DELIVER, "--name", name,
    ]
    if not recurring:
        argv += ["--repeat", "1"]

    try:
        cp = _run(argv)
    except Exception as e:
        script_path.unlink(missing_ok=True)
        return f"Error scheduling reminder: {e}"

    if cp.returncode != 0:
        script_path.unlink(missing_ok=True)
        err = (cp.stderr or cp.stdout or "unknown error").strip()
        return f"Error: hermes cron rejected the schedule — {err}"

    if key:
        seen.add(key)
        _save_seen(seen)

    out = cp.stdout or ""
    job_id = next_run = ""
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("Created job:"):
            job_id = s.split(":", 1)[1].strip()
        elif s.startswith("Next run:"):
            next_run = s.split(":", 1)[1].strip()

    kind = "recurring" if recurring else "one-time"
    when = f"\n- **Next fires:** {next_run}" if next_run else ""
    return (
        f"✅ {kind.capitalize()} reminder set.\n"
        f"- **Reminder:** {PREFIX} {text}\n"
        f"- **Schedule:** `{schedule}`{when}\n"
        f"- **Name:** `{name}`"
        f"{(' (id ' + job_id + ')') if job_id else ''}\n"
        f"- Sent to Discord when it fires. Cancel with the reminder name above."
    )

# ── List / cancel ─────────────────────────────────────────────────────────────

def list_reminders() -> str:
    hermes = _hermes_bin()
    try:
        cp = _run([hermes, "cron", "list"])
    except Exception as e:
        return f"Error listing reminders: {e}"
    out = (cp.stdout or cp.stderr or "").strip()
    return out or "No scheduled jobs."


def cancel_reminder(ref: str) -> str:
    """Cancel by reminder name (preferred) or job id; also remove its script."""
    ref = (ref or "").strip()
    if not ref:
        return "Error: no reminder name/id given."
    hermes = _hermes_bin()
    try:
        cp = _run([hermes, "cron", "remove", ref])
    except Exception as e:
        return f"Error cancelling reminder: {e}"
    msg = (cp.stdout or cp.stderr or "").strip()
    # Best-effort: if a reminder name was given, drop its generated script too.
    # name  reminder-<slug>-<token>  ↔  script reminder_<slug>_<token>.py
    if ref.startswith("reminder-"):
        rest = ref[len("reminder-"):].replace("-", "_")
        try:
            (SCRIPTS_DIR / f"reminder_{rest}.py").unlink(missing_ok=True)
        except Exception:
            pass
    if cp.returncode != 0:
        return f"Error: {msg or 'could not cancel'}"
    return f"🗑️ {msg or 'Reminder cancelled.'}"

# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Hermes reminder engine (cron-backed)")
    ap.add_argument("text", nargs="?", help="reminder text")
    ap.add_argument("schedule", nargs="?", help="schedule (e.g. '2h', '0 9 * * *')")
    ap.add_argument("--recurring", action="store_true", help="repeat on schedule")
    ap.add_argument("--list", action="store_true", help="list scheduled jobs")
    ap.add_argument("--cancel", metavar="NAME", help="cancel a reminder by name/id")
    args = ap.parse_args()

    if args.list:
        print(list_reminders()); return 0
    if args.cancel:
        print(cancel_reminder(args.cancel)); return 0
    if not args.text or not args.schedule:
        ap.error("need TEXT and SCHEDULE (or --list / --cancel NAME)")
    print(make_reminder(args.text, args.schedule, recurring=args.recurring))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
