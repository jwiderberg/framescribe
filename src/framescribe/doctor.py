"""What framescribe needs and whether this machine has it: `framescribe doctor`, and the checks `run` makes first.

Every check returns a `Check` and never raises. Nothing here uses the network. The small functions at the top
(`_machine`, `_which`, `_tool_version`, ...) are the only places that look at the machine, so tests replace them.
"""

from __future__ import annotations

import importlib
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass

from . import hub
from .config import ENV_WHISPERKIT_CLI, ENV_WHISPERKIT_MODEL, Config
from .stages import asr_whisperkit

OK, MISSING, WARNING = "ok", "missing", "warning"

LOW_MEMORY_FREE_PERCENT = 20
LOW_MEMORY_TOTAL_GB = 8.0
WORK_FREE_GB = 2.0  # a run of a long recording writes key frames and a WAV file of a few hundred MB


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # ok | missing | warning
    detail: str
    fix: str | None = None
    required: bool = True

    @property
    def blocking(self) -> bool:
        return self.required and self.status != OK and self.status != WARNING


# ---- the machine (replaced in tests) -------------------------------------------------------------------------------


def _machine() -> tuple[str, str]:
    return platform.system(), platform.machine()


def _python_version() -> tuple[int, int, int]:
    v = sys.version_info
    return v.major, v.minor, v.micro


def _which(name: str) -> str | None:
    return shutil.which(name)


def _tool_version(path: str) -> str | None:
    """The version word of `<tool> -version` (ffmpeg and ffprobe print 'ffmpeg version 7.1 ...'), or None."""
    try:
        out = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"version\s+(\S+)", out)
    return m.group(1) if m else None


def _model_files(repo: str):
    return hub.cached_model_files(repo)


def _model_hash_problem(repo: str, files) -> str | None:
    return hub.model_problem(repo, files)


def _hf_cache_dir():
    return hub.cache_dir()


def _free_disk_gb(path) -> float:
    return hub.free_gb(path)


def _memory() -> tuple[float, int | None] | None:
    """(total memory in GB, free percentage from `memory_pressure`), or None when neither can be read."""
    try:
        total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=10).stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    free = None
    try:
        out = subprocess.run(["memory_pressure"], capture_output=True, text=True, timeout=20).stdout
        m = re.search(r"free percentage:\s*(\d+)%", out)
        free = int(m.group(1)) if m else None
    except (OSError, subprocess.SubprocessError):
        pass
    return total / 2**30, free


def _vision_importable() -> str | None:
    """None when Apple Vision can be used from Python, else the reason."""
    try:
        importlib.import_module("Vision")
    except Exception as e:  # ImportError, or a pyobjc failure
        return f"{type(e).__name__}: {e}"
    return None


# ---- the checks ----------------------------------------------------------------------------------------------------


def check_platform() -> Check:
    system, machine = _machine()
    if system == "Darwin" and machine == "arm64":
        return Check("platform", OK, "macOS on Apple Silicon (arm64)")
    return Check(
        "platform", MISSING, f"{system} {machine}: framescribe needs macOS on Apple Silicon (MLX and Apple Vision)",
        "run it on a Mac with an M-series chip, in a terminal that is not running under Rosetta",
    )


def check_python() -> Check:
    major, minor, micro = _python_version()
    text = f"{major}.{minor}.{micro}"
    if (major, minor) == (3, 12):
        return Check("python", OK, text)
    return Check("python", MISSING, f"{text}: framescribe needs Python 3.12", "reinstall with `uv tool install --force --reinstall` (uv fetches Python 3.12)")


def check_ffmpeg_tool(tool: str) -> Check:
    path = _which(tool)
    if path is None:
        return Check(tool, MISSING, "not found on PATH", "brew install ffmpeg")
    version = _tool_version(path)
    if version is None:
        return Check(tool, WARNING, f"{path} does not report a version", "reinstall: brew reinstall ffmpeg")
    return Check(tool, OK, f"{version} ({path})")


def check_model(repo: str | None = None, verify: bool = False) -> Check:
    """The speech model at its pinned revision in the cache; with `verify`, also the SHA-256 of its files (a read of
    about 2.5 GB, no network): `doctor` and `setup` verify, `run` only looks for the files."""
    repo = repo or Config().parakeet_model
    files = _model_files(repo)
    pinned = f" at revision {hub.MODEL_REVISION[:12]}" if repo == hub.MODEL_REPO else ""
    if files is None:
        return Check(
            "speech model", MISSING,
            f"{repo}{pinned} is not in the Hugging Face cache ({_hf_cache_dir()}); the download is about "
            f"{hub.MODEL_SIZE_GB} GB",
            "framescribe setup",
        )
    if verify and (problem := _model_hash_problem(repo, files)) is not None:
        return Check("speech model", MISSING, f"the cached model is not the pinned one: {problem}",
                     "framescribe setup (it removes the wrong file and downloads it again)")
    checked = ", SHA-256 checked" if verify and repo == hub.MODEL_REPO else ""
    return Check("speech model", OK, f"{repo}{pinned} ({files['model.safetensors'].parent}{checked})")


