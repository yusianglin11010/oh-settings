#!/usr/bin/env python3
"""Rewrite OpenHands settings.json mcp_config to point at the HTTP MCP sidecars.

Usage (run as root because ~/.openhands/settings.json is root-owned):
    sudo python3 set-mcp-servers.py [GITHUB_PAT] [settings_path]

If GITHUB_PAT is omitted, the PAT already stored in settings.json (in the old
stdio 'github' server's env, or an existing github shttp api_key) is reused.

Configures two streamable-http MCP servers (see docker-compose.yml):
  - github     -> needs a PAT; OpenHands sends it as `Authorization: Bearer <pat>`
  - playwright -> no auth

- Removes any stdio 'github' server (its binary doesn't exist inside the sandbox).
- Replaces shttp_servers with exactly these two (idempotent: safe to re-run).
- Leaves every other field in settings.json untouched.
"""
import json
import os
import pwd
import sys
from pathlib import Path

GITHUB_URL = "http://192.168.31.53:8082/mcp"
PLAYWRIGHT_URL = "http://192.168.31.53:8931/mcp"


def default_settings_path():
    """Resolve the invoking user's home even under sudo (where HOME becomes /root)."""
    user = os.environ.get("SUDO_USER")
    home = pwd.getpwnam(user).pw_dir if user else str(Path.home())
    return Path(home) / ".openhands" / "settings.json"


def find_existing_pat(mcp):
    """Reuse a PAT already present in settings.json so the user need not re-enter it."""
    for s in mcp.get("stdio_servers", []):
        if s.get("name") == "github":
            tok = (s.get("env") or {}).get("GITHUB_PERSONAL_ACCESS_TOKEN")
            if tok:
                return tok
    for s in mcp.get("shttp_servers", []):
        if s.get("url") == GITHUB_URL and s.get("api_key"):
            return s["api_key"]
    return None


args = list(sys.argv[1:])
pat = args[0] if args and args[0].startswith(("ghp_", "github_pat_")) else None
rest = [a for a in args if a != pat]
path = Path(rest[0]) if rest else default_settings_path()

data = json.loads(path.read_text())

mcp = data.get("mcp_config") or {}
mcp.setdefault("sse_servers", [])

if pat is None:
    pat = find_existing_pat(mcp)
    if pat is None:
        sys.exit("No PAT given and none found in settings.json. "
                 "Run: sudo python3 set-mcp-servers.py ghp_yourtoken")
    print("Reusing PAT already stored in settings.json")

# Drop the broken stdio github entry (and any stdio servers — none are valid here).
mcp["stdio_servers"] = [s for s in mcp.get("stdio_servers", []) if s.get("name") != "github"]
# Replace shttp servers with our two sidecars.
mcp["shttp_servers"] = [
    {"url": GITHUB_URL, "api_key": pat, "timeout": 60},
    {"url": PLAYWRIGHT_URL, "timeout": 60},
]
data["mcp_config"] = mcp

path.write_text(json.dumps(data, ensure_ascii=False))
print("OK: mcp_config updated")
print("  shttp_servers ->", GITHUB_URL, "(github, bearer auth)")
print("                ->", PLAYWRIGHT_URL, "(playwright, no auth)")
print("  stdio_servers ->", mcp["stdio_servers"])
