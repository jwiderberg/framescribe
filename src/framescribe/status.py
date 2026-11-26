"""Live status: renderers for the pipeline event stream (rich panel, plain lines, JSON lines)."""

from __future__ import annotations

import json
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, TextIO

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from .fmt import terminal_text, timestamp

ProgressMode = Literal["auto", "rich", "plain", "json"]
PLAIN_PROGRESS_INTERVAL = 5.0  # seconds between progress lines of one stage in plain mode

STATE_STYLE = {"pending": "dim", "running": "bold cyan", "done": "green", "cached": "blue", "skipped": "yellow", "failed": "bold red"}


@dataclass
class StageState:
    name: str
    state: str = "pending"
    done: float = 0.0
    total: float = 0.0
    unit: str = ""
    note: str | None = None
    started: float | None = None  # monotonic
    wall_seconds: float | None = None
    summary: str = ""
    reason: str | None = None
    log_path: str | None = None


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return terminal_text(value)
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


@dataclass
class RunState:
    """What the event stream says so far. `now` is injectable so tests control time."""

    now: Callable[[], float] = time.monotonic
    source: str = ""
    duration: float = 0.0
    started: float | None = None
    stages: dict[str, StageState] = field(default_factory=dict)
    status: str | None = None
    run_dir: str = ""
    outputs: list[str] = field(default_factory=list)
    error: str | None = None
    finished_wall: float | None = None

    def apply(self, ev: dict[str, Any]) -> None:
        ev = _clean(ev)  # what the rich panel shows: no control character from a file name or a message
        kind = ev["event"]
        if kind == "run_start":
            self.started = self.now()
            self.source, self.duration, self.run_dir = ev["source"], ev["duration"] or 0.0, ev["run_dir"]
            self.stages = {s["name"]: StageState(s["name"], reason=s["skipped_reason"]) for s in ev["stages"]}
            return
        if kind == "run_end":
            self.status, self.error = ev["status"], ev.get("error")
            self.outputs = ev.get("outputs", [])
            self.finished_wall = ev["wall_seconds"]
            return
        st = self.stages[ev["stage"]]
        if kind == "stage_start":
            st.state, st.started = "running", self.now()
        elif kind == "stage_progress":
            st.done, st.total, st.unit, st.note = ev["done"], ev["total"], ev["unit"], ev["note"]
        elif kind == "stage_end":
            st.state = ev["status"]
            st.wall_seconds = ev["wall_seconds"]
            st.summary = ev.get("summary", "")
            st.reason = ev.get("reason") or ev.get("error")
            st.log_path = ev.get("log_path")

    @property
    def elapsed(self) -> float:
        if self.finished_wall is not None:
            return self.finished_wall
        return 0.0 if self.started is None else self.now() - self.started


def format_amount(value: float, unit: str) -> str:
    return timestamp(value) if unit == "audio_s" else f"{value:g}"


def throughput(st: StageState, elapsed: float) -> str:
    if elapsed <= 0 or st.done <= 0:
        return ""
    if st.unit == "audio_s":
        return f"{st.done / elapsed:.0f}x realtime"
    return f"{st.done / elapsed:.1f} {st.unit}/s"


def eta_seconds(st: StageState, elapsed: float) -> float | None:
    if st.done <= 0 or st.total <= st.done or elapsed <= 0:
        return None
    return elapsed * (st.total - st.done) / st.done


class PlainRenderer:
    """One timestamped line per event; progress lines at most every PLAIN_PROGRESS_INTERVAL seconds per stage."""

    def __init__(self, out: TextIO | None = None) -> None:
        self.out = out or sys.stderr
        self._last_progress: dict[str, float] = {}

    def __call__(self, ev: dict[str, Any]) -> None:
        t, kind = ev["t"], ev["event"]
        if kind == "stage_progress":
            stage = ev["stage"]
            last = self._last_progress.get(stage)
            if last is not None and t - last < PLAIN_PROGRESS_INTERVAL:
                return
            self._last_progress[stage] = t
            self._line(t, stage, self._progress_text(ev))
        elif kind == "run_start":
            self._line(t, "run", f"start {ev['source']} ({timestamp(ev['duration'] or 0.0)})")
        elif kind == "stage_start":
            self._line(t, ev["stage"], "start")
        elif kind == "log":
            self._line(t, ev["stage"], ev["message"])
        elif kind == "stage_end":
            extra = ev.get("summary") or ev.get("reason") or ev.get("error") or ""
            if ev.get("log_path"):
                extra += f" (log: {ev['log_path']})"
            self._line(t, ev["stage"], f"{ev['status']} in {ev['wall_seconds']:.1f}s {extra}".rstrip())
        elif kind == "run_end":
            tail = f" -> {ev['run_dir']}" if ev["status"] == "ok" else f": {ev.get('error')}"
            self._line(t, "run", f"{ev['status']} in {ev['wall_seconds']:.1f}s{tail}")

    @staticmethod
    def _progress_text(ev: dict[str, Any]) -> str:
        pct = f"{100 * ev['done'] / ev['total']:.0f}%" if ev["total"] else "?"
        amount = f"{format_amount(ev['done'], ev['unit'])} / {format_amount(ev['total'], ev['unit'])}"
        return f"{pct} {amount} {ev['note'] or ''}".rstrip()

    def _line(self, t: float, who: str, text: str) -> None:
        # file names and messages come from the video and its tools: no control character reaches the terminal
        print(f"[{timestamp(t)}] {terminal_text(who):15} {terminal_text(text.replace(chr(10), " "))}", file=self.out, flush=True)


