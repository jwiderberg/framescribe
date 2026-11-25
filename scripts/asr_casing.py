"""Measure forced capitals at parakeet's chunk cuts: how often the first word after a cut is capitalised although the
word before it ends no sentence, against the same rate at pauses that are not cuts and against the other pass's
words at the same instants (where that pass has no cut). Reads a pipeline run directory; no model is loaded.

    uv run python scripts/asr_casing.py RUN_DIR

Prints counts and rates only (no transcript text).
"""

from __future__ import annotations

import argparse
import re
from bisect import bisect_left
from collections.abc import Sequence
from pathlib import Path

from framescribe.models import AsrArtifact, Word

_STOP = re.compile(r"[.?!…]+[\"')\]]*$")
_I_FORMS = {"i", "i'm", "i'll", "i'd", "i've"}
MIN_PAUSE_SECONDS = 0.3  # a pair of words this far apart, without a cut between them, is a "pause" pair


def _capital(text: str) -> bool:
    return text[:1].isupper() and re.sub(r"[^\w']", "", text.lower()) not in _I_FORMS


def capital_rates(words: Sequence[Word], cut_times: Sequence[float]) -> dict:
    """For word pairs across a cut and across a pause of MIN_PAUSE_SECONDS that is not a cut: the number of pairs
    and the share whose second word is capitalised while the first ends no sentence."""
    cuts = sorted(cut_times)
    at_cut = [0, 0]
    at_pause = [0, 0]
    for a, b in zip(words, words[1:]):
        k = bisect_left(cuts, a.start + 1e-9)
        is_cut = k < len(cuts) and a.start < cuts[k] <= b.start
        row = at_cut if is_cut else at_pause if b.start - a.end >= MIN_PAUSE_SECONDS else None
        if row is not None:
            row[0] += 1
            row[1] += _capital(b.text) and not _STOP.search(a.text)
    return {"cut_pairs": at_cut[0], "capital_without_stop_at_cuts": round(at_cut[1] / at_cut[0], 4) if at_cut[0] else None,
            "pause_pairs": at_pause[0],
            "capital_without_stop_at_pauses": round(at_pause[1] / at_pause[0], 4) if at_pause[0] else None}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", type=Path)
    args = ap.parse_args()
    art = AsrArtifact.model_validate_json((args.run_dir / "work/asr.parakeet.json").read_text())
    if art.second_pass is None:
        raise SystemExit("the artifact has no second pass")
    p1, p2 = art.words, art.second_pass.words
    c1, c2 = [c.time for c in art.cuts], [c.time for c in art.second_pass.cuts]
    rows = {
        "pass 1 at its cuts / pauses": capital_rates(p1, c1),
        "pass 2 at its cuts / pauses": capital_rates(p2, c2),
        "pass 2 words at pass 1's cut instants (baseline)": capital_rates(p2, c1),
        "pass 1 words at pass 2's cut instants (baseline)": capital_rates(p1, c2),
    }
    for name, r in rows.items():
        print(f"{name}: {r}")


if __name__ == "__main__":
    main()
