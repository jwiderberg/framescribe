"""Pydantic models for every stage artifact. These are the contract between stages."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel as _PydanticModel
from pydantic import ConfigDict, Field, field_validator

SCHEMA_VERSION = 1
MAX_SECONDS = 1e7  # no time in any artifact is this large (115 days); a larger one is an error, not an overflow later

# A time in seconds from the start of the video: finite, not negative.
Seconds = Annotated[float, Field(ge=0, le=MAX_SECONDS)]

KEYFRAME_PATH = r"^frames/s\d{4,}_t\d{2,}-\d{2}-\d{2}\.jpg$"  # what the visual stage writes (visual.keyframe_name)
AUDIO_WAV = "work/audio.wav"  # what the ingest stage writes


class BaseModel(_PydanticModel):
    """Every model: NaN and infinities are invalid numbers, wherever they come from (a cached artifact, an imported
    transcript, a backend)."""

    model_config = ConfigDict(allow_inf_nan=False)


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    schema_version: int = SCHEMA_VERSION


class Rect(BaseModel):
    """Pixel rectangle: x, y is the top-left corner, w and h are the size."""

    x: int
    y: int
    w: int
    h: int

    @property
    def x1(self) -> int:
        return self.x + self.w

    @property
    def y1(self) -> int:
        return self.y + self.h


class Caption(BaseModel):
    start: Seconds
    end: Seconds
    speaker: str | None = None
    text: str


class VideoFacts(BaseModel):
    width: int
    height: int
    fps: float
    codec: str


class AudioFacts(BaseModel):
    codec: str
    sample_rate: int
    channels: int


class Layout(BaseModel):
    frame_w: int
    frame_h: int
    content_rect: Rect
    other_rects: list[Rect] = Field(default_factory=list)
    method: str
    confidence: float
    warning: str | None = None


class IngestArtifact(Artifact):
    source: str
    duration: Seconds
    video: VideoFacts
    audio: AudioFacts | None
    audio_wav: Literal["work/audio.wav"] | None = Field(
        description="16 kHz mono PCM, path relative to the run directory (always work/audio.wav); None without audio")
    embedded_captions: list[Caption]
    layout: Layout


class Word(BaseModel):
    """A backend's word. Its times are finite but may be a little off (cross-check sources are sloppy); the words of
    a transcript (TranscriptWord) are not negative."""

    text: str
    start: float
    end: float
    conf: float | None = None


class Segment(BaseModel):
    """A sentence. The word indices are None when the source has no word timings."""

    start: float
    end: float
    text: str
    word_start_idx: int | None = None
    word_end_idx: int | None = Field(default=None, description="exclusive")


class Cut(BaseModel):
    """Where the audio was cut into chunks for decoding."""

    time: Seconds
    kind: Literal["pause", "no_pause"] = Field(description="no_pause: no quiet run was found, so the cut is at a word boundary")
    level_db: float = Field(description="audio level at the cut, dBFS over 30 ms")
    pause_seconds: float = Field(description="length of the quiet run the cut is centred in; for no_pause, the gap between the words")


class AsrPass(BaseModel):
    """Another decode of the same audio by the same backend, on its own cut grid: a cross-check, not merged."""

    name: str
    words: list[Word]
    cuts: list[Cut]


class AsrArtifact(Artifact):
    schema_version: int = 2
    backend: str
    model: str
    language: str | None
    words: list[Word]
    segments: list[Segment]
    audio_seconds: Seconds
    wall_seconds: float
    cuts: list[Cut] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    second_pass: AsrPass | None = Field(default=None, description="parakeet: the decode on the offset cut grid")
    pass_seconds: list[float] = Field(default_factory=list, description="wall time of each decoding pass")

    def describe(self) -> str:
        second = f" (+ {len(self.second_pass.words)} in the second pass)" if self.second_pass else ""
        return (f"{len(self.words)} words{second}, {len(self.segments)} sentences, "
                f"{self.audio_seconds / self.wall_seconds:.0f}x realtime")


class TranscriptWord(Word):
    """A word of the final transcript. `filled`: not heard by the primary backend but by `source`, and inserted."""

    start: Seconds
    end: Seconds
    source: str | None = Field(default=None, description="filled words: the source whose text was used")
    filled: bool = False
    confirmed: bool | None = Field(default=None, description="filled words: True when two or more sources have it")


class Gap(BaseModel):
    """Words that another source has where the primary transcript has none."""

    start: float
    end: float
    text: str
    words: int
    sources: list[str] = Field(description="every source that has this gap")
    chosen: str = Field(description="the source whose text is used")
    status: Literal["confirmed", "unconfirmed", "rejected"] = Field(
        description="confirmed: found by two or more sources, and every filled word (normalised) is in at least two "
                    "sources within the time tolerance; unconfirmed: filled otherwise; rejected: not filled, see reason")
    reason: str | None = Field(default=None, description="rejected: starts with the code (repeats-adjacent, fragment, "
                                                         "no-speech) and a colon; unconfirmed: why, when two sources "
                                                         "found the gap but disagree on its words")


class Completeness(BaseModel):
    """How much of the transcript came from the primary backend and what was filled in from which source."""

    primary: str
    primary_words: int
    sources: list[str] = Field(description="the cross-check sources compared with the primary")
    unavailable: list[str] = Field(default_factory=list, description="cross-check sources that were not available, and why")
    filled_words: int
    filled_by_source: dict[str, int]
    filled_share: float = Field(description="filled words / all words of the transcript")
    confirmed: int = Field(description="gaps found in two or more sources (filled)")
    unconfirmed: int = Field(description="gaps found in one source (filled)")
    rejected: int = Field(description="gaps not filled, with the reason in `gaps`")
    rejected_by_reason: dict[str, int] = Field(default_factory=dict, description="rejected gaps per reason code")
    gaps: list[Gap] = Field(default_factory=list)
    limit: float | None = Field(default=None, description="the run's --max-filled-share")
    above_limit: bool = Field(default=False, description="the filled share is above `limit`: the transcript is kept, "
                              "with `warning`")
    warning: str | None = Field(default=None, description="set when the filled share is above the limit: how much was "
                                "filled in, in words")

    def summary(self) -> str:
        """One line: what the primary was cross-checked against and what was filled in."""
        if self.sources == ["parakeet-pass2"]:
            against = "cross-checked against the second parakeet pass only (no other transcript available)"
        elif self.sources:
            names = [SOURCE_NAMES.get(s, s) for s in self.sources]
            against = "cross-checked against " + (" and ".join(names) if len(names) <= 2
                                                   else ", ".join(names[:-1]) + " and " + names[-1])
        else:
            against = "no cross-check source"
        by = ", ".join(f"{n} from {s}" for s, n in sorted(self.filled_by_source.items())) or "none"
        reasons = ", ".join(f"{n} {r}" for r, n in sorted(self.rejected_by_reason.items()))
        above = (f"; ABOVE THE USUAL {self.limit:.1%}: check the filled-in words"
                 if self.above_limit and self.limit is not None else "")
        return (f"{against}; filled {self.filled_words} words ({self.filled_share:.1%}; {by}): {self.confirmed} "
                f"confirmed, {self.unconfirmed} unconfirmed, {self.rejected} rejected gaps"
                + (f" ({reasons})" if reasons else "") + above)


SOURCE_NAMES = {"parakeet-pass2": "the second parakeet pass", "whisperkit": "WhisperKit",
                "captions": "the embedded captions", "import": "the imported transcript"}


class TranscriptSegment(BaseModel):
    id: int
    start: Seconds
    end: Seconds
    text: str = Field(description="filled words are marked: [+ confirmed +], [? unconfirmed ?]")
    speaker: str | None = None
    words: list[TranscriptWord] = Field(default_factory=list)

    @field_validator("words", mode="before")
    @classmethod
    def _plain_words(cls, value):
        """Plain backend words are primary words."""
        return [w.model_dump() if isinstance(w, Word) and not isinstance(w, TranscriptWord) else w for w in value]


class TranscriptArtifact(Artifact):
    schema_version: int = 3
    primary: str
    segments: list[TranscriptSegment]
    alternates: dict[str, list[Segment]] = Field(default_factory=dict)
    sources: list[str]
    completeness: Completeness | None = Field(default=None, description="None for an imported transcript")


class StageRecord(BaseModel):
    stage: str
    status: Literal["hit", "miss", "forced", "skipped", "failed"]
    wall_seconds: float
    cache_key: str | None = None
    version: int | None = None
    reason: str | None = None
    metrics: dict[str, float | int] = Field(default_factory=dict, description="what the stage measured about its own run, e.g. peak memory")


class RunRecord(BaseModel):
    schema_version: int = 2
    framescribe_version: str
    source: str
    source_fingerprint: str
    config: dict
    stages: list[StageRecord]
    total_wall_seconds: float
    status: Literal["ok", "failed", "interrupted"] = "ok"
    error: str | None = None
    completeness: dict | None = Field(default=None, description="the transcript's completeness figures (without the gap list)")


ChangeKind = Literal["initial", "page", "partial", "scroll", "minor"]


class Change(BaseModel):
    """What happened on the way into a state, from the previous state's settled frame to this one's. A heuristic."""

    kind: ChangeKind
    kind_confidence: float = Field(description="0 to 1; heuristic, not a probability")
    changed_fraction: float = Field(description="share of the content crop's pixels that differ by more than 16 gray levels")
    changed_rect: Rect | None = Field(description="bounding box of the changed pixels in content-crop pixels; None for the first state")
    scroll_dy: int | None = Field(
        description="kind scroll only: px the content moved vertically (negative = moved up, the user scrolled down)"
    )


class CursorSample(BaseModel):
    """Pointer position in content-crop pixels (centre of the pointer's footprint) at time t."""

    t: Seconds
    x: float
    y: float
    confidence: float


class VisualState(BaseModel):
    id: int = Field(description="1-based, in time order; matches the key frame file name")
    start: Seconds
    end: Seconds
    keyframe_time: Seconds
    keyframe_path: str = Field(pattern=KEYFRAME_PATH, description="relative to the run directory: frames/sNNNN_tHH-MM-SS.jpg")
    settled: bool = Field(description="False when the key frame is the best available but the picture was still changing")
    change: Change
    cursor: list[CursorSample] = Field(description="pointer positions seen while this state was current, in time order")


class CapInfo(BaseModel):
    """Safeguards against footage that changes all the time. `hit` is True when any of them acted."""

    max_states_per_minute: int
    min_state_seconds: float
    max_event_seconds: float
    hit: bool
    forced_closes: int = Field(description="changes cut off after max_event_seconds without ever settling")
    suppressed_changes: int = Field(description="changes dropped by min_state_seconds or max_states_per_minute")


class VisualArtifact(Artifact):
    fps_sampled: float
    duration: Seconds
    content_rect: Rect = Field(description="the crop of the video frame that was analysed; all coordinates are inside it")
    states: list[VisualState] = Field(description="contiguous: the first starts at 0, each ends where the next starts, the last at duration")
    cap: CapInfo
    cursor_samples: int = Field(description="pointer positions found over the whole video")
    wall_seconds: float


class OcrLine(BaseModel):
    text: str
    rect: Rect = Field(description="content-crop pixels (not the upscaled image)")
    confidence: float


class OcrScroll(BaseModel):
    """For a state that the visual stage called a scroll: does the text agree with the measured shift?"""

    visual_dy: int
    ocr_dy: int | None = Field(description="the most common vertical shift among lines found in both frames; None without matches")
    matched_lines: int
    consistent_lines: int = Field(description="matched lines whose shift is within tolerance of visual_dy")


class OcrState(BaseModel):
    state_id: int
    keyframe_path: str = Field(pattern=KEYFRAME_PATH)
    content_hash: str = Field(description="sha256 of the key frame file")
    duplicate_of: int | None = Field(description="id of an earlier state with the same key frame, whose result this reuses")
    diff_against: int | None = Field(description="id of the previous state; None for the first, where everything is added")
    lines: list[OcrLine]
    added: list[OcrLine] = Field(description="lines of this state with no counterpart in the previous state")
    removed: list[OcrLine] = Field(description="lines of the previous state with no counterpart in this one (their own coordinates)")
    scroll: OcrScroll | None


class OcrArtifact(Artifact):
    engine: str
    scale: int = Field(description="the key frame is upscaled by this factor (Lanczos) before recognition; rects are mapped back")
    level: str
    language_correction: bool
    languages: list[str]
    states: list[OcrState]
    frames_recognised: int = Field(description="key frames sent to the engine; duplicates are not")
    wall_seconds: float


TimelineEventType = Literal["said", "screen", "pointer"]


class TimelineEvent(BaseModel):
    """One entry of a step's chronological timeline. Which fields are set depends on `type`."""

    type: TimelineEventType
    time: Seconds
    end: Seconds | None = Field(default=None, description="said: the end of the last segment; screen events that collapse a "
                              "run of minor changes without new text: the run's end (None for every other screen event)")
    text: str | None = Field(default=None, description="said: verbatim transcript text, filled-in words marked [+ +] or "
                             "[? ?]; pointer: the nearest on-screen line")
    filled_words: int = Field(default=0, description="said: words in `text` that were filled in from another source")
    speaker: str | None = None
    segment_ids: list[int] = Field(default_factory=list, description="said: the transcript segments merged into this paragraph")
    state_id: int | None = Field(default=None, description="screen: the state that appeared (the first one of a minor run)")
    state_ids: list[int] = Field(default_factory=list, description="screen: every state the event stands for")
    kind: ChangeKind | None = Field(default=None, description="screen: the change kind; collapsed minor runs have kind minor")
    scroll_dy: int | None = Field(default=None, description="screen, scroll: px moved, only set when the OCR text confirms it")
    appeared: list[str] = Field(default_factory=list)
    gone: list[str] = Field(default_factory=list)
    x: float | None = Field(default=None, description="pointer: content-crop pixels")
    y: float | None = None


class TimelineStep(BaseModel):
    id: int
    start: Seconds
    end: Seconds
    title: str
    title_is_fallback: bool = Field(description="True when no usable on-screen line was found and the title is 'Step N'")
    main_state_id: int
    extra_state_ids: list[int]
    state_ids: list[int]
    segment_ids: list[int]
    words: int
    events: list[TimelineEvent]


class TimelineArtifact(Artifact):
    """The merged evidence of the report: steps, each with its chronological events. `report.md` is rendered from it."""

    schema_version: int = 3  # 3: `source` is the file name only
    source: str = Field(description="the source video's file name, without its folder (timeline.json is shared; "
                                    "run.json keeps the full path)")
    duration: Seconds
    asr_backend: str
    step_min_seconds: float
    step_max_seconds: float
    chrome: list[str] = Field(description="normalised on-screen lines present in most states (navigation, sidebar)")
    steps: list[TimelineStep]
