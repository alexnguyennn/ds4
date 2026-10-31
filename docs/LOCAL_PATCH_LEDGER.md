# Local Patch Ledger — KV cache / session concurrency

Branch: `feature/local-mods`. Backup ref before this series:
`backup/pre-patch-series-20261031` (`5e41068`).

**Policy.** We carry the minimum set of patches that makes the models we actually
run work properly, one commit per upstream PR, so each is independently
revertable. Every patch carries `Upstream-PR:` / `Carried-because:` /
`Eject-when:` trailers — `git log --grep='Upstream-PR:'` is the machine-readable
ledger. This file is the human index and the ejection recipe. Nothing here is a
fork-local feature; all of it is waiting on upstream.

This serves the project's own stated goal (`AGENT.md`): *"Make long local agent
sessions practical through live KV reuse and disk KV checkpoints."*

## Workload this is tuned for

One M-series box, 128 GiB RAM, `Qwen3.8-Flash-Next-Q4` + vision sidecar, Metal,
`--batched-session 2`, `--ctx 262144`. Several long-lived interactive
conversations (100k–250k tokens) that get interrupted and parked, then swapped
back in. Interrupts and parked-conversation swap-ins are the workload, not
batch throughput.

## Carried set (7 commits, all from open upstream PRs)

| commit | PR | fixes | why we need it | eject when |
|---|---|---|---|---|
| `39bb4f1` | [#1221] | #1214 | memory-text reuse tier cannot byte-compare the rendered `vision_start…vision_end` expansion, so every vision turn after the first full-prefills | #1221 merged, or equivalent fix for #1214 |
| `86ebd28` | [#1212] | #1211 | eviction saves a reasoning-free `thinking-visible` disk key while our client replays `reasoning_content`; the returning conversation can never match it | #1212 merged |
| `391d83d` | [#1258] | — | drops store attempts against a budget measured at 100% full | #1258 merged |
| `906751b` | [#1228] | — | keeps a bounded cold prefix; the cold anchor is the only entry in our cache dir that ever records hits | #1228 merged |
| `988fdaf` | [#1281] | — | a short live prefix was hiding a much longer disk checkpoint | #1281 merged |
| `f86e2f2` | [#1280] | — | continued checkpoints only ever landed at an exact multiple of the interval, so a long read had no usable ladder | #1280 merged |
| `77136f8` | [#1283] | — | **speculative — eject first.** V4.1-only gate widening, carried as the template for our own Qwen widening | revert if measurement shows no Qwen effect; or #1283 merged; or superseded by our own widening |

Also in this series: `97af164` merges `upstream/main` (13 commits), which brings
`a8ce74a server: render blank reasoning as absent` — itself a fix for full
re-prefill after reasoning-free turns. That merge is not a carried patch; it is
just catching up.

## Evidence that motivated it

Measured with `tools/ds4-kv-telemetry.py` over tmux scrollback (de-wrapped —
see that tool's docstring for why raw greps of a capture undercount).

| window | misses | **near-perfect misses** | churn evictions | continued rungs | decode p50 |
|---|---|---|---|---|---|
| Oct 30 12:02–23:40 (64 GiB budget) | 28 | **16** (1.80M tokens) | 13 | `[131072]` | 39.8 t/s |
| Oct 31 19:01–20:42 (128 GiB budget) | 3 | 1 | **0** | `[]` | 31.8 t/s |

The second row is not the patch series: it is the `--kv-disk-space-mb 131072`
retune, already running in production. **It removed the churn evictions outright.**
The near-perfect miss that survived is a code-path failure, not a budget failure,
and is what the carried patches are for. Full 8-hour day: 29 full rebuilds and
3.65M tokens re-prefilled.

The two failure signatures, straight from the log:

```
14:36:07  live kv cache miss live=193921 prompt=193563 common=193563 vision=match reason=token-mismatch
14:36:07  slot 0: request reuses nothing; evicting checkpoint (193921 tokens, idle 8943 s, stale)
14:36:10  kv cache stored tokens=193921 reason=evict key=token-text size=6426.35 MiB save=1472.0 ms
14:36:10  chat ctx=0..193563:193563 TOOLS prompt start          ← 3.5 min to recover a 358-token interrupt
12:24:57  slot 1: request reuses nothing; evicting checkpoint (55694 tokens, idle 38 s, protected)
12:24:57  live kv cache miss live=55694 prompt=55799 common=49134 vision=match reason=token-mismatch
12:24:57  chat ctx=0..55799:55799 TOOLS prompt start            ← 88% of the live board thrown away
```

1. **The frontier is written 358 tokens too long to be loadable.**
   `ds4_kvstore_find_text_prefix()` requires `e->text_bytes <= prompt_bytes`. The
   `evict` snapshot is the prompt *plus the generated tail*, so it is always
   longer than the prompt the interrupted client replays next. Measured: 8 frontier
   files, 199k–246k tokens, 57 GiB, **every one `hits=0`**, both key kinds. The same
   argument is upstream issue #1266, which has **no fix PR**.
2. **Truncating rewind is gated off for Qwen.**
   `live_prefix_rewind_target(ds4_engine_is_glm_dsa(s->engine), …)` — with #1283 it
   becomes `ds41_rewind || ds4_engine_is_glm_dsa(...)`, i.e. V4.1 joins GLM and Qwen
   still does not. `ds4_session_rewind()` itself is generic (it runs on Metal/Qwen
   every turn for speculative decoding), so the gate is conservatism, not a limit.
3. **The disk budget was pinned at 100% full**, which turns every write into an
   eviction: 13 `reason=disk-cache-full` in one day, mostly `hits=0` victims.
4. **Continued checkpoints almost never fired.** `continued_store_target()` demands
   `live_tokens % step == 0`; with the old `--kv-cache-continued-interval-tokens
   131072` the only firing point in a 262k context was 131072 (4 saves, all
   exactly 131072). #1280 replaces that with a real ladder.

Full write-up (local-only, `misc/` is gitignored):
`misc/reports/2026-10-18-ds4-server-thrash-analysis.md`. Baseline telemetry and
the restart runbook: `misc/reports/baseline/` and
`misc/reports/2026-10-31-restart-runbook.md`. Inventory probe:
`tools/ds4-kv-inventory.py`. Remediation probe: `tools/ds4-kv-telemetry.py`.

## Runtime contract the patches assume

```
--metal --mtp --ctx 262144 --batched-session 2
--prefill-chunk 2048 --mixed-prefill-quantum 1024
--kv-disk-dir ~/.ds4/server-kv-qwen --kv-disk-space-mb 131072
--kv-cache-min-tokens 8192 --kv-cache-cold-max-tokens 32768
--kv-cache-continued-interval-tokens 32768     # required by #1280 (KV_CACHE_SEGMENT_MIN_STEP 16384)
--kv-cache-boundary-align-tokens 2048 --kv-cache-boundary-trim-tokens 32
```

- `--kv-cache-continued-interval-tokens 32768` is **part of the patch**, not a
  preference: below 16384 #1280 is inert. Do not lower it further to chase finer
  granularity — each rung costs one session sync.
- `--kv-disk-space-mb 131072` is not a patch. A budget that is always full converts
  every store into an eviction, which is what made the loop self-sustaining.
- `--batched-session 3` costs ~8.3 GiB, not 19.6 GiB (the prefill workspace is
  shared: startup reports 39.15 GiB for two sessions, or 27.91 GiB shared). It
  competes with the page cache streaming a 166 GB model — A/B it and watch decode
  t/s.

## Ejection recipe

1. `git fetch upstream && git log --oneline HEAD..upstream/main`
2. Check each PR's state:
   `gh pr view <N> --repo antirez/ds4 --json state,mergedAt,title`
3. Merged upstream → `git revert <sha>`. If the revert conflicts, the upstream
   version wins — take upstream's file and drop our commit entirely.
4. **Also check for equivalent fixes that did not arrive under these PR numbers.**
   Re-run the probes after any upstream bump and compare against this baseline:
   - `python3 tools/ds4-kv-inventory.py` — frontier files with `hits=0` and
     `reason=evict` are the signature of finding 1.
   - server log: `grep -c 'reason=token-mismatch'` over a busy day, and
     `grep -c 'reason=disk-cache-full'`.
   - a turn where `common` is within ~1% of `live` but the next line is
     `ctx=0..N` — that is finding 2 still open.
   If those numbers are at zero on stock upstream, the equivalent fix landed and
   the commit goes, even if the PR was closed unmerged.
5. Target end state: **zero carried commits.** Realistically the last one to go is
   the Qwen rewind widening, which may have to be ours.

## Deliberately not carried (yet)

- **Our own Qwen rewind widening** (drop or widen the `ds4_engine_is_glm_dsa()`
  gate at the `live_prefix_rewind_target()` call). Still open — P1 reproduces it on
  demand — and no upstream PR does it for Qwen; #1283 is the template (its `align`
  argument and side-state handling). **Priority downgraded by measurement:** the
  disk fallbacks we now carry bound the damage at ~2 s instead of ~36 s, so this is
  no longer the emergency it looked like on 2026-10-30. Revisit if a truncation
  lands on a conversation long enough that the cold anchor misses it by a lot —
  the cold anchor only covers prompts that are cold *first* prompts, so a
  multi-turn regeneration is the exposure to watch.
- **Issue #1266** (the pre-lookup `evict` save runs the eviction pass and deletes
  the cold prefix the very next line would have loaded). Open, zero comments, no
  PR. If measurement still shows `cached=0` immediately after an `evict` store, we
  write this one ourselves: either store after the lookup, or protect entries the
  in-flight request could load.
- Multimodal disk-cache path: #1281 documents that multimodal requests **never**
  take the disk path ("payloads carry no image identity"). No carried patch changes
  that; vision conversations still cannot swap in from disk.

## Measured outcome (in-anger, first restart: 2026-10-31 22:07)

Like-for-like evening window, same workload shape, old binary vs new:

| | OLD Oct 30 22:00–23:59 | NEW Oct 31 22:07–23:59 |
|---|---|---|
| turns | 61 | 363 |
| token hit rate | 89.1% | **99.3%** |
| tokens re-prefilled | 921,168 | 277,006 |
| **per turn** | 15,101 | **763** (−95%) |
| rebuilds >10k tokens | 4 | **0** |
| near-perfect misses | 4 | 1 |
| decode p50 | 27.3 t/s | 30.1 t/s (+10%) |
| `evict` disk keys written | 3 thinking-visible, 1 token-text | **4 / 4 token-text** |

Per-patch evidence, all observed in production:

- **#1212** — every `reason=evict` store now picks `key=token-text`; the old
  binary wrote `thinking-visible` for the same turns. `hits` on `evict` files went
  0 → 3. Causal chain: 22:25:38 theft dumps 158,319 tokens as `token-text`; at
  23:54:48 the conversation returns with prompt 158,367 and resumes
  `ctx=158319..158367` — **48 tokens re-prefilled instead of 158,367.**
- **#1280** — `continued checkpoint at 131072 of 142711 (segment 48.8s)` and
  `continued checkpoint at prompt end 175414`. The **prompt-end store is the
  important one**: it is keyed at the prompt boundary, so unlike the `evict`
  snapshot it *is* loadable (finding 1 is sidestepped, not fixed).
- **#1221** — 85 `multimodal live kv hit` lines, 0 `vision=mismatch` misses.
- **#1281 / #1258** — no `reason=disk-cache-full` churn at all.
- **P1 probe** (`tools/probe-p1-truncation.py`) — the live rewind is still
  refused for Qwen: `live=32029 prompt=31997 common=31997 reason=token-mismatch`
  → `request reuses nothing`. But the cost is no longer a rebuild, because #1228's
  bounded cold prefix caught it at 30,720: `ctx=30720..31997:1277`, **2.0 s vs
  36.3 s** for the same prompt cold. 18× cheaper, still not the ~1 token a rewind
  would cost.

**Consequence for the interval question:** the `continued ... prompt end` store
plus loadable `evict` snapshots mean a *smaller*
`--kv-cache-continued-interval-tokens` is much less valuable than this ledger
first claimed. Observed value over ~2 h at 131072: one case, ~13 s. Keep 131072
until `tools/ds4-kv-inventory.py` shows `hits>0` on `continued` files (it shows
`hits=0` today, 4 files / 17.9 GiB).

## Verification status

- `make -j14`: clean, **zero errors, zero warnings**.
- `make test`: passes, except `ds4_agent_test` which fails on `strstr(obs, cwd)`
  **only when the tree is under `/tmp`** (macOS symlink → `/private/tmp`). Run from
  a real path and it passes. Not a regression.
- `tests/test_qwen_tool_disk_key` (#1212's regression): **PASS, 0 failures.** The
  counterfactual does not even compile against pre-patch code — the test calls
  `remember_qwen_tool_turn_visible_checkpoint()` with the `visible_continuation`
  argument #1212 introduced — so the patch is load-bearing by construction.
- `./ds4_test` **not run**: it includes `ds4_server.c` with `DS4_SERVER_TEST`, so it
  carries the reuse-tier and `test_live_prefix_rewind_target` assertions this series
  edits, but it refuses to start while the server holds the instance lock.
- `tests/test_metal_rewind{,_vision}` (#1283): built, **not run** — they load a real
  model (`DS4_TEST_MODEL`) and `AGENT.md` forbids concurrent huge-model processes.
- Both of the above belong in the restart window.

[#1212]: https://github.com/antirez/ds4/pull/1212
[#1214]: https://github.com/antirez/ds4/issues/1214
[#1221]: https://github.com/antirez/ds4/pull/1221
[#1228]: https://github.com/antirez/ds4/pull/1228
[#1258]: https://github.com/antirez/ds4/pull/1258
[#1280]: https://github.com/antirez/ds4/pull/1280
[#1281]: https://github.com/antirez/ds4/pull/1281
[#1283]: https://github.com/antirez/ds4/pull/1283
[#1266]: https://github.com/antirez/ds4/issues/1266