# DEL, C1 controls and the bidirectional and other invisible format characters of fmt.strip_invisible
_C1 = re.compile("[\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u206f\ufeff\ufff9-\ufffb\U000e0000-\U000e007f]")


class JsonRenderer:
    """One JSON object per event on stdout, and nothing else there."""

    def __init__(self, out: TextIO | None = None) -> None:
        self.out = out or sys.stdout

    def __call__(self, ev: dict[str, Any]) -> None:
        # json.dumps escapes C0 controls itself; C1 controls (U+0080 to U+009F) and invisible format characters it
        # leaves raw, so they are escaped too (the data stays the same: a JSON reader gets them back)
        line = _C1.sub(lambda m: json.dumps(m.group())[1:-1], json.dumps(ev, ensure_ascii=False))
        print(line, file=self.out, flush=True)


class RichRenderer:
    """A live panel while running, then a summary table and the output paths."""

    def __init__(self, console: Console | None = None, now: Callable[[], float] = time.monotonic) -> None:
        self.console = console or Console(stderr=True)
        self.state = RunState(now=now)
        self._live: Live | None = None

    def __call__(self, ev: dict[str, Any]) -> None:
        self.state.apply(ev)
        kind = ev["event"]
        if kind == "run_start":
            self._live = Live(self, console=self.console, refresh_per_second=4, transient=True)
            self._live.start()
        elif kind == "run_end":
            if self._live:
                self._live.stop()
            self.console.print(self.summary())
            self.console.print(self.final_lines(), soft_wrap=True)  # long paths must stay whole

    def __rich__(self) -> RenderableType:
        return self.panel()

    def panel(self) -> RenderableType:
        s = self.state
        table = Table.grid(padding=(0, 1))
        for name, st in s.stages.items():
            table.add_row(*self._stage_row(name, st))
        done = sum(st.state in ("done", "cached", "skipped") for st in s.stages.values())
        overall = Text(
            f"stages {done}/{len(s.stages)}   elapsed {timestamp(s.elapsed)}   {s.source.rsplit('/', 1)[-1]}   "
            f"duration {timestamp(s.duration)}",
            style="bold",
        )
        return Panel(Group(table, Text(""), overall), title="framescribe", border_style="cyan")

    def _stage_row(self, name: str, st: StageState) -> tuple[RenderableType, RenderableType, RenderableType]:
        label = Text(f"{st.state:8}", style=STATE_STYLE[st.state])
        if st.state == "running":
            elapsed = self.state.now() - (st.started or self.state.now())
            frac = st.done / st.total if st.total else 0.0
            eta = eta_seconds(st, elapsed)
            bar = Table.grid(padding=(0, 1))
            bar.add_row(
                ProgressBar(total=st.total or 1, completed=st.done, width=30),
                Text(f"{frac:4.0%}  {format_amount(st.done, st.unit)} / {format_amount(st.total, st.unit)}"),
            )
            timing = Text(
                f"elapsed {timestamp(elapsed)}   ETA {timestamp(eta) if eta is not None else '--:--:--'}   "
                f"{throughput(st, elapsed)}"
            )
            return (Text(f"{name:15}"), label, Group(bar, timing, Text(st.note or "", style="italic")))
        text = {
            "pending": "",
            "skipped": st.reason or "",
            "failed": st.reason or "",
        }.get(st.state, f"{st.wall_seconds or 0:.1f}s  {st.summary}")
        return (Text(f"{name:15}"), label, Text(text))

    def summary(self) -> RenderableType:
        table = Table(title="framescribe run", title_justify="left")
        for col in ("stage", "state", "wall", "details"):
            table.add_column(col)
        for name, st in self.state.stages.items():
            wall = "" if st.wall_seconds is None else f"{st.wall_seconds:.1f}s"
            # Text, never a plain string: a plain string is Rich markup, and a summary or reason holds recorded text
            # (speaker names, file names, messages) that could then open a hyperlink or blink
            table.add_row(Text(name), Text(st.state, style=STATE_STYLE[st.state]), Text(wall),
                          Text(st.summary or st.reason or ""))
        table.columns[-1].overflow = "fold"
        return table

    def final_lines(self) -> RenderableType:
        s = self.state
        head = f"{s.status} in {timestamp(s.elapsed)}"
        if s.status == "ok":
            return Group(Text(head, style="green"), *(self._path(p) for p in s.outputs))
        logs = [self._path(f"log: {st.log_path}") for st in s.stages.values() if st.log_path]
        return Group(Text(f"{head}: {s.error}", style="red"), *logs)

    @staticmethod
    def _path(text: str) -> Text:
        return Text(text)


def make_renderer(mode: ProgressMode, stderr_is_tty: bool | None = None) -> Callable[[dict[str, Any]], None]:
    if mode == "auto":
        tty = sys.stderr.isatty() if stderr_is_tty is None else stderr_is_tty
        mode = "rich" if tty else "plain"
    return {"rich": RichRenderer, "plain": PlainRenderer, "json": JsonRenderer}[mode]()
