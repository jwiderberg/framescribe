"""Writes the two synthetic WhisperKit report fixtures next to this file.

    python tests/fixtures/make_whisperkit_fixtures.py

Both fixtures are invented: the sentences are written here, and every duration, pause, probability and score is drawn
from a seeded random generator (`random.Random(SEED).random()` only, which is stable across Python versions). Nothing is
taken from a recording or from a real WhisperKit run, and the timings block holds obviously artificial values.
tests/test_asr.py checks that the committed files equal what this script produces and derives its expected numbers
from the design below.

`whisperkit_report.json`  a 30 s report: window 0 (seek 0) with four segments, then a later window with one segment,
                          ending in a special token. The first word has zero length. Used by the parser test and by
                          the stage tests that copy it in place of a CLI report.
`whisperkit_restart_report.json`  a 60 s report that exercises the decoder restart:
    window 0   (seek 0 s)   two segments, real words from 3.4 s to about 15 s
    window A   (seek 24 s)  short one-to-four-word segments from 24.9 s to before 40 s, then two hallucinated words
                            at TAIL_AT (51.31 s and 52.74 s), far beyond where window B restarts
    window B   (seek 40 s)  restarts at RESTART_AT = 40.90 s and holds the real words up to the audio end, in one
                            segment that spans the tail's times
  So parse_report must drop exactly the two hallucinated words of window A and keep window B's true timings.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

SEED = 20240611
HERE = Path(__file__).parent
SAMPLE_RATE = 16000

RESTART_AT = 40.90
TAIL_AT = (51.31, 52.74)
TAIL_WORDS = ("Yes.", "Right.")
WINDOW_A_SEEK_S = 24
WINDOW_B_SEEK_S = 40

REPORT_SENTENCES = [
    ["The baker lit the oven before dawn.", "Flour drifted across the narrow counter like fine snow."],
    ["She counted twelve loaves, then six more.", "A cold wind rattled the shutters all morning."],
    ["Nobody remembered where the spare scale went."],
    ["Cinnamon and burnt sugar filled the stairwell.", "He wiped his hands and checked the timer again."],
]
REPORT_LATE_SENTENCES = [["The delivery van was late, as usual."]]
REPORT_LATE_SEEK_S = 25.6

RESTART_WINDOW0 = [
    ["Rain tapped softly on the skylight.", "We should label the trays before the lunch crowd arrives."],
    ["Honey needs a slower hand than molasses.", "The apprentice stirred the starter twice.", "Tomorrow's batch will use the rye."],
]
RESTART_WINDOW_A = [["Sure,", "go", "ahead."], ["Yes."], ["Honey", "first."], ["Then", "the", "rye", "flour."],
                    ["Right."], ["Please", "move", "the", "ladder."], ["Fine."]]
RESTART_WINDOW_B = [
    # one segment on purpose: it starts before the hallucinated tail and ends after it, so a parser that takes the
    # restart point from the segment's end instead of its first word keeps the tail and fails the fixture test
    [["Right."], ["Somebody propped the back door with a crate of lemons.", "Steam curled up from the cooling racks."],
     ["Keep the dough away from the window.", "Three bells rang from the harbour chapel."],
     ["The kettle whistled, so nobody heard the phone.", "Pears are cheaper this week, though bruised."]],
]

TIMINGS = {  # artificial: nothing here was measured
    "pipelineStart": 1000.0, "firstTokenTime": 1000.5, "fullPipeline": 10.0, "modelLoading": 1.0, "audioLoading": 0.0,
    "audioProcessing": 0.0, "logmels": 0.0, "encoding": 1.0, "prefill": 0.0, "decodingInit": 0.0,
    "decodingLoop": 5.0, "decodingPredictions": 4.0, "decodingFiltering": 0.0, "decodingSampling": 0.0,
    "decodingKvCaching": 0.0, "decodingWordTimestamps": 0.0, "decodingNonPrediction": 0.0, "decodingWindowing": 0.0,
    "decodingFallback": 0, "encoderLoadTime": 0.0, "decoderLoadTime": 0.0, "tokenizerLoadTime": 0.0,
    "prewarmLoadTime": 0, "encoderSpecializationTime": 0, "decoderSpecializationTime": 0,
    "totalDecodingFallbacks": 0,
}


class Maker:
    def __init__(self, seed: int):
        self.rng = random.Random(seed)

    def uniform(self, lo: float, hi: float, digits: int = 2) -> float:
        return round(lo + (hi - lo) * self.rng.random(), digits)

    def probability(self) -> float:
        r = self.rng.random()
        return round(1.0 if r < 0.45 else 0.3 + 0.7 * self.rng.random(), 2)

    def words(self, tokens: list[str], t: float) -> tuple[list[dict], float]:
        """Contiguous words from `t` (each starting where the one before ended); the time after the last one."""
        out = []
        for token in tokens:
            end = round(t + self.uniform(0.1, 0.5), 2)
            out.append({"word": " " + token, "start": t, "end": end, "probability": self.probability()})
            t = end
        return out, t

    def segment(self, seg_id: int, seek: int, words: list[dict]) -> dict:
        return {
            "id": seg_id, "seek": seek, "start": words[0]["start"], "end": words[-1]["end"],
            "text": "".join(w["word"] for w in words if not w["word"].startswith("<|")).strip(),
            "words": words, "temperature": 0, "noSpeechProb": 0,
            "avgLogprob": -self.uniform(0.05, 0.35, 4), "compressionRatio": self.uniform(1.0, 2.0, 3),
        }


def tokens_of(sentences: list[str]) -> list[str]:
    return [token for sentence in sentences for token in sentence.split()]


def report(segments: list[dict], audio_seconds: int, windows: int) -> dict:
    timings = {**TIMINGS, "inputAudioSeconds": audio_seconds, "totalDecodingWindows": windows,
               "totalEncodingRuns": windows, "totalLogmelRuns": windows, "totalAudioProcessingRuns": windows,
               "totalTimestampAlignmentRuns": windows, "totalDecodingLoops": 10 * windows,
               "totalKVUpdateRuns": 10 * windows}
    return {"text": " ".join(s["text"] for s in segments), "segments": segments, "language": "en", "seekTime": None,
            "timings": dict(sorted(timings.items()))}


def build_report() -> dict:
    m = Maker(SEED)
    segments, t = [], 0.0
    for sentences in REPORT_SENTENCES:
        words = []
        for sentence in sentences:
            part, t = m.words(sentence.split(), t)
            words += part
            t = round(t + m.uniform(0.1, 0.5), 2)
        if not segments:
            words[0]["end"] = words[0]["start"]  # a zero-length first word
        segments.append(m.segment(len(segments), 0, words))
    t = round(max(t, REPORT_LATE_SEEK_S + 0.1), 2)
    words, t = m.words(tokens_of(REPORT_LATE_SENTENCES[0]), t)
    words.append({"word": "<|endoftext|>", "start": t, "end": t, "probability": 0.5})
    segments.append(m.segment(len(segments), round(REPORT_LATE_SEEK_S * SAMPLE_RATE), words))
    assert words[-1]["end"] <= 29.9, "the last word must end inside the 30 s of audio"
    return report(segments, 30, 2)


def build_restart_report() -> dict:
    m = Maker(SEED + 1)
    segments, t = [], 3.4
    for sentences in RESTART_WINDOW0:
        words = []
        for sentence in sentences:
            part, t = m.words(sentence.split(), t)
            words += part
            t = round(t + m.uniform(0.1, 0.5), 2)
        segments.append(m.segment(len(segments), 0, words))
    assert t < WINDOW_A_SEEK_S
    seek_a = WINDOW_A_SEEK_S * SAMPLE_RATE
    t = 24.9
    for tokens in RESTART_WINDOW_A:
        words, t = m.words(tokens, t)
        segments.append(m.segment(len(segments), seek_a, words))
        t = round(t + m.uniform(0.4, 1.6), 2)
    assert t < RESTART_AT - 0.5, "window A's real words must end before window B restarts"
    for word, start in zip(TAIL_WORDS, TAIL_AT):  # hallucinated: far beyond the restart, low probability
        end = round(start + m.uniform(0.2, 0.4), 2)
        words = [{"word": " " + word, "start": start, "end": end, "probability": m.uniform(0.1, 0.2)}]
        segments.append(m.segment(len(segments), seek_a, words))
    seek_b = WINDOW_B_SEEK_S * SAMPLE_RATE
    t = RESTART_AT
    for part_sentences in RESTART_WINDOW_B:
        words = []
        for group in part_sentences:
            for sentence in group:
                part, t = m.words(sentence.split(), t)
                words += part
                t = round(t + m.uniform(0.1, 0.5), 2)
        segments.append(m.segment(len(segments), seek_b, words))
    assert segments[-1]["words"][-1]["end"] <= 59.8, "window B must end inside the 60 s of audio"
    return report(segments, 60, 3)


def main() -> None:
    for name, build in (("whisperkit_report.json", build_report), ("whisperkit_restart_report.json", build_restart_report)):
        (HERE / name).write_text(json.dumps(build(), indent=1) + "\n")


if __name__ == "__main__":
    main()
