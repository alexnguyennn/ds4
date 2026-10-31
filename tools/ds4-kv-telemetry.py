#!/usr/bin/env python3
"""Measure ds4-server KV cache remediation from a tmux scrollback capture.

Usage:
    tmux capture-pane -p -S - -t ds4:1.1 > before.log
    tools/ds4-kv-telemetry.py before.log --json before.json
    ... restart server, work normally for a while ...
    tmux capture-pane -p -S - -t ds4:1.1 > after.log
    tools/ds4-kv-telemetry.py after.log --json after.json
    tools/ds4-kv-telemetry.py --diff before.json after.json

Why this exists: `tmux capture-pane` re-emits the pane at its *current* width and
wraps mid-token ("reaso\nn=continued"), so grepping the raw capture undercounts
anything past column ~80. Every record is de-wrapped before it is matched.

What each counter means, and the direction we want:

  misses               `live kv cache miss` lines.                    down
  near_perfect         misses where common >= 95% of live.            -> 0
                       The slot held an almost-exact prefix of the prompt and
                       the reuse ladder still returned nothing. This is the
                       signature of the GLM-only rewind gate (ledger finding 2)
                       and of the vision byte-compare miss (#1214 / #1221).
  full_rebuilds        `ctx=0..N prompt start` lines.                 down
  repay_tokens         sum of (end-start) over finished turns.        down
  disk_cache_full      stores that triggered a budget eviction.       -> ~0
                       A budget that is always full converts every write into
                       an eviction of some other conversation's only copy.
  continued           aligned ladder checkpoints written.             up, and
                       spread over several token counts (not all 131072) --
                       that spread is what #1280 buys us.
  evict               frontier snapshots written on slot theft.       down
  multimodal_hits     vision turns that reused live KV.               up
"""
import argparse
import json
import re
import sys

RECORD = re.compile(r"(\d{4}) (\d\d:\d\d:\d\d) ds4-server: ")
MISS = re.compile(r"live kv cache miss live=(?P<live>\d+)\s*prompt=(?P<prompt>\d+)"
                  r"\s*common=(?P<common>\d+)\s*vision=(?P<vision>\S+)"
                  r"\s*reason=(?P<reason>\S+)")
CTXFIN = re.compile(r"ctx=(\d+)\.\.(\d+):\d+.*?finish=")
START = re.compile(r"ctx=(\d+)\.\.(\d+):\d+.*?prompt start")
STORE = re.compile(r"kv cache stored tokens=(?P<tokens>\d+) trimmed=(?P<trimmed>\d+)"
                   r"\s*reason=(?P<reason>[a-z-]+)(?: key=(?P<key>[a-z-]+))?")
DEC = re.compile(r"decoding chunk=([0-9.]+)")


def records(path):
    """Split a capture into server log records, undoing tmux line wrapping."""
    raw = open(path, errors="replace").read()
    parts = RECORD.split(raw)
    out = []
    for i in range(1, len(parts) - 2, 3):
        msg = "".join(parts[i + 2].split("\n"))
        out.append({"day": parts[i], "t": parts[i + 1], "m": msg})
    return out


def measure(path):
    recs = records(path)
    blob = "\n".join(r["m"] for r in recs)

    misses = [MISS.search(r["m"]) for r in recs]
    misses = [m.groupdict() for m in misses if m]
    near = [m for m in misses if int(m["live"]) >= 4096
            and int(m["common"]) >= 0.95 * int(m["live"])]

    repay = sum(int(b) - int(a) for a, b in CTXFIN.findall(blob))
    rebuilds = [s for s in START.findall(blob) if int(s[0]) == 0]
    stores = [s.groupdict() for s in
              (STORE.search(r["m"]) for r in recs) if s]
    dec = sorted(float(x) for x in DEC.findall(blob))

    cont = [int(s["tokens"]) for s in stores if s["reason"] == "continued"]
    return {
        "source": path,
        "window": f"{recs[0]['t']}..{recs[-1]['t']}" if recs else None,
        "records": len(recs),
        "misses": len(misses),
        "near_perfect": len(near),
        "near_perfect_tokens": sum(int(m["common"]) for m in near),
        "vision_mismatch_misses": sum(1 for m in misses
                                      if m["vision"] == "mismatch"),
        "full_rebuilds": len(rebuilds),
        "repay_tokens": repay,
        "disk_cache_full": blob.count("reason=disk-cache-full"),
        "evict": sum(1 for s in stores if s["reason"] == "evict"),
        "cold": sum(1 for s in stores if s["reason"] == "cold"),
        "continued": len(cont),
        "continued_rungs": sorted(set(cont)),
        "multimodal_hits": blob.count("multimodal live kv hit"),
        "decode_p10": round(dec[len(dec) // 10], 1) if len(dec) > 9 else None,
        "decode_p50": round(dec[len(dec) // 2], 1) if dec else None,
        "decode_p90": round(dec[9 * len(dec) // 10], 1) if len(dec) > 9 else None,
    }


KEYS = ["window", "records", "misses", "near_perfect", "near_perfect_tokens",
        "vision_mismatch_misses", "full_rebuilds", "repay_tokens",
        "disk_cache_full", "evict", "cold", "continued", "continued_rungs",
        "multimodal_hits", "decode_p10", "decode_p50", "decode_p90"]

# counter -> 1 if lower is better, -1 if higher is better, 0 = context only
DIR = {"misses": 1, "near_perfect": 1, "near_perfect_tokens": 1,
       "vision_mismatch_misses": 1, "full_rebuilds": 1, "repay_tokens": 1,
       "disk_cache_full": 1, "evict": 1, "continued": -1,
       "multimodal_hits": -1, "decode_p50": -1}


def diff(a, b):
    print(f"{'metric':22} {'before':>12} {'after':>12} {'delta':>12}  verdict")
    print("-" * 74)
    for k in KEYS:
        va, vb = a.get(k), b.get(k)
        if isinstance(va, list) or isinstance(vb, list):
            print(f"{k:22} {str(va):>12} {str(vb):>12} {'':>12}  ladder rungs")
            continue
        if va is None or vb is None:
            print(f"{k:22} {str(va):>12} {str(vb):>12} {'':>12}  (n/a)")
            continue
        d = vb - va
        verdict = "  context"
        if k in DIR and va:
            better = (d < 0) if DIR[k] == 1 else (d > 0)
            if d == 0:
                verdict = "  ="
            else:
                verdict = ("  BETTER" if better else "  WORSE")
                if abs(d) / abs(va) > 0.5:
                    verdict += " <<"
        elif k in DIR and not va:
            verdict = "  new" if d else "  ="
        print(f"{k:22} {va:>12} {vb:>12} {d:>+12}  {verdict}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log", nargs="?")
    ap.add_argument("--json", dest="json_out")
    ap.add_argument("--diff", nargs=2, metavar=("BEFORE", "AFTER"))
    a = ap.parse_args()
    if a.diff:
        diff(json.load(open(a.diff[0])), json.load(open(a.diff[1])))
        return
    if not a.log:
        ap.error("need a log file, or --diff BEFORE AFTER")
    m = measure(a.log)
    for k in KEYS:
        print(f"  {k:22} {m[k]}")
    if a.json_out:
        json.dump(m, open(a.json_out, "w"), indent=1)
        print(f"\nwrote {a.json_out}")


if __name__ == "__main__":
    sys.exit(main())
