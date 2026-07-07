#!/usr/bin/env python3
"""Regenerate the EMBEDDED_PRICES table in tokenchecker.py from LiteLLM's
community-maintained pricing data. Run from the repo root:

    python3 scripts/update_prices.py

The embedded table is only the offline fallback (reports prefer a live fetch),
so it holds a curated subset: the model families the supported agents actually
emit. A monthly GitHub workflow runs this and opens a PR when prices drift.
"""

import datetime
import json
import re
import sys
import urllib.request

sys.path.insert(0, ".")
import tokenchecker  # noqa: E402

TARGET = "tokenchecker.py"
BEGIN = "# >>> embedded prices (auto-generated, run scripts/update_prices.py) >>>"
END = "# <<< embedded prices <<<"

# Model families worth embedding: what Claude Code, Codex CLI, Gemini CLI and
# Cursor put in their logs. Everything else is covered by the live fetch.
KEEP = re.compile(r"^(claude-|gpt-4|gpt-5|o[34](-|$)|gemini-[23]|codex-|composer)")
DROP = re.compile(r"(audio|realtime|embedding|tts|whisper|transcribe|moderation|"
                  r"image|-search-|instruct|chat-latest)")


def main():
    req = urllib.request.Request(tokenchecker.PRICES_URL,
                                 headers={"User-Agent": "tokenchecker-updater"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        prices = tokenchecker.parse_litellm_prices(json.loads(resp.read().decode()))

    subset = {name: p for name, p in prices.items()
              if KEEP.match(name) and not DROP.search(name)}
    if len(subset) < 20:
        raise SystemExit(f"suspiciously few embedded models ({len(subset)}); aborting")

    today = datetime.date.today().isoformat()
    lines = [BEGIN, f'EMBEDDED_PRICES_DATE = "{today}"', "EMBEDDED_PRICES = {"]
    for name in sorted(subset):
        p = subset[name]
        lines.append(
            f'    "{name}": {{"input": {p["input"]:g}, "cache_read": {p["cache_read"]:g},'
            f' "cache_write": {p["cache_write"]:g}, "output": {p["output"]:g}}},')
    lines.append("}")
    lines.append(END)

    with open(TARGET, encoding="utf-8") as fh:
        src = fh.read()
    pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.DOTALL)
    if not pattern.search(src):
        raise SystemExit(f"embedded-prices markers not found in {TARGET}")
    updated = pattern.sub("\n".join(lines), src)
    if updated == src:
        print("embedded prices already up to date")
        return
    with open(TARGET, "w", encoding="utf-8") as fh:
        fh.write(updated)
    print(f"embedded {len(subset)} models (as of {today}) into {TARGET}")


if __name__ == "__main__":
    main()