def check_ocr() -> Check:
    reason = _vision_importable()
    if reason is None:
        return Check("apple vision ocr", OK, "importable (pyobjc Vision framework)")
    return Check(
        "apple vision ocr", MISSING, reason,
        "reinstall with `uv tool install --force --reinstall` (needs pyobjc-framework-Vision, macOS only)",
    )


def check_disk(model_present: bool) -> Check:
    path = _hf_cache_dir()
    free = _free_disk_gb(path)
    need = WORK_FREE_GB if model_present else hub.MIN_FREE_GB
    why = "to work on a recording" if model_present else "to download the model and work on a recording"
    if free >= need:
        return Check("disk space", OK, f"{free:.1f} GB free where the model cache is ({path})", required=False)
    return Check("disk space", WARNING, f"{free:.1f} GB free where the model cache is; about {need:g} GB are needed {why}",
                "free some disk space", required=False)


def check_memory() -> Check:
    mem = _memory()
    if mem is None:
        return Check("memory", WARNING, "cannot read the memory figures", None, required=False)
    total, free = mem
    text = f"{total:.0f} GB installed" + (f", {free}% free" if free is not None else "")
    if total < LOW_MEMORY_TOTAL_GB:
        return Check("memory", WARNING, f"{text}; 8 GB or more is recommended", "close other applications before a run", required=False)
    if free is not None and free < LOW_MEMORY_FREE_PERCENT:
        return Check("memory", WARNING, f"{text}; under {LOW_MEMORY_FREE_PERCENT}% is free right now", "close other applications before a run", required=False)
    return Check("memory", OK, text, required=False)


def check_whisperkit(cfg: Config | None = None) -> Check:
    cfg = (cfg or Config()).with_env_defaults()
    if not cfg.whisperkit_cli and not cfg.whisperkit_model:
        return Check("whisperkit", OK, "not configured (optional)", required=False)
    reason = asr_whisperkit.unresolved_reason(cfg)
    if reason is None:
        return Check("whisperkit", OK, f"{cfg.whisperkit_cli} with model {cfg.whisperkit_model}", required=False)
    return Check("whisperkit", WARNING, reason, f"set both {ENV_WHISPERKIT_CLI} and {ENV_WHISPERKIT_MODEL}, or unset them", required=False)


def run_checks(cfg: Config | None = None) -> list[Check]:
    model = check_model(cfg.parakeet_model if cfg else None, verify=True)
    return [
        check_platform(), check_python(), check_ffmpeg_tool("ffmpeg"), check_ffmpeg_tool("ffprobe"), model, check_ocr(),
        check_disk(model.status == OK), check_memory(), check_whisperkit(cfg),
    ]


def all_required_ok(checks: list[Check]) -> bool:
    return not any(c.blocking for c in checks)


def as_dict(checks: list[Check]) -> dict:
    return {"ok": all_required_ok(checks), "checks": [asdict(c) for c in checks]}


def format_text(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    lines = []
    for c in checks:
        line = f"{c.status:<8} {c.name:<{width}}  {c.detail}"
        if c.fix and c.status != OK:
            line += f"  -> fix: {c.fix}"
        lines.append(line)
    lines.append("")
    lines.append("everything required is present" if all_required_ok(checks) else "something required is missing: see the lines marked 'missing'")
    return "\n".join(lines)


def start_up_problem(cfg: Config) -> str | None:
    """What stops `run` before it starts, as one message, or None. Platform, ffmpeg and ffprobe always; the speech model
    only when this run decodes speech with parakeet."""
    needed = [check_platform(), check_ffmpeg_tool("ffmpeg"), check_ffmpeg_tool("ffprobe")]
    if cfg.transcript is None and cfg.asr in ("parakeet", "both", "auto"):
        needed.append(check_model(cfg.parakeet_model))
    return _blocking_message(needed)


def _blocking_message(checks: list[Check]) -> str | None:
    for c in checks:
        if c.blocking:
            fix = f" Fix: {c.fix}." if c.fix else ""
            return f"{c.name}: {c.detail}.{fix} (`framescribe doctor` shows everything)"
    return None


def uncached_model_problem(repo: str | None = None) -> str | None:
    """The offline guard's core, for anything that loads the speech model: put Hugging Face's client into offline mode
    for this process, then return the message to stop with when the model is not in the local cache (it says to run
    `framescribe setup`), or None when it is. Loading the model after a None result makes no network attempt, so that
    nothing but `setup` ever downloads. Used by the asr.parakeet stage (which raises it as a StageError, so a program
    that calls the pipeline as a library is guarded too) and, through `require_cached_model`, by the scripts."""
    hub.go_offline()
    return _blocking_message([check_model(repo)])


def model_load_path(repo: str | None = None) -> str:
    """What to give parakeet-mlx's `from_pretrained`: the folder of the pinned snapshot in the cache (given the
    repository name it would read the branch main instead), or the name when the model is not cached."""
    repo = repo or Config().parakeet_model
    files = _model_files(repo)
    return str(files["model.safetensors"].parent) if files else repo


def require_cached_model(repo: str | None = None) -> None:
    """The offline guard for the scripts: `uncached_model_problem`, turned into an exit with that message."""
    problem = uncached_model_problem(repo)
    if problem is not None:
        raise SystemExit(f"error: {problem}")
