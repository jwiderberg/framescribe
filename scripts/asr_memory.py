"""Memory of the parakeet stage's decode loop: per decode, MLX active and cache memory and the process's resident
size, and MLX's peak at the end. Runs both passes of the stage on a WAV (16 kHz mono, keep it to 10 minutes or less).

Usage: uv run python scripts/asr_memory.py AUDIO.wav OUT.json [--limit auto|MB] [--no-limit] [--min-free 30]

--limit auto  (the default) the bound the stage sets for itself (Config.mlx_cache_limit_mb, 512 MB), through the
              stage's own code
--limit MB    an experiment: this cache limit, set here around the decode loop
--no-limit    no bound on the MLX cache (the stage before the memory bound); it grows by about 0.5 GB per decode
              and can exhaust the machine's memory, so use it on short audio only

The script aborts when the system's free memory falls below --min-free percent (read from `memory_pressure`). Nothing
of the audio's content is printed, only numbers; the words go to OUT.json for comparing two runs.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import resource
import subprocess
import sys
import time
from pathlib import Path

import mlx.core as mx
import numpy as np
from parakeet_mlx import from_pretrained

from framescribe.chunking import Levels, read_wav
from framescribe.config import Config
from framescribe.doctor import model_load_path, require_cached_model
from framescribe.stages import asr_parakeet

MB = 1024 * 1024


def free_percent() -> int:
    out = subprocess.run(["memory_pressure"], capture_output=True, text=True, check=True).stdout
    m = re.search(r"free percentage:\s*(\d+)%", out)
    if not m:
        raise RuntimeError("cannot read the free memory percentage from memory_pressure")
    return int(m.group(1))


def rss_mb() -> float:
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True)
    return int(out.stdout.strip()) / 1024


class Recorder:
    """Wraps the model: after each decode, one row of memory numbers; before each, the free-memory guard."""

    def __init__(self, model, min_free: int, log: Path) -> None:
        self.model, self.min_free, self.log = model, min_free, log
        self.rows: list[dict] = []
        self.lowest_free = 100
        self.pass_name = "pass1"

    def transcribe(self, path):
        free = free_percent()
        self.lowest_free = min(self.lowest_free, free)
        if free < self.min_free:
            raise MemoryError(f"system free memory {free}% is below {self.min_free}%: aborting")
        t0 = time.perf_counter()
        result = self.model.transcribe(path)
        seconds = time.perf_counter() - t0
        free = free_percent()
        self.lowest_free = min(self.lowest_free, free)
        self.rows.append({
            "i": len(self.rows), "pass": self.pass_name, "seconds": round(seconds, 3),
            "active_mb": round(mx.get_active_memory() / MB, 1), "cache_mb": round(mx.get_cache_memory() / MB, 1),
            "rss_mb": round(rss_mb(), 1), "free_pct": free,
        })
        with self.log.open("a") as f:  # one line per decode, so an aborted run leaves its numbers
            f.write(json.dumps(self.rows[-1]) + "\n")
        return result


def scope(limit: str):
    if limit == "none":
        return contextlib.nullcontext()
    if limit == "auto":
        return asr_parakeet.mlx_memory_scope(Config().mlx_cache_limit_mb, lambda message: None, {})
    return asr_parakeet.mlx_memory_scope(int(limit), lambda message: None, {})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--limit", default="auto", help="auto (the stage's own bound, the default) or a number of MB")
    ap.add_argument("--no-limit", action="store_true", help="do not bound the MLX cache (exhausts memory on long audio)")
    ap.add_argument("--min-free", type=int, default=30)
    args = ap.parse_args()
    if args.no_limit:
        if args.limit != "auto":
            ap.error("--no-limit cannot be combined with --limit")
        args.limit = "none"

    samples, rate = read_wav(args.audio)
    seconds = len(samples) / rate
    if seconds > 600.5:
        raise SystemExit("keep the audio to 10 minutes or less")
    cfg = Config()
    free = free_percent()
    if free < 50:
        raise SystemExit(f"system free memory is {free}%, below the 50% needed to start")

    require_cached_model(cfg.parakeet_model)  # offline, and fails with "run framescribe setup" when the model is not cached
    t_all = time.perf_counter()
    model = from_pretrained(model_load_path(cfg.parakeet_model))  # the pinned snapshot
    after_load = {"active_mb": round(mx.get_active_memory() / MB, 1), "cache_mb": round(mx.get_cache_memory() / MB, 1),
                  "rss_mb": round(rss_mb(), 1)}
    mx.reset_peak_memory()
    args.out.with_suffix(".rows.jsonl").unlink(missing_ok=True)
    rec = Recorder(model, args.min_free, args.out.with_suffix(".rows.jsonl"))
    levels = Levels(samples, rate)
    decoding = asr_parakeet.Decoding()
    planned = asr_parakeet.plan_cuts(levels, seconds, cfg.chunk_seconds, cfg.cut_search_seconds, cfg.chunk_max_seconds)
    t0 = time.perf_counter()
    with scope(args.limit):
        first = asr_parakeet.decode_pass(rec, samples, rate, levels, planned, decoding, args.out.parent,
                                         cfg.cut_search_seconds)
        rec.pass_name = "pass2"
        plan2 = asr_parakeet.plan_offset_cuts(levels, first.cuts, seconds, cfg.chunk_max_seconds)
        second = asr_parakeet.decode_pass(rec, samples, rate, levels, plan2, decoding, args.out.parent,
                                          cfg.cut_search_seconds, label="pass 2: ")
    summary = {
        "audio_seconds": seconds, "limit": args.limit, "after_load": after_load,
        "decode_seconds": round(time.perf_counter() - t0, 2), "total_seconds": round(time.perf_counter() - t_all, 2),
        "mlx_peak_mb": round(mx.get_peak_memory() / MB, 1),
        "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / MB, 1),  # macOS: bytes
        "lowest_free_pct": rec.lowest_free, "decodes": len(rec.rows),
    }
    words = [[w.text, round(w.start, 3), round(w.end, 3)] for w in first.words]
    words2 = [[w.text, round(w.start, 3), round(w.end, 3)] for w in second.words]
    args.out.write_text(json.dumps({"summary": summary, "rows": rec.rows, "words": words, "words2": words2}))
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
