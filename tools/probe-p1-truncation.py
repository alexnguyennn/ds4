#!/usr/bin/env python3
"""P1: does a pure truncation of a live checkpoint reuse, or rebuild?

Send one long prompt with a small max_tokens, let it finish.  Send the *byte
identical* request again.  The second prompt is now a strict truncation of the
live session (prompt_len < live_pos, common == prompt_len): the exact shape the
REUSE_MEMORY_REWIND tier exists for, and the tier gated on
ds4_engine_is_glm_dsa()/ds4_engine_is_deepseek41().

Expected if the gate is still closed for this model: a full re-prefill, logged as
`ctx=0..N prompt start`.  Expected if it is open: a resume near N, or a rewind line.
"""
import json
import sys
import time
import urllib.request

URL = "http://127.0.0.1:8000/v1/chat/completions"
PARA = (
    "The whiteboard in the classroom is not a filing cabinet. Copying a textbook "
    "onto the board costs one line per token, and a photocopy of the board is only "
    "usable if it covers no more than what you are about to ask. A photocopy taken "
    "after the aborted turn is therefore always slightly too long, and the lookup "
    "rule skips it outright instead of matching it partially. "
)
BODY = {
    "model": "qwen3.8-flash-next",
    "max_tokens": 32,
    "temperature": 0,
    "messages": [
        {"role": "system", "content": "You are a terse assistant. Answer in one sentence."},
        {"role": "user", "content": PARA * 420 + "\n\nIn one sentence: what makes a photocopy unusable?"},
    ],
}


def call(body, tag):
    req = urllib.request.Request(
        URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as r:
        d = json.load(r)
    u = d.get("usage", {})
    print(f"  {tag}: {time.time()-t0:6.1f}s  prompt={u.get('prompt_tokens')} "
          f"completion={u.get('completion_tokens')}")
    return time.time()


def main():
    print(f"prompt body: {len(json.dumps(BODY)):,} bytes "
          f"(~{len(BODY['messages'][1]['content'])//4:,} tokens est.)\n")
    print("[1] first request — builds the live checkpoint")
    call(BODY, "turn 1")
    print("\n    pausing 5s so the checkpoint settles")
    time.sleep(5)
    print("\n[2] byte-identical request — strict truncation of the live session")
    call(BODY, "turn 2")
    print("\nNow read the server log for the turn-2 decision:")
    print("  tmux capture-pane -p -S - -t ds4:1.1 | grep -E 'live kv cache|ctx=0\\.\\.|rewind' | tail")
    return 0


if __name__ == "__main__":
    sys.exit(main())
