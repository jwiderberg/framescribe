"""Cutting long audio into chunks at pauses, so no word is decoded twice and no chunk merge is needed."""

from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

LEVEL_HOP_SECONDS = 0.01
LEVEL_WINDOW_HOPS = 3  # levels are measured over 30 ms
FLOOR_PERCENTILE = 5  # the noise floor: the level that 5 % of the audio is quieter than
SPEECH_PERCENTILE = 95
PAUSE_FRACTION = 0.3  # quiet: below floor + this fraction of the way from the floor to the speech level
MIN_PAUSE_SECONDS = 0.2  # a shorter quiet run is a gap inside speech, not a pause to cut in


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Samples of a 16-bit mono PCM wav as int16, and the sample rate."""
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise ValueError(f"{path}: expected 16-bit mono PCM")
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16), w.getframerate()


def write_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())


@dataclass(frozen=True)
class Pause:
    centre: float
    seconds: float


class Levels:
    """Audio level in dBFS every LEVEL_HOP_SECONDS, and the level below which audio counts as quiet."""

    def __init__(self, samples: np.ndarray, rate: int) -> None:
        hop = int(LEVEL_HOP_SECONDS * rate)
        frames = samples[: len(samples) // hop * hop].astype(np.float64).reshape(-1, hop)
        energy = np.convolve((frames * frames).mean(axis=1), np.ones(LEVEL_WINDOW_HOPS) / LEVEL_WINDOW_HOPS, "same")
        self.db = 10 * np.log10(np.maximum(energy, 1e-12)) - 20 * np.log10(32768)
        if len(self.db):
            floor, speech = np.percentile(self.db, [FLOOR_PERCENTILE, SPEECH_PERCENTILE])
        else:
            floor = speech = -120.0
        self.threshold = float(floor + PAUSE_FRACTION * (speech - floor))

    def at(self, t: float) -> float:
        return float(self.db[min(len(self.db) - 1, max(0, int(t / LEVEL_HOP_SECONDS)))])

    def speech_share(self, a: float, b: float) -> float:
        """The share of the 10 ms frames in [a, b] whose level is at or above the pause threshold."""
        i, j = max(0, int(a / LEVEL_HOP_SECONDS)), min(len(self.db), int(np.ceil(b / LEVEL_HOP_SECONDS)))
        return float(np.mean(self.db[i:j] >= self.threshold)) if j > i else 0.0

    def longest_pause(self, lo: float, hi: float, target: float) -> Pause | None:
        """The longest quiet run whose centre lies in [lo, hi] (ties nearest `target`), if it is at least
        MIN_PAUSE_SECONDS long. A run is measured in full even where it extends past the range."""
        quiet = np.concatenate([[False], self.db < self.threshold, [False]])
        edges = np.flatnonzero(np.diff(quiet.astype(np.int8)))
        starts, ends = edges[0::2], edges[1::2]  # run i covers hops [starts[i], ends[i])
        centres = (starts + ends) / 2 * LEVEL_HOP_SECONDS
        lengths = (ends - starts) * LEVEL_HOP_SECONDS
        inside = (centres >= lo) & (centres <= hi) & (lengths >= MIN_PAUSE_SECONDS - 1e-9)
        if not inside.any():
            return None
        order = np.lexsort((np.abs(centres[inside] - target), -lengths[inside]))
        i = order[0]
        return Pause(centre=float(centres[inside][i]), seconds=float(lengths[inside][i]))
