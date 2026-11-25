"""Small synthetic artifacts for the report tests: states, segments and OCR lines built in code."""

from __future__ import annotations

import random
from pathlib import Path

from framescribe.models import (
    CapInfo,
    Change,
    CursorSample,
    OcrArtifact,
    OcrLine,
    OcrScroll,
    OcrState,
    Rect,
    TranscriptArtifact,
    TranscriptSegment,
    VisualArtifact,
    VisualState,
    Word,
)

FRAME_W, FRAME_H = 960, 516


def vstate(id: int, start: float, end: float, kind: str = "partial", conf: float = 0.9, frac: float = 0.1,
           settled: bool = True, cursor: list[CursorSample] | None = None, dy: int | None = None) -> VisualState:
    return VisualState(
        id=id, start=start, end=end, keyframe_time=(start + end) / 2, keyframe_path=f"frames/s{id:04d}.jpg", settled=settled,
        change=Change(kind=kind, kind_confidence=conf, changed_fraction=frac,  # type: ignore[arg-type]
                      changed_rect=None if kind == "initial" else Rect(x=0, y=0, w=FRAME_W, h=FRAME_H), scroll_dy=dy),
        cursor=cursor or [],
    )


def visual(states: list[VisualState]) -> VisualArtifact:
    return VisualArtifact(
        fps_sampled=2.0, duration=states[-1].end, content_rect=Rect(x=0, y=0, w=FRAME_W, h=FRAME_H), states=states,
        cap=CapInfo(max_states_per_minute=20, min_state_seconds=1.5, max_event_seconds=10.0, hit=False, forced_closes=0,
                    suppressed_changes=0),
        cursor_samples=sum(len(s.cursor) for s in states), wall_seconds=1.0,
    )


def segment(id: int, start: float, end: float, text: str = "some words here", speaker: str | None = None) -> TranscriptSegment:
    n = len(text.split())
    words = [Word(text=w, start=start + (end - start) * i / n, end=start + (end - start) * (i + 1) / n) for i, w in enumerate(text.split())]
    return TranscriptSegment(id=id, start=start, end=end, text=text, speaker=speaker, words=words)


def transcript(segments: list[TranscriptSegment]) -> TranscriptArtifact:
    return TranscriptArtifact(primary="parakeet", segments=segments, sources=["parakeet"])


def line(text: str, x: int = 100, y: int = 100, w: int = 120, h: int = 12) -> OcrLine:
    return OcrLine(text=text, rect=Rect(x=x, y=y, w=w, h=h), confidence=1.0)


def ostate(state_id: int, lines: list[OcrLine] | None = None, added: list[OcrLine] | None = None,
           removed: list[OcrLine] | None = None, scroll: OcrScroll | None = None) -> OcrState:
    lines = lines or []
    return OcrState(
        state_id=state_id, keyframe_path=f"frames/s{state_id:04d}.jpg", content_hash=f"h{state_id}", duplicate_of=None,
        diff_against=state_id - 1 if state_id > 1 else None, lines=lines, added=lines if added is None else added,
        removed=removed or [], scroll=scroll,
    )


def ocr(states: list[OcrState]) -> OcrArtifact:
    return OcrArtifact(engine="test", scale=2, level="accurate", language_correction=True, languages=["en"], states=states,
                       frames_recognised=len(states), wall_seconds=1.0)


def regular_states(n: int, seconds: float = 10.0, kinds: dict[int, str] | None = None) -> list[VisualState]:
    """n states of equal length; the first is `initial`, the others `partial` unless `kinds` says otherwise.
    A page kind in `kinds` is given full confidence."""
    out = []
    for i in range(n):
        kind = "initial" if i == 0 else (kinds or {}).get(i, "partial")
        out.append(vstate(i + 1, i * seconds, (i + 1) * seconds, kind=kind, conf=1.0))
    return out


def random_run(seed: int) -> tuple[VisualArtifact, TranscriptArtifact, OcrArtifact]:
    """A random but valid set of artifacts: varied state lengths and kinds, pauses, cursor samples, OCR lines."""
    rng = random.Random(seed)
    states: list[VisualState] = []
    t = 0.0
    for i in range(rng.randint(5, 150)):
        d = rng.uniform(1.5, 40.0)
        kind = "initial" if i == 0 else rng.choices(["page", "partial", "scroll", "minor"], [2, 4, 3, 3])[0]
        cur = [CursorSample(t=t + rng.uniform(0, d), x=rng.uniform(0, FRAME_W), y=rng.uniform(0, FRAME_H), confidence=0.5)
               for _ in range(rng.choice([0, 0, 0, 1, 2]))]
        states.append(vstate(i + 1, t, t + d, kind=kind, conf=rng.choice([0.3, 0.7, 1.0]), frac=rng.random(),
                             settled=rng.random() > 0.1, cursor=cur, dy=rng.randint(-200, 200) if kind == "scroll" else None))
        t += d
    segs: list[TranscriptSegment] = []
    s = rng.uniform(0, 3)
    while s < t + 20:
        d = rng.uniform(0.5, 12)
        text = " ".join(rng.choice(["this", "button", "okay", "so", "data", "here", "um", "list"]) for _ in range(rng.randint(1, 9)))
        segs.append(segment(len(segs), s, s + d, text, speaker=rng.choice(["A", "A", "A", "B"])))
        s += d + rng.choice([0.0, 0.2, 0.5, 2.0, 5.0])
    pool = ["Home", "Settings", "Shelves", "Loans", "Borrowers", "Total 42", "Save", "Cancel", "Name", "Search"]
    ostates = []
    for st in states:
        ls = [line(rng.choice(pool), rng.randint(0, 800), rng.randint(0, 500), h=rng.choice([10, 12, 20])) for _ in range(rng.randint(0, 12))]
        ostates.append(ostate(st.id, ls, added=ls[: rng.randint(0, len(ls))], removed=ls[: rng.randint(0, 2)]))
    return visual(states), transcript(segs), ocr(ostates)


def make_frames(run_dir: Path, v: VisualArtifact) -> None:
    for st in v.states:
        p = run_dir / st.keyframe_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"jpeg")
