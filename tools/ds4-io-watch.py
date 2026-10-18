#!/usr/bin/env python3
"""Sample live disk I/O and memory footprint of a running ds4 process.

Uses libproc's proc_pid_rusage(), the same counters Activity Monitor shows as
"Bytes Written" / "Bytes Read", plus wired size and phys footprints.

    ./tools/ds4-io-watch.py [pid] [--interval 10] [--samples 6]

Pass no pid to auto-select the single running ds4-server / ds4 / ds4-agent.
"""
import argparse
import ctypes
import re
import struct
import subprocess
import sys
import time

LIBPROC = ctypes.CDLL("/usr/lib/libproc.dylib")
RUSAGE_INFO_V4 = 4
BUF_BYTES = 512
# rusage_info_v0 starts with ri_uuid[16], then uint64 fields in order.
# v2 appends ri_diskio_bytesread (index 16) and ri_diskio_byteswritten (17).
OFF_READ = 16 + 8 * 16
OFF_WRITE = 16 + 8 * 17
OFF_WIRED = 16 + 8 * 5
OFF_RESIDENT = 16 + 8 * 6
OFF_FOOTPRINT = 16 + 8 * 7


def sample(pid: int, buf) -> tuple[int, int, int, int]:
    LIBPROC.proc_pid_rusage.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_char * BUF_BYTES)
    ]
    if LIBPROC.proc_pid_rusage(pid, RUSAGE_INFO_V4, buf) != 0:
        raise SystemExit(f"proc_pid_rusage failed for pid {pid}")
    raw = bytes(buf.raw)
    read_b, write_b = struct.unpack_from("<QQ", raw, OFF_READ)
    wired, resident, footprint = struct.unpack_from("<QQQ", raw, OFF_WIRED)
    return read_b, write_b, footprint, wired


def find_pid() -> int:
    out = subprocess.run(["pgrep", "-fl", "ds4(-server| |-agent)"],
                         capture_output=True, text=True, check=False).stdout
    pids = [int(line.split(None, 1)[0]) for line in out.splitlines()
            if re.search(r"ds4(-server| |-agent)", line)]
    if len(pids) != 1:
        raise SystemExit(f"expected exactly one ds4 process, found {pids}")
    return pids[0]


def gib(n: float) -> str:
    return f"{n / 1024 ** 3:.2f} GiB"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pid", type=int, nargs="?")
    ap.add_argument("--interval", type=float, default=10.0)
    ap.add_argument("--samples", type=int, default=6)
    args = ap.parse_args()

    pid = args.pid or find_pid()
    buf = ctypes.create_string_buffer(BUF_BYTES)
    prev_read, prev_write, _, _ = sample(pid, buf)
    start_read, start_write = prev_read, prev_write
    start_t = time.time()
    print(f"pid {pid}: footprint/wired at start; then per-interval write rates")
    print(f"{'t':>6} {'write/s':>12} {'write/interval':>15} {'read/s':>12} "
          f"{'footprint':>11} {'wired':>11}")
    for i in range(args.samples):
        time.sleep(args.interval)
        read_b, write_b, footprint, wired = sample(pid, buf)
        dw = write_b - prev_write
        dr = read_b - prev_read
        prev_read, prev_write = read_b, write_b
        print(f"{(i + 1) * args.interval:>6.0f} {dw / args.interval / 1024 ** 2:>10.1f}M"
              f" {gib(dw):>15} {dr / args.interval / 1024 ** 2:>10.1f}M"
              f" {gib(footprint):>11} {gib(wired):>11}")
    total_w = prev_write - start_write
    span = time.time() - start_t
    print(f"\nwindow: {total_w / 1024 ** 3:.2f} GiB written in {span / 60:.1f} min "
          f"= {total_w / span * 86400 / 1024 ** 3:.1f} GiB/day projected")
    print(f"lifetime: {prev_write / 1024 ** 3:.2f} GiB written, "
          f"{prev_read / 1024 ** 3:.2f} GiB read")


if __name__ == "__main__":
    main()
