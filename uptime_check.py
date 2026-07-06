#!/usr/bin/env python3
"""Token-free site uptime checker.

Runs every few minutes from repo-steward-uptime.timer. Reads `sites:` from
config.yaml, probes each URL, appends samples to uptime.jsonl, and tracks
up/down transitions in uptime_state.json. A site is declared DOWN after two
consecutive failed probes (one blip never alerts); transitions append an
incident to incidents.jsonl and a note to escalations.md for the steward and
the maintainer. No LLM involved — this costs nothing to run often.
"""
import json
import re
import time
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FAILS_TO_ALERT = 2
TIMEOUT = 15


def sites():
    out, cur, in_block = [], None, False
    for line in (ROOT / "config.yaml").read_text().splitlines():
        if re.match(r"^sites:", line):
            in_block = True
            continue
        if in_block:
            if line.strip() and not line.startswith((" ", "\t", "-")):
                break
            m = re.match(r"\s*-\s*url:\s*(\S+)", line)
            if m:
                cur = {"url": m.group(1)}
                out.append(cur)
                continue
            m = re.match(r"\s*repo:\s*(\S+)", line)
            if m and cur:
                cur["repo"] = m.group(1)
    return out


def probe(url):
    req = urllib.request.Request(url, headers={"User-Agent": "repo-steward-uptime/1.0"})
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            ms = int((time.monotonic() - start) * 1000)
            return resp.status < 400, resp.status, ms, None
    except urllib.error.HTTPError as e:
        return False, e.code, int((time.monotonic() - start) * 1000), None
    except Exception as e:
        return False, None, int((time.monotonic() - start) * 1000), type(e).__name__


def main():
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state_path = ROOT / "uptime_state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    samples, incidents = [], []

    for site in sites():
        url = site["url"]
        ok, status, ms, err = probe(url)
        samples.append({"ts": now, "url": url, "ok": ok, "status": status,
                        "ms": ms, "error": err})
        st = state.setdefault(url, {"ok": True, "since": now, "fails": 0,
                                    "repo": site.get("repo")})
        st["repo"] = site.get("repo")
        st["last_ms"] = ms
        st["last_status"] = status
        st["last_checked"] = now
        if ok:
            if not st["ok"]:
                incidents.append({"ts": now, "url": url, "event": "up",
                                  "down_since": st["since"]})
                st["ok"] = True
                st["since"] = now
            st["fails"] = 0
        else:
            st["fails"] += 1
            if st["ok"] and st["fails"] >= FAILS_TO_ALERT:
                st["ok"] = False
                st["since"] = now
                incidents.append({"ts": now, "url": url, "event": "down",
                                  "status": status, "error": err,
                                  "repo": site.get("repo")})

    with open(ROOT / "uptime.jsonl", "a") as f:
        for s in samples:
            f.write(json.dumps(s) + "\n")
    state_path.write_text(json.dumps(state, indent=2))

    if incidents:
        with open(ROOT / "incidents.jsonl", "a") as f:
            for i in incidents:
                f.write(json.dumps(i) + "\n")
        with open(ROOT / "escalations.md", "a") as f:
            for i in incidents:
                if i["event"] == "down":
                    f.write(f"\n## {now} · SITE DOWN · {i['url']}\n\n"
                            f"Two consecutive failed probes "
                            f"(status={i.get('status')}, error={i.get('error')}). "
                            f"Linked repo: {i.get('repo') or 'none'}. The next steward "
                            f"tick will investigate recent deploys/CI there.\n")
                else:
                    f.write(f"\n## {now} · site recovered · {i['url']} "
                            f"(down since {i['down_since']})\n")


if __name__ == "__main__":
    main()
