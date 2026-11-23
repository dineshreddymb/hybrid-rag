"""Bring .env up to date with .env.example without touching values you already set.

 - creates .env from .env.example if it does not exist
 - appends keys that .env.example has and .env lacks (with the example's default)
 - generates REDIS_PASSWORD if it is empty

Run:  python scripts/sync_env.py        (tasks.ps1 setup does this)
"""
from __future__ import annotations

import re
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / ".env.example"
ENV = ROOT / ".env"
KEY = re.compile(r"^([A-Z0-9_]+)=(.*)$", re.M)


def main() -> None:
    example = EXAMPLE.read_text(encoding="utf-8")
    if not ENV.exists():
        ENV.write_text(example, encoding="utf-8")
        print("created .env from .env.example")
    env = ENV.read_text(encoding="utf-8")
    have = dict(KEY.findall(env))

    # keys added to .env.example since your .env was made
    missing_lines = []
    for line in example.splitlines():
        m = KEY.match(line)
        if m and m.group(1) not in have:
            missing_lines.append(line)
            print(f"added {m.group(1)}")
    if missing_lines:
        env = env.rstrip("\n") + "\n\n# --- added by scripts/sync_env.py ---\n" + "\n".join(missing_lines) + "\n"

    if not dict(KEY.findall(env)).get("REDIS_PASSWORD", "").strip():
        env = re.sub(r"^REDIS_PASSWORD=.*$", f"REDIS_PASSWORD={secrets.token_hex(16)}", env,
                     flags=re.M)
        print("generated REDIS_PASSWORD")
    ENV.write_text(env, encoding="utf-8")
    print(".env is up to date")


if __name__ == "__main__":
    main()
