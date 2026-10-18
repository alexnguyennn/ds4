#!/usr/bin/env python3
"""Inventory a ds4 KV disk-cache directory from its fixed headers.

Reads only the 52-byte header of every <sha>.kv file, so it is cheap on a
multi-hundred-GiB directory. Answers the questions the server log cannot:

  * how much of the budget is state the CURRENT model can never load
    (foreign model_id, or ctx_size larger than the running --ctx)
  * which files actually earn their keep (hits, and hits by save reason)
  * how many generations of the same conversation are accumulating
  * what one parked conversation costs, in bytes

    ./tools/ds4-kv-inventory.py [dir] [--model N] [--ctx 262144] [--min-tokens 8192]

model_id is read from the files; pass --model of the running server to get a
loadable/unloadable split (the server logs its own model at startup, or omit
--model and inspect the per-geometry groups).
"""
import argparse
import glob
import os
import struct
import time
from collections import Counter, defaultdict

REASON = {0: "unknown", 1: "cold", 2: "continued", 3: "evict", 4: "shutdown",
          5: "agent-system", 6: "agent-session"}
# Anchor reasons are the ones the eviction scorer protects (x2.0).
ANCHORS = {"cold", "evict", "shutdown"}


def read_header(path):
    with open(path, "rb") as fh:
        h = fh.read(52)
    if len(h) < 52:
        return None
    quant, reason, ext, model = struct.unpack_from("4B", h, 4)
    tokens, hits, ctx = struct.unpack_from("<3I", h, 8)
    created, used = struct.unpack_from("<2Q", h, 24)
    payload = struct.unpack_from("<Q", h, 40)[0]
    text_len = struct.unpack_from("<I", h, 48)[0]
    st = os.stat(path)
    return dict(path=path, name=os.path.basename(path), quant=quant,
                reason=REASON.get(reason, str(reason)), model=model,
                tokens=tokens, hits=hits, ctx=ctx, created=created, used=used,
                payload=payload, text_len=text_len, size=st.st_size)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir", nargs="?", default=os.path.expanduser("~/.ds4/server-kv"))
    ap.add_argument("--model", type=int, help="model_id of the running server")
    ap.add_argument("--ctx", type=int, default=262144)
    ap.add_argument("--min-tokens", type=int, default=512)
    ap.add_argument("--list", action="store_true", help="list every file")
    ap.add_argument("--print-unloadable", action="store_true",
                    help="print absolute paths of files this server can never load "
                         "(foreign model_id, ctx too large, or below --min-tokens); "
                         "pipe to xargs rm after stopping the server")
    a = ap.parse_args()

    files = [e for e in (read_header(p) for p in glob.glob(os.path.join(a.dir, "*.kv"))) if e]
    if not files:
        raise SystemExit(f"no .kv files in {a.dir}")
    now = time.time()
    gib = 2 ** 30

    total = sum(f["size"] for f in files)
    print(f"{len(files)} files, {total / gib:.1f} GiB in {a.dir}\n")

    print("per-token geometry (KiB/token clusters = distinct model layouts)")
    geo = defaultdict(lambda: [0, 0])
    for f in files:
        k = (f["model"], round(f["size"] / f["tokens"] / 1024, 1))
        geo[k][0] += 1
        geo[k][1] += f["size"]
    for (m, kib), (n, sz) in sorted(geo.items(), key=lambda x: -x[1][1]):
        loadable = ""
        if a.model is not None:
            loadable = "  <- current model" if m == a.model else "  <- NEVER loadable"
        print(f"  model_id {m}  {kib:5.1f} KiB/tok  {n:3d} files  {sz / gib:6.1f} GiB{loadable}")

    if a.model is not None:
        ok = [f for f in files if f["model"] == a.model and f["ctx"] <= a.ctx
              and f["tokens"] >= a.min_tokens]
        bad = [f for f in files if f not in ok]
        print(f"\nloadable by this server: {len(ok)} files, {sum(f['size'] for f in ok) / gib:.1f} GiB")
        print(f"dead weight           : {len(bad)} files, {sum(f['size'] for f in bad) / gib:.1f} GiB")
        if bad:
            print("  (the eviction scorer does not know about model mismatch: score is\n"
                  "   (hits+1) * tokens/bytes, so a smaller-per-token foreign layout\n"
                  "   scores higher and is evicted LAST. Delete these to reclaim budget.)")

    print("\nhits by save reason (anchors = cold/evict/shutdown, protected x2)")
    agg = defaultdict(lambda: [0, 0, 0])
    for f in files:
        agg[f["reason"]][0] += 1
        agg[f["reason"]][1] += f["hits"]
        agg[f["reason"]][2] += f["size"]
    for r, (n, hits, sz) in sorted(agg.items(), key=lambda x: -x[1][1]):
        print(f"  {r:<14} {n:3d} files  {sz / gib:6.1f} GiB  hits={hits}"
              f"  {'anchor' if r in ANCHORS else 'ladder'}")

    print("\nconversation generations (same model, tokens within 2% -> one thread's dumps)")
    by_model = defaultdict(list)
    for f in files:
        by_model[f["model"]].append(f)
    for m, fs in by_model.items():
        fs.sort(key=lambda f: f["created"])
        cluster, prev = [], None
        clusters = []
        for f in fs:
            if prev and abs(f["tokens"] - prev["tokens"]) <= 0.02 * max(prev["tokens"], 1) \
                    and (f["created"] - prev["created"]) < 86400:
                cluster.append(f)
            else:
                if len(cluster) > 2:
                    clusters.append(cluster)
                cluster = [f]
            prev = f
        if len(cluster) > 2:
            clusters.append(cluster)
        for c in clusters:
            print(f"  model {m}: {len(c)} dumps {c[0]['tokens']} -> {c[-1]['tokens']} tokens, "
                  f"{sum(x['size'] for x in c) / gib:.1f} GiB retained "
                  f"(only the longest can serve a returning client)")

    if a.print_unloadable:
        if a.model is None:
            raise SystemExit("--print-unloadable needs --model of the running server")
        for f in files:
            if not (f["model"] == a.model and f["ctx"] <= a.ctx
                    and f["tokens"] >= a.min_tokens):
                print(f"{f['path']}\0", end="")
        return

    if a.list:
        print("\nall files")
        for f in sorted(files, key=lambda f: -f["size"]):
            print(f"  {f['tokens']:7d} tok hits={f['hits']:<3d} {f['reason']:<10} "
                  f"model={f['model']} ctx={f['ctx']} {f['size'] / gib:6.2f} GiB "
                  f"text={f['text_len']:8d} idle={(now - f['used']) / 3600:8.1f} h  {f['name'][:16]}")


if __name__ == "__main__":
    main()
