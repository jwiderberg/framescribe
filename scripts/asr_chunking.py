"""Measure parakeet chunk configurations on a full recording against independent transcripts of the same audio.

Run from the repository root, in the main project's environment (it uses the pipeline's own chunking code):

    uv run python scripts/asr_chunking.py RUN_DIR --alt ALT.json --out RESULTS.json [--only NAME ...] [--metrics-only]

RUN_DIR is a pipeline run directory (`out/<stem>/`) with `work/audio.wav`, `work/ingest.json` (embedded captions)
and `work/asr.whisperkit.json`. ALT.json is another parakeet decode of the same audio as `[[text, start], ...]`
(for example in 40 s windows that keep their middle 20 s). Each configuration is decoded with the model loaded
once, and compared with every reference by `framescribe.completeness`: a missing phrase is a gap that a
reference has where the configuration has no words. Prints a Markdown table of counts only (no transcript text).
The decodes are saved next to RESULTS.json (`RESULTS.decodes/`, keep it out of the repository: it holds transcript
text), so `--metrics-only` can redo the comparison without decoding again.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path

from framescribe.chunking import Levels, read_wav
from framescribe.completeness import Source, caption_source, cluster, find_gaps, normalise
from framescribe.config import Config
from framescribe.doctor import require_cached_model
from framescribe.models import AsrArtifact, Cut, IngestArtifact, Word
from framescribe.stages.asr_parakeet import Decoding, decode_pass, plan_cuts
from framescribe.words import attach_leading_punctuation, finalize_words

# name: (target, search, maximum or None, pad, left, right). The first nine rows were measured with an earlier
# bare-chunk retry (a chunk of 30+ words: 2 s of silence, then 8 s of context on both sides), the next two with the
# pipeline's retry chain (asr_parakeet.retry_decodings, chunks of 10+ words), the last with the final pipeline code
# (also asr_parakeet.drop_seam_duplicates).
CONFIGS = {
    "120s pad0.5 L1 R0 (before)": (120.0, 15.0, None, 0.5, 1.0, 0.0),
    "30s pad0.5 L1 R0": (30.0, 5.0, 40.0, 0.5, 1.0, 0.0),
    "30s pad0 L0 R0": (30.0, 5.0, 40.0, 0.0, 0.0, 0.0),
    "30s pad0 L1 R0": (30.0, 5.0, 40.0, 0.0, 1.0, 0.0),
    "30s pad0 L0 R2": (30.0, 5.0, 40.0, 0.0, 0.0, 2.0),
    "30s pad0 L1 R2": (30.0, 5.0, 40.0, 0.0, 1.0, 2.0),
    "30s pad0 L2 R2": (30.0, 5.0, 40.0, 0.0, 2.0, 2.0),
    "30s pad0.5 L1 R2": (30.0, 5.0, 40.0, 0.5, 1.0, 2.0),
    "20s pad0 L1 R2": (20.0, 5.0, 30.0, 0.0, 1.0, 2.0),
    "30s pad0 L1 R2, retry chain": (30.0, 5.0, 40.0, 0.0, 1.0, 2.0),
    "30s pad0 L2 R2, retry chain": (30.0, 5.0, 40.0, 0.0, 2.0, 2.0),
    "30s pad0 L1 R2, retry chain, seam dedupe (final)": (30.0, 5.0, 40.0, 0.0, 1.0, 2.0),
}
_PUNCT = re.compile(r"[.,?!]")
_END = re.compile(r"[.?!]$")


def chunk_words(words: list[Word], cuts: list[Cut], total: float) -> list[list[Word]]:
    edges = [0.0, *(c.time for c in cuts), total]
    return [[w for w in words if a <= w.start < b] for a, b in zip(edges, edges[1:])]


def seam_checks(words: list[Word], cuts: list[Cut], refs: list[Source]) -> dict:
    """At each cut: a word that straddles it, a repeated phrase across it, a sentence break across it, and words
    near it (2 s) that no reference has (garbled or duplicated text)."""
    straddle = sum(1 for c in cuts for w in words if w.start < c.time < w.end - 0.05)
    repeated = breaks = ref_breaks = 0
    wk = next((list(r.words) for r in refs if r.name == "whisperkit"), [])
    for c in cuts:
        before_ref = [w for w in wk if w.start < c.time]
        after_ref = [w for w in wk if w.start >= c.time]
        if before_ref and after_ref and _END.search(before_ref[-1].text) and after_ref[0].text[:1].isupper():
            ref_breaks += 1
        before = [t for w in words if c.time - 3 <= w.start < c.time for t in normalise(w.text)]
        after = [t for w in words if c.time <= w.start < c.time + 3 for t in normalise(w.text)]
        if any(len(before) >= k and before[-k:] == after[:k] for k in (2, 3, 4)):
            repeated += 1
        prev = [w for w in words if w.start < c.time]
        nxt = [w for w in words if w.start >= c.time]
        if prev and nxt and _END.search(prev[-1].text) and nxt[0].text[:1].isupper():
            breaks += 1
    # words of this configuration that every reference lacks, near a cut
    as_source = Source("config", words)
    extra_sets = []
    for r in refs:
        if r.cue_level:
            continue
        extra = set()
        for g in find_gaps(list(r.words), as_source):
            extra.update(range(g.first, g.last + 1))
        extra_sets.append(extra)
    common = set.intersection(*extra_sets) if extra_sets else set()
    near = sum(1 for i in common if any(abs(words[i].start - c.time) <= 2.0 for c in cuts))
    return {"straddling_words": straddle, "repeated_at_seams": repeated, "sentence_breaks_at_cuts": breaks,
            "whisperkit_sentence_breaks_at_cuts": ref_breaks,
            "unreferenced_words_near_cuts": near, "unreferenced_words": len(common)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--alt", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--metrics-only", action="store_true", help="recompute the comparison from the saved decodes")
    args = ap.parse_args()
    work = args.run_dir / "work"
    samples, rate = read_wav(work / "audio.wav")
    total = len(samples) / rate
    levels = Levels(samples, rate)
    ingest = IngestArtifact.model_validate_json((work / "ingest.json").read_text())
    wk = AsrArtifact.model_validate_json((work / "asr.whisperkit.json").read_text())
    alt = [Word(text=t, start=s, end=s + 0.3) for t, s in json.loads(args.alt.read_text())]
    refs = [Source("alt", alt), Source("whisperkit", wk.words), caption_source(ingest.embedded_captions)]

    model = None
    if not args.metrics_only:
        require_cached_model(Config().parakeet_model)  # offline, and fails with "run framescribe setup" when the model is not cached
        os.environ.setdefault("HF_HUB_VERBOSITY", "error")
        from parakeet_mlx import from_pretrained

        model = from_pretrained(Config().parakeet_model)
    results = json.loads(args.out.read_text()) if args.out.is_file() else {}
    decodes = args.out.with_suffix(".decodes")
    decodes.mkdir(exist_ok=True)
    for n, (name, (target, search, maximum, pad, left, right)) in enumerate(CONFIGS.items()):
        if args.only and name not in args.only:
            continue
        saved = decodes / f"config{n}.json"
        if args.metrics_only:
            if not saved.is_file():
                continue
            d = json.loads(saved.read_text())
            words = [Word(**w) for w in d["words"]]
            cuts = [Cut(**c) for c in d["cuts"]]
            wall, warnings = d["wall_seconds"], d["warnings"]
        else:
            planned = plan_cuts(levels, total, target, search, maximum)
            t0 = time.perf_counter()
            res = decode_pass(model, samples, rate, levels, planned, Decoding(pad=pad, left=left, right=right), work,
                              search)
            wall = time.perf_counter() - t0
            words = finalize_words(attach_leading_punctuation(res.words), total)
            cuts, warnings = res.cuts, res.warnings
            saved.write_text(json.dumps({"name": name, "words": [w.model_dump() for w in words],
                                         "cuts": [c.model_dump() for c in cuts], "wall_seconds": wall,
                                         "warnings": warnings}))
        chunks = chunk_words(words, cuts, total)
        row = {"words": len(words), "chunks": len(chunks), "wall_seconds": round(wall, 1),
               "longest_chunk": round(max(b - a for a, b in zip([0.0, *(c.time for c in cuts)], [*(c.time for c in cuts), total])), 1),
               "no_pause_cuts": sum(c.kind == "no_pause" for c in cuts),
               "unpunctuated_chunks": sum(1 for ch in chunks if len(ch) >= 10 and not any(_PUNCT.search(w.text) for w in ch)),
               "bare_warnings": sum("without punctuation" in w for w in warnings)}
        all_gaps = []
        for r in refs:
            gaps = find_gaps(words, r)
            all_gaps += gaps
            row[f"missing_{r.name}"] = len(gaps)
            row[f"missing_{r.name}_3plus"] = sum(1 for g in gaps if len(g.text.split()) >= 3)
            row[f"missing_{r.name}_words"] = sum(len(g.text.split()) for g in gaps)
        clusters = cluster(all_gaps)
        row["missing_union"] = len(clusters)
        row["missing_in_2plus_refs"] = sum(1 for c in clusters if len({g.source for g in c}) >= 2)
        row["missing_in_2plus_refs_3plus"] = sum(1 for c in clusters if len({g.source for g in c}) >= 2
                                                 and max(len(g.text.split()) for g in c) >= 3)
        row.update(seam_checks(words, cuts, refs))
        results[name] = row
        args.out.write_text(json.dumps(results, indent=1))
        print(name, row, flush=True)
    cols = ["words", "chunks", "longest_chunk", "missing_union", "missing_in_2plus_refs", "missing_in_2plus_refs_3plus", "missing_alt",
            "missing_alt_3plus", "missing_whisperkit", "missing_captions", "unpunctuated_chunks", "bare_warnings",
            "sentence_breaks_at_cuts", "whisperkit_sentence_breaks_at_cuts", "repeated_at_seams", "straddling_words", "unreferenced_words_near_cuts",
            "wall_seconds"]
    print("| configuration | " + " | ".join(cols) + " |")
    print("|---" * (len(cols) + 1) + "|")
    for name, row in results.items():
        print(f"| {name} | " + " | ".join(str(row.get(c, "")) for c in cols) + " |")


if __name__ == "__main__":
    main()
