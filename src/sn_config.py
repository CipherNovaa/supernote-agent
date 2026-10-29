#!/usr/bin/env python3
"""
sn_config.py — the single source of truth for paths, models and credentials.

Every script in this repo imports this module instead of hardcoding vault
folders, model names or Discord IDs. Settings come from three layers, each
overriding the one before:

  1. DEFAULTS below (always work, so a missing config can't brick a script)
  2. a TOML file: $SUPERNOTE_AGENT_CONFIG, else ~/.config/supernote-agent/config.toml
     (see config.example.toml)
  3. environment variables for secrets and a few overrides
     (see .env.example; a .env file next to the config is also read)

CLI:
    python sn_config.py              # selftest: print every resolved path + exists?
    python sn_config.py get inbox    # print one absolute path
    python sn_config.py pdf-areas    # "source<TAB>out" lines
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import tomllib                      # Python 3.11+
except ModuleNotFoundError:             # pragma: no cover
    tomllib = None

CONFIG_DIR  = Path(os.environ.get("SUPERNOTE_AGENT_CONFIG_DIR",
                                  str(Path.home() / ".config" / "supernote-agent")))
CONFIG_FILE = Path(os.environ.get("SUPERNOTE_AGENT_CONFIG", str(CONFIG_DIR / "config.toml")))
ENV_FILE    = Path(os.environ.get("SUPERNOTE_AGENT_ENV", str(CONFIG_DIR / ".env")))

# ── DEFAULTS (mirror config.example.toml) ────────────────────────────────────
DEFAULTS: dict = {
    "vault": {"root": "~/Documents/Obsidian Vault"},
    # Vault-relative folders. Rename a folder here, once, and every script follows.
    "dirs": {
        "inbox": "Inbox",                         # where new notes land
        "academics": "Knowledge/Academics",       # few-shot example source
        "systems": "Knowledge/Systems",
        "projects": "Projects",
        "source_material": "Knowledge/Source Material",
        "tags": "Tags",                           # one empty .md per tag (tag vocabulary)
        "images": "Images",                       # rendered page PNGs go in Images/Supernote
        "supernote_raw": "Supernote/Raw",         # where Syncthing drops .note files
        "pdf_output": "Supernote/Vault PDFs",     # PDFs mirrored back to the device
    },
    # Few-shot examples for the formatter. Absolute or ~ paths are used as-is;
    # a relative path is resolved against the vault. Empty = the academics dir.
    "examples_dir": "",
    # Which vault folders are exported to A5 PDFs for the device, and the flat
    # output subfolder each one gets.
    "pdf_areas": [
        {"source": "Knowledge/Academics", "out": "Academics"},
        {"source": "Knowledge/Systems", "out": "Systems"},
        {"source": "Projects", "out": "Projects"},
    ],
    "exclude_globs": ["**/_Archive/**", "**/Archive/**", "**/.trash/**"],
    # Auto-filing: first route whose keywords intersect a note's tags wins.
    # Paths are vault-relative. No match -> the inbox.
    "tag_routes": [
        {"folder": "Knowledge/Academics/Mathematics",
         "tags": ["math", "arithmetic", "calculus", "physics", "science"]},
        {"folder": "Knowledge/Academics/Programming",
         "tags": ["c++", "python", "programming", "code"]},
        {"folder": "Knowledge/Systems",
         "tags": ["linux", "software", "hardware", "obsidian"]},
        {"folder": "Projects", "tags": ["supernote", "automation", "ai", "project"]},
    ],
    # Agent state: style memory, Discord pending-thread metadata, fail counters,
    # the OCR-correction glossary, caches. Kept out of the vault.
    "state": {
        "dir": "~/.local/state/supernote-agent",
        "style_file": "",          # default: <state.dir>/style.md
        "memory_file": "",         # default: <state.dir>/memories.md
    },
    "ollama": {
        "url": "http://localhost:11434",
        "text_model": "qwen3:8b",
        "vision_model": "qwen2.5vl:7b",
        "cover_model": "llava:7b",
        "genre_model": "qwen3:8b",
    },
    "discord": {
        # Optional: send notifications through the Hermes Agent CLI
        # (https://github.com/NousResearch/hermes-agent) instead of the bot REST API.
        "hermes_bin": "",
    },
    "scan": {
        "inbox": "~/scans/inbox",
        "done": "~/scans/done",
    },
    "health": {
        "services": ["supernote-watcher", "supernote-feedback"],
        "syncthing_config": "~/.local/state/syncthing/config.xml",
        "backup_repo": "",         # a git repo whose last commit age is reported
    },
    "web": {
        "searx_url": "http://localhost:8888/search",   # optional local SearXNG
    },
}


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def _load() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    if tomllib is not None and CONFIG_FILE.exists():
        try:
            _merge(cfg, tomllib.loads(CONFIG_FILE.read_text()))
        except Exception as e:              # never let bad config brick a caller
            print(f"[sn_config] WARNING: ignoring bad {CONFIG_FILE}: {e}", file=sys.stderr)
    return cfg


def _load_env() -> dict:
    env: dict = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items()})
    return env


_CFG = _load()
_ENV = _load_env()


def _p(s: str) -> Path:
    return Path(os.path.expandvars(s)).expanduser()


# ── Public API: paths ────────────────────────────────────────────────────────
def config() -> dict:
    return _CFG


def env(key: str, default: str = "") -> str:
    return _ENV.get(key, default)


def layout() -> str:
    return str(CONFIG_FILE) if CONFIG_FILE.exists() else "defaults"


def vault_root() -> Path:
    return _p(env("SUPERNOTE_VAULT") or _CFG["vault"]["root"])


def rel(key: str) -> str:
    """Vault-relative path string for a logical dir key."""
    r = _CFG["dirs"].get(key)
    if r is None:
        raise KeyError(f"[sn_config] unknown dir key {key!r}; known: {sorted(_CFG['dirs'])}")
    return r.strip("/")


def d(key: str) -> Path:
    return vault_root() / rel(key)


def inbox() -> Path: return d("inbox")
def tags() -> Path: return d("tags")
def images() -> Path: return d("images")
def supernote_raw() -> Path: return d("supernote_raw")
def pdf_output() -> Path: return d("pdf_output")


def examples_dir() -> Path:
    e = (_CFG.get("examples_dir") or "").strip()
    if not e:
        return d("academics")
    p = _p(e)
    return p if p.is_absolute() else vault_root() / e


def pdf_areas() -> list[tuple[str, str]]:
    return [(a["source"], a["out"]) for a in _CFG.get("pdf_areas", [])]


def exclude_globs() -> list[str]:
    return list(_CFG.get("exclude_globs", []))


def tag_routes() -> list[tuple[str, set[str]]]:
    return [(r["folder"], {t.lower() for t in r.get("tags", [])})
            for r in _CFG.get("tag_routes", [])]


def state_dir() -> Path:
    p = _p(_CFG["state"]["dir"])
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_path(name: str) -> Path:
    return state_dir() / name


def style_file() -> Path:
    s = _CFG["state"].get("style_file") or ""
    return _p(s) if s else state_path("style.md")


def memory_file() -> Path:
    s = _CFG["state"].get("memory_file") or ""
    return _p(s) if s else state_path("memories.md")


def scan_inbox() -> Path: return _p(_CFG["scan"]["inbox"])
def scan_done() -> Path: return _p(_CFG["scan"]["done"])


def health() -> dict:
    return _CFG["health"]


def searx_url() -> str:
    return env("SEARX_URL") or _CFG["web"]["searx_url"]


# ── Public API: models ───────────────────────────────────────────────────────
OLLAMA_URL      = (env("OLLAMA_URL") or _CFG["ollama"]["url"]).rstrip("/")
OLLAMA_GENERATE = f"{OLLAMA_URL}/api/generate"
TEXT_MODEL      = env("SUPERNOTE_TEXT_MODEL") or _CFG["ollama"]["text_model"]
VISION_MODEL    = env("SUPERNOTE_VISION_MODEL") or _CFG["ollama"]["vision_model"]
COVER_MODEL     = _CFG["ollama"]["cover_model"]
GENRE_MODEL     = _CFG["ollama"]["genre_model"]


# ── Public API: Discord ──────────────────────────────────────────────────────
DISCORD_API = "https://discord.com/api/v10"


def discord_creds() -> dict:
    """Bot token, home channel and the user IDs allowed to steer notes — all from
    the environment / .env file, never from code."""
    return {
        "token":   env("DISCORD_BOT_TOKEN"),
        "channel": env("DISCORD_HOME_CHANNEL"),
        "allowed": {u.strip() for u in env("DISCORD_ALLOWED_USERS").split(",") if u.strip()},
    }


def discord_headers() -> dict:
    return {"Authorization": f"Bot {discord_creds()['token']}",
            "User-Agent": "supernote-agent/1.0",
            "Content-Type": "application/json"}


def hermes_bin() -> str | None:
    """Absolute path to the optional Hermes Agent CLI. Under systemd a user
    service runs with a minimal PATH that omits ~/.local/bin, so a bare
    "hermes" fails with ENOENT and every notification silently dies — resolve
    it to an absolute path once."""
    explicit = env("HERMES_BIN") or _CFG["discord"].get("hermes_bin") or ""
    if explicit:
        p = _p(explicit)
        return str(p) if p.exists() else None
    for cand in (Path.home() / ".local" / "bin" / "hermes",):
        if cand.exists():
            return str(cand)
    return shutil.which("hermes")


def send_discord(msg: str) -> str | None:
    """Post `msg` to the home channel and return the message id (the id a
    reply will reference), or None. Uses the Hermes CLI when available,
    otherwise the Discord bot REST API directly."""
    hb = hermes_bin()
    if hb:
        try:
            r = subprocess.run([hb, "send", "--to", "discord", "--json", msg],
                               timeout=15, check=True, capture_output=True, text=True)
            return json.loads(r.stdout).get("message_id")
        except Exception as e:
            print(f"[Discord] hermes send failed ({e}); trying REST")
    creds = discord_creds()
    if not (creds["token"] and creds["channel"]):
        print("[Discord] no DISCORD_BOT_TOKEN / DISCORD_HOME_CHANNEL — not sent")
        return None
    try:
        import requests
        r = requests.post(f"{DISCORD_API}/channels/{creds['channel']}/messages",
                          headers=discord_headers(),
                          json={"content": msg[:1990], "allowed_mentions": {"parse": []}},
                          timeout=15)
        r.raise_for_status()
        return str(r.json().get("id"))
    except Exception as e:
        print(f"[Discord] {e}")
        return None


# ── CLI / selftest ───────────────────────────────────────────────────────────
def _selftest() -> int:
    vr = vault_root()
    print(f"config : {CONFIG_FILE}  exists={CONFIG_FILE.exists()}")
    print(f"env    : {ENV_FILE}  exists={ENV_FILE.exists()}")
    print(f"vault  : {vr}  exists={vr.exists()}")
    for k in _CFG["dirs"]:
        p = d(k)
        print(f"  {k:16} {'ok ' if p.exists() else 'MISS'}  {p}")
    print(f"examples : {examples_dir()}")
    print(f"state    : {state_dir()}")
    print(f"style    : {style_file()}")
    print(f"ollama   : {OLLAMA_URL}  text={TEXT_MODEL}  vision={VISION_MODEL}")
    c = discord_creds()
    print(f"discord  : token={'set' if c['token'] else 'MISSING'}  "
          f"channel={'set' if c['channel'] else 'MISSING'}  allowed={len(c['allowed'])}  "
          f"hermes={hermes_bin() or 'no'}")
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        return _selftest()
    if argv[0] == "get" and len(argv) > 1:
        print(d(argv[1]))
        return 0
    if argv[0] == "pdf-areas":
        for src, out in pdf_areas():
            print(f"{src}\t{out}")
        return 0
    if argv[0] == "vault":
        print(vault_root())
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
