# framescribe — design

## Goal

Turn a long screen recording with spoken commentary into evidence that a product manager, or an LLM working for one,
can act on: the full transcript, the key frames, the text on them, how the screen changed and, sparsely, where the
pointer was, all in time order in one Markdown document (`report.md`) with its frames.

framescribe extracts evidence; it does not interpret it. There is no local vision or language model in the
pipeline. The interpretation (what each frame shows, what the speaker referred to, call-outs such as bugs, confusion,
praise, questions and suggestions) is written by an external LLM tool that can read files and open images, that reads
`report.md`, opens the linked frames and follows `ANALYSIS_PROMPT.md`.

Priorities, in order: **accuracy, then speed**. Everything runs locally.

Usable as a component of a host application. That is why a transcript can be supplied from outside (`--transcript`),
why `--progress json` writes one event per line on stdout, why `timeline.json` is the data contract of the report, and
why capture-side events (cursor, clicks, window titles) could later override inferred ones.

## Measured basis

See `docs/MEASUREMENTS.md`, which records what was measured and on what kind of input. Decisions taken from it:

- ASR: `parakeet-mlx` (`mlx-community/parakeet-tdt-0.6b-v3`) in-process, on chunks the pipeline cuts itself (see
  "Speech" below). Unchunked uses 15 GB for 10 minutes of audio; never run unchunked. parakeet-mlx's own
  overlapping-chunk merge duplicated and garbled words at the seams; it is not used.
- Memory: MLX's buffer cache is unbounded by default and grew to 26 GB on 10 minutes of audio (active memory flat at
  1.2 GB); the stage bounds it with `Config.mlx_cache_limit_mb` (512 MB, not in the cache key) and records its peak MLX
  memory in `run.json` (`StageRecord.metrics`), not in the artifact.
- Chunk length and context were chosen by measuring candidate configurations on a full 1 h 53 min recording against
  three independent transcripts of the same audio (section "Chunk length and context for parakeet" in `docs/MEASUREMENTS.md`).
- The supported, default path is parakeet alone: two passes on different cut grids, plus the video's embedded captions
  when it has them, as the cross-check of the transcript.
- Second ASR, optional and not recommended by default: WhisperKit with Whisper large-v3 via an external
  `whisperkit-cli` (7x realtime in a debug build). The model used so far is Argmax's speed-optimised CoreML build of
  the full large-v3 (32 decoder layers), named `openai_whisper-large-v3_turbo`; WhisperKit's `_turbo` suffix does not
  mean OpenAI's distilled large-v3-turbo. When configured, it is one more cross-check source.
- No ASR gets product names right. The report keeps the spoken forms; the on-screen spelling is shown next to them and
  `ANALYSIS_PROMPT.md` tells the reader that it is authoritative.
- OCR: Apple Vision "accurate" on a 2x Lanczos upscale of the content crop.
- Change detection: software ffmpeg decode at 2 fps (VideoToolbox is slower), gray crop, changed-pixel fraction; open an
  event above 0.01, close when below 0.002 for 2 pairs, merge events within 1 s, keep a state only if it differs
  > 0.01 from the last kept one.
- A local vision-language model was measured (about 25 s per call, cannot read small text) and is not used.

## Layout

```
pyproject.toml            package "framescribe", Python 3.12, uv
src/framescribe/
  cli.py                  typer app: `framescribe run VIDEO ...`, `setup`, `doctor`, `--version`
  doctor.py               the checks behind `doctor` and the start-up checks of `run` (never uses the network)
  hub.py                  the speech model in the Hugging Face cache; the only code that downloads
  config.py               Config dataclass (every stage declares the fields it reads)
  cache.py                source fingerprint, stage keys, artifact store, write-if-changed
  pipeline.py             stage plan + runner, run.json
  models.py               pydantic models for every artifact (the contract)
  status.py               live status renderers (rich, plain, json)
  ffmpeg.py, proc.py      ffprobe/ffmpeg helpers, child processes and signals
  chunking.py             audio levels, pauses, wav I/O
  words.py                words from tokens, timing checks, sentences
  completeness.py         cross-check of the primary transcript against other sources, gap filling
  captions.py             SRT/VTT parsing
  layout.py               content rectangle detection
  framestream.py          ffmpeg frame pipes
  changedet.py            visual states, change kinds, scroll estimation
  cursor.py               pointer tracking
  ocr_engine.py, ocrdiff.py   Apple Vision, text diff between states
  steps.py, timeline.py   report steps and their events
  report_md.py, render.py report.md (+ the text of ANALYSIS_PROMPT.md), transcript.md
  stages/
    ingest.py
    asr_parakeet.py, asr_whisperkit.py, asr_import.py, asr.py
    visual.py
    ocr.py
    report.py
tests/
scripts/                  measurement scripts (asr_memory.py, asr_casing.py, asr_chunking.py); run in the main project
install.sh                installer (checks, `uv tool install`, `framescribe setup`, `framescribe doctor`)
app/                      the Mac app (see "App"): Swift package, build_app.sh, run_tests.sh, Support/
docs/                     DESIGN.md, MEASUREMENTS.md
```

## Run directory

`framescribe run VIDEO --out OUT` writes `OUT/<video-stem>/` (`--run-dir DIR`: exactly `DIR`, for a host that
chooses the folder):

```
work/<stage>.json         one artifact per stage (plus work/audio.wav, work/whisperkit-report/, and
                          work/transcript.gate-failed.json after --strict-completeness refused the transcript)
work/<stage>.log          messages a stage logged
frames/                   key frames as JPEG, named s0001_t00-25-03.jpg
report.md                 the deliverable: the evidence report
timeline.json             the report as data (TimelineArtifact)
ANALYSIS_PROMPT.md        fixed instructions for the LLM that writes the interpretation
transcript.md             the plain transcript
run.json                  per-stage wall time, cache hit/miss, versions, config, completeness figures
```

Output files are written only when their content changes, so a run that changes nothing leaves them (and their
modification times) alone. `run.json` is the exception: it records the run itself.

## Stage rules

- A stage is a function `(ctx) -> Artifact`. It reads earlier artifacts from `ctx`, never from another stage's
  internals.
- Cache key = sha256 of: source fingerprint (size, mtime, sha256 of first and last 4 MB), stage name, stage `VERSION`
  constant, the config fields the stage declares it uses, the cache keys of the stages it depends on, and the content
  digests of their artifacts and side files (volatile fields such as wall times left out). Hit → load JSON, do not
  recompute. `--force STAGE` invalidates that stage and everything downstream. A stage whose output changes gets a new
  `VERSION`.
- Every artifact is a pydantic model with `schema_version`, raised when the model changes. All times are float seconds
  from the start of the source video.
- Stages fail loudly. No silent fallback from a requested backend to another one; an optional backend that is absent
  is skipped only when the config says "auto", and `run.json` records that it was skipped and why.
- Stages never print; they report through `ctx.progress` and `ctx.log`.

Stages, in order: `ingest` → `asr.parakeet` (→ `asr.whisperkit`, optional) → `asr` → `visual` → `ocr` → `report`.
With `--transcript FILE` the two backend stages are left out.

## Packaging

The repo is self-contained: clone, one install command, run.

- Platform: macOS on Apple Silicon (MLX and Apple Vision). `run` and `setup` stop with a clear message on anything
  else; `doctor` reports it.
- Install: `install.sh` (also usable as `curl ... | bash`), or `uv tool install --python 3.12 .` in a clone. The
  lockfile is committed and dependencies are pinned for development (`uv sync`). The only system prerequisite is
  `ffmpeg`/`ffprobe`.
- `install.sh`: POSIX-careful bash with `set -euo pipefail`, its whole body in a `main` function called on the last
  line (a truncated download runs nothing). It checks macOS and arm64, `ffmpeg` and `ffprobe`, and `uv` (printing the
  command to install what is missing and stopping; it installs them only with `--install-ffmpeg` or `--install-uv`),
  runs `uv tool install --force --reinstall --python 3.12` on the clone it is in (or on the Git URL, or on
  `FRAMESCRIBE_SOURCE`), then `framescribe setup` (not with `--no-model`) and `framescribe doctor`, and prints
  the next step. After `--no-model`, a doctor failure whose only failed required check is the speech model (read from
  `doctor --json`) is expected and the script exits 0 with a line pointing at `framescribe setup`; any other doctor
  failure exits 1. The source is the clone that holds the script file, otherwise the Git URL: a script piped into `bash`
  never installs from the directory the user stands in. `--dry-run` prints every action without doing it. It never uses `sudo`, never edits a shell profile
  (it prints the `PATH` line when the uv tool directory is not on `PATH`) and sends no telemetry.
- `framescribe setup`: downloads the speech model (`config.json` and `model.safetensors` of
  `mlx-community/parakeet-tdt-0.6b-v3`, about 2.5 GB) into the Hugging Face cache with Hugging Face's own progress
  bars, after checking that at least 6 GB are free where the cache is (otherwise it stops and says how much there is).
  `--check` only reports. It is the only code path in the project that opens a network connection (`hub.download_model`).
  Everything that loads the model goes through one guard first, `doctor.uncached_model_problem`: it puts Hugging Face's
  client into offline mode (`HF_HUB_OFFLINE=1`, for the whole process) and returns the "run `framescribe setup`"
  message when the model is not in the local cache. The `asr.parakeet` stage calls it before it imports the model
  loader and raises the message as a `StageError`, so a program that uses the pipeline as a library cannot trigger a
  download either (it just finds the process in offline mode afterwards). The scripts that load the model
  (`scripts/asr_memory.py`, `scripts/asr_chunking.py`) call `doctor.require_cached_model`, the same guard turned into an
  exit; a test fails for any script that calls a loader without it.
- `framescribe doctor`: one line per check (`ok`, `missing` or `warning`, and how to fix it): platform, Python,
  `ffmpeg` and `ffprobe` with versions, the speech model in the local cache, Apple Vision, free disk space, free
  memory, and optional WhisperKit ("not configured (optional)" when unset). Exit status 0 when everything required
  (platform, Python 3.12, ffmpeg, ffprobe, model, Apple Vision) is present, 1 otherwise; disk, memory and WhisperKit are
  warnings. `--json` prints the same as JSON. No network.
- `framescribe run` checks first, before it touches the video: platform, `ffmpeg`, `ffprobe` and, when it will
  decode speech with parakeet (not with `--transcript` or `--asr whisperkit`), the model. A missing model says to run
  `framescribe setup`; `run` never starts a download, and it puts Hugging Face's client into offline mode
  (`HF_HUB_OFFLINE=1`) for its process, as the `asr.parakeet` stage does for any process that runs it. Failure exits with
  status 2.
- `python -m framescribe` is the same command line as the `framescribe` script (`__main__.py`), for a host that
  runs the package with its own interpreter.
- WhisperKit stays an optional external backend for machines that have its CLI and a CoreML model; nothing needs it.
- No machine-specific paths anywhere in `src/`. Configuration comes from CLI options and environment variables.
- Tests run without models or network by default; the end-to-end test that needs the parakeet model is marked slow.
  `install.sh` is tested against stubbed `uv`, `ffmpeg` and `uname` on a PATH that holds nothing else.
- Not built: a configuration file.

## Live status

The CLI shows a full live status for the whole run; a long run must never look hung.

- Stages report through one API: `ctx.progress(done, total, unit, note=None)` and `ctx.log(message)`. The runner turns
  these into events: `run_start`, `stage_start`, `stage_progress`, `log`, `stage_end` (status `done | cached | skipped
  | failed`, wall seconds, summary line), `run_end`.
- `--progress auto|rich|plain|json` (default `auto`: `rich` on a TTY, `plain` otherwise).
  - `rich`: a live panel listing every stage with its state, a progress bar with percent, elapsed and ETA for the
    running stage, its throughput where meaningful, the current note (e.g. "pass 2, chunk 14/225"), and an overall line.
    On finish it leaves a summary table (stage, state, wall time, key numbers) and the output paths.
  - `plain`: one timestamped line per event, progress lines throttled to every 5 s.
  - `json`: one JSON object per event on stdout (for a parent process); nothing else is written to stdout in this mode.
    `run_end` also carries `completeness` (the completeness figures as in `run.json`, plus `summary`, the line the
    `asr` stage shows; `above_limit`, `limit` and `warning` say whether more than `--max-filled-share` was filled in;
    for a transcript `--strict-completeness` refused, its figures; `null` without a cross-checked transcript) and
    `warnings` (layout detection, the speech backends' chunk warnings, the above-limit warning), so a host shows them
    without reading `report.md`. Both were added later: a host must accept a `run_end` without them.
  - A failed `stage_end` and a failed or interrupted `run_end` carry `error_kind`, the machine-readable reason a host
    can act on without reading `error`: `no_audio` (the video has no audio stream), `incomplete_transcript`
    (`--strict-completeness` refused the transcript) or `null` (no particular one). Added later: a host must accept
    events without it, and a kind it does not know. It comes from `StageError.kind` (`errors.py`).
- `setup --progress json` writes the model download as events in the same way: `setup_start` (repo, `size_gb`,
  `cache_dir`, `free_gb`, `files`), `setup_progress` (`file`, `done` and `total` bytes, at most every 0.5 s per file,
  from a tqdm class handed to Hugging Face's download that draws nothing), and one `setup_end` (`status` `ok`,
  `present`, `missing` for `--check`, `failed` or `interrupted`, with `detail` and `error`). The messages go to stderr
  then. The default `--progress auto` keeps the messages and Hugging Face's own progress bars.
- Every stage reports real progress: parakeet per chunk of each pass; WhisperKit from the CLI's printed percentage (an
  estimate, labelled as such, until it prints one); the completeness check per source; frame decoding, key frames and
  OCR per item; the report per step.
- The `asr` stage's summary line carries the completeness figures (see below).
- Errors show the stage, the message and the path of the log file; Ctrl-C, SIGTERM and SIGHUP stop cleanly, kill child
  processes, and leave finished stages cached.

## App

`app/` holds a native Mac app: a SwiftUI window that runs this command line tool and shows its event stream. It adds no
pipeline logic of its own.

```
SwiftUI window (Framescribe)  --posix_spawn-->  Contents/Resources/runtime/bin/python3.12
  queue, settings, health                            -m framescribe_app_launcher run VIDEO --out DIR
                                                       --run-dir DIR/<folder> --progress json
  FramescribeCore (library)   <--stdout-------  one JSON event per line (this document, "Live status")
                                 <--stderr-------  messages, appended to DIR/<folder>/framescribe-app.log
```

- Swift package (`app/Package.swift`, macOS 14 for the Swift code, arm64, no dependencies; the bundle as a whole needs
  macOS 15, see below): `FramescribeCore` holds everything that
  needs no window (event decoding, the per-run state the events build, the job state machine, the queue, the process
  launcher, tool location, the child's environment, output paths, settings, `doctor --json` and `setup` events);
  `Framescribe` is the SwiftUI app; `FramescribeE2E` is a command line driver that runs a built app's runtime on
  a video through the same library code; the tests (Swift Testing, `app/run_tests.sh`) use event streams recorded from
  the embedded runtime on a 3-minute excerpt, with neutral paths. It builds with the Command Line Tools alone: the
  bundle is assembled by `app/build_app.sh`, not by Xcode.
- Open-document events (a drop on the Dock or Finder icon, `open -a`) reach an `NSApplicationDelegate`
  (`application(_:open:)`); a drop on the window uses SwiftUI's drop destination; File ▸ Open uses an open panel. All
  three go through one function that rejects what is not a video file (by extension, or a type that conforms to
  `public.movie`) with a message in the window.
- One job at a time, in the order added, and only while the health checks pass (`doctor --json`, run at launch and on
  "Check Again"); the command and output folder are taken when a job starts, so changed settings apply to waiting jobs.
  A job is done only when the process exited 0 and the stream ended with `run_end` `ok`. A video that is gone when its
  turn comes fails without anything being created. The app never passes `--strict-completeness`: a done job whose
  `run_end` says `above_limit` shows the share filled in as a warning line above its buttons. A failure with
  `error_kind` `no_audio` is shown in plain words of the app's own; a child that dies by a signal after Stop is
  "Stopped", without Stop it is a failure that names the signal. The app reads no message text to decide any of this.
- Window: the single window closes like any other; while a job runs or the model downloads, closing it does not quit
  the app (`applicationShouldTerminateAfterLastWindowClosed` is false then), the work goes on, and a Dock icon click
  (`applicationShouldHandleReopen`) opens it again with the live state. With nothing running, closing it quits. Only
  Quit asks, so no sequence of close and cancel can loop (`WindowPolicy` in the core library).
- Child processes: `posix_spawn` with a new process group, default signal dispositions and an empty signal mask (an
  app may run with SIGINT ignored, which a child would inherit), stdin from `/dev/null`, and no inherited file
  descriptor except stdout, stderr and the lifeline. Stop sends SIGINT to the group (framescribe stops cleanly, exit
  130, finished stages cached), SIGTERM after 20 s, SIGKILL 10 s later; when the group leader has exited, whatever is
  left of its group is killed. Quitting with a running job asks first, then stops it the same way and quits when it has
  exited.
- Lifeline: the app passes the read end of a pipe as file descriptor 3 (`FRAMESCRIBE_APP_LIFELINE_FD`) and keeps the
  write end. `framescribe_app_launcher` (`app/Support/`, installed next to the package in the runtime) is
  `python -m framescribe` plus a thread that waits for end-of-file on it: when the app is gone without stopping the
  child (a crash, Force Quit), it points stdout and stderr at `/dev/null` and sends itself SIGTERM, so the run still
  stops cleanly and writes `run.json`.
- Environment of the child, built from scratch: `HOME`, `USER`, `TMPDIR` and the locale from the app; `PATH` made of the
  login shell's `PATH` (read once with `$SHELL -l -c env` between markers, because apps started from the Finder do not
  get it), `/opt/homebrew/bin`, `/usr/local/bin` and the system directories; the Hugging Face cache variables, proxies
  and the WhisperKit variables from the login shell; `PYTHONNOUSERSITE=1`, `PYTHONSAFEPATH=1`,
  `PYTHONDONTWRITEBYTECODE=1`. Nothing else (no `PYTHONPATH`, no `DYLD_*`).
- Output: `~/Documents/framescribe/` unless Settings names another folder (`UserDefaults`, the app's own domain
  `io.github.jwiderberg.framescribe`; the `allowIncomplete` key of earlier versions is ignored). The run directory
  is `<folder>/<video stem>/`, unless that folder's `run.json` records a different source file (compared by resolved
  path); then `<video stem> 2`, `<video stem> 3`, … (`OutputLocation.chooseRunDirectory`), passed with `--run-dir`. A
  second run of the same file gets the same folder and reuses the cache. The app's log of a job is appended to, after a
  dated separator line.
- The speech model is downloaded only by the button, which runs `setup --progress json`.

Bundle layout (`app/build/Framescribe.app`, about 640 MB, ad-hoc signed):

```
Contents/Info.plist            id io.github.jwiderberg.framescribe, version from pyproject.toml, macOS 15.0, arm64,
                               document types for movies (public.movie, mp4, mov, m4v, mkv, webm)
Contents/MacOS/Framescribe  the Swift executable (stripped)
Contents/Resources/AppIcon.icns  drawn by app/Support/make_icon.swift (CoreGraphics), converted with sips and iconutil
Contents/Resources/runtime/    python-build-standalone CPython 3.12 (from `uv python install`), statically linked
  bin/python3.12               the only executable kept
  lib/python3.12/              the standard library, and site-packages with framescribe (wheel, non-editable),
                               its dependencies from uv.lock (`uv export --locked`, `uv pip install --require-hashes`)
                               and framescribe_app_launcher.py
```

Deployment target: the wheels are installed for macOS 15.0 (`--min-macos`), not for the build machine:
`MACOSX_DEPLOYMENT_TARGET=15.0 uv pip install --python-platform aarch64-apple-darwin --only-binary :all:` makes uv
take the newest wheel tagged for that version or older (without `--python-platform` uv takes the host's tags, which
gave `mlx` and `mlx-metal` wheels for macOS 26). 14.0 is not possible: `opencv-python-headless` 5.1.0.95 has wheels for
macOS 15.0 and later only (every other locked package has one for 14.0). After assembling, the script reads the
minimum macOS of every Mach-O file in the bundle (`vtool -show-build`: `minos` of `LC_BUILD_VERSION`, or the version of
`LC_VERSION_MIN_MACOSX`), fails if any is above the target, and writes the target into `LSMinimumSystemVersion`.

`build_app.sh` strips the interpreter's headers, static library, Tcl/Tk, `test`, `idlelib`, `ensurepip`, `pip`, the
packages' test directories, bundled headers and static libraries (`*.pyi` stubs stay: librosa reads its own at import
time), removes the embedding library (`libpython3.12.dylib`; uv had written its install path into it) and writes the
standalone build's `/install` prefix back into the build-time configuration, so the bundle names no path of the build
machine (the script checks that). It compiles every module to `.pyc` in unchecked-hash mode, so Python never rewrites
one and the signed bundle never changes. It signs every Mach-O file of the runtime, then the Python executable, then the
app (counting codesign's "replacing existing signature" lines instead of printing each), and checks with
`codesign --verify --deep --strict`. The runtime is rebuilt only when `uv.lock`, `pyproject.toml`, `README.md`,
`LICENSE`, the sources, the launcher, the script or the deployment target change. `--out DIR` builds into another
folder than `app/build`. With `--sign IDENTITY` it signs with the hardened runtime and a
timestamp (the Python executable with `app/Support/runtime.entitlements`: unsigned executable memory for libffi
closures, no library validation), and `--notarize PROFILE` submits it with `xcrun notarytool` and staples the ticket.

Not sandboxed: the app starts a bundled interpreter that reads videos from anywhere and writes to a folder the user
chooses, and the tool it runs spawns `ffmpeg`; a sandboxed app would need security-scoped bookmarks for every input and
output and could not hand them to the child. It needs no network entitlement (only `setup`, run on request, downloads),
collects nothing and does not update itself.

## Speech

### Ingest

`IngestArtifact`: source path, duration, video stream facts (w, h, fps, codec), audio facts, `audio_wav` path (16 kHz
mono PCM), `embedded_captions: [Caption{start, end, speaker|None, text}]`, `layout: Layout{frame_w, frame_h,
content_rect, other_rects, method, confidence, warning}`.

Layout detection does not hard-code a video. It samples ~40 frames spread over the file, computes the per-pixel "ever
non-black" mask, finds connected rectangular regions and takes the largest as `content_rect`; remaining regions go to
`other_rects` (webcam tiles and the like). A full-frame video yields the whole frame. `--content-rect x,y,w,h` overrides.

### Backends

- `parakeet` (`asr.parakeet`) — in-process. The audio is cut into chunks at pauses: about 30 s (`chunk_seconds`), at the
  centre of the longest pause within 5 s of the target, else within 10 s, never longer than 40 s
  (`chunk_max_seconds`); where there is no pause at all, at the widest gap between two decoded words, with a warning.
  Each chunk is decoded with 1 s of audio before its cut and 2 s after its end, without digital silence; the words
  found in that context belong to the neighbouring chunks and are dropped (every word belongs to the chunk its start
  falls in). A word that the chunks on both sides of a cut decoded (the first one in its context after the cut, timed
  just before it) is kept once, from the chunk after the cut. Words are built from tokens (a token that starts with a
  space starts a new word); sentences come from the model's own punctuation across chunk boundaries, or a pause over
  2 s.
  - Retries. A chunk of 10 or more words that comes back without punctuation ("bare") is decoded again with other
    context (none, 2 s or 0.5 s before the cut, then 0.5 s or 2 s of leading silence, then 8 s on both sides) until
    the best decode so far is punctuated. Of all decodes of a chunk the one with the most words is kept, and among
    those with as many words a punctuated one, so a retry never replaces a decode that has more words. When the kept
    decode is bare, a warning names the chunk.
  - Empty chunks. A chunk that comes back without any word although at least 1 s of its audio is above the pause
    threshold is decoded once more with its context reaching back to the longest pause 5 to 15 s before it (at the
    start of the audio: up to 15 s more after it), and is a warning if it is still empty. Measured on a 3 min
    excerpt: the model decoded the same samples to 31 words in a fresh process and to none after one particular
    earlier chunk had been decoded, so the window is changed rather than decoded again.
  - Casing at cuts. The context does not stop the model from starting a sentence at a cut: on the 1 h 53 min
    recording the first word after a cut was capitalised without a sentence stop before it at 24% of the cuts, against
    about 2% for words elsewhere. So the first word after each cut takes its casing from the other pass when that pass
    has the same word (normalised, not a filler) starting within 0.5 s, and is left as it is otherwise. Only the case
    of its letters changes, never the word; both passes are treated so (`scripts/asr_casing.py` measures the rate). What
    remains is under "Known limitations".
- Second pass, always on: with the model loaded once, the audio is decoded again on a second cut grid with one cut near
  the middle of every first-pass chunk (the longest pause in its middle half, never more than 40 s apart). It is stored
  in the parakeet artifact as `second_pass` and is not merged: it is the in-process cross-check that a plain screen
  recording without captions or WhisperKit still has. It costs about one more parakeet pass.
- `whisperkit` (`asr.whisperkit`), optional — subprocess `whisperkit-cli`, its JSON report parsed into words and sentences. The
  raw report is kept and re-parsed when only the parsing changed. A word up to 0.5 s past the end of the audio is moved
  to the end; one starting later is dropped and logged; more than 5 such words fail the stage.
- `import` — `--transcript FILE` (SRT, VTT, or this project's transcript JSON). Skips ASR. An imported transcript is
  taken as given and not cross-checked (its completeness is None, and the summary, `transcript.md` and the report say
  so).
- `--asr parakeet|whisperkit|both|auto` — default `auto`: parakeet as primary, plus whisperkit only when both its CLI
  and model are configured. Without that configuration `auto` is parakeet alone, the supported path.

`AsrArtifact` (one per backend): `backend`, `model`, `language`, `words: [Word{text, start, end, conf|None}]`,
`segments` (sentences with word index ranges), `cuts` (time, `pause | no_pause`, level, pause length), `warnings`,
`second_pass: {name, words, cuts} | None`, `audio_seconds`, `wall_seconds`, `pass_seconds`.

### Completeness

Stage `asr` builds the final transcript from the primary backend (parakeet, or WhisperKit with `--asr whisperkit`) and
cross-checks it against every other source of the same speech: the second parakeet pass, the other backend, and the
embedded captions (`completeness.py`, pure functions).

- Words are compared as normalised tokens: case and punctuation dropped, common contractions and colloquial forms
  expanded ("gonna" → "going to", "it's" → "it is"), numbers spelled out, filler sounds removed.
- The token sequences are aligned with an edit-distance alignment that may only pair tokens within 3 s of each other
  (`TIME_TOLERANCE`; for captions, which only have cue-level times, within the cue's span plus 5 s,
  `CUE_TOLERANCE`). Equal tokens paired so are anchors. Ordinals are spelled out ("3rd" and "third" compare equal).
- A run of another source's tokens without an anchor is a gap only where the primary has no words at that time: for a
  word-timed source after shifting it by the offset of the anchors around it, and keeping the words no primary word
  covers; for captions only where the primary has no words between the two anchors within the cue (with 1 s margin).
  The primary must also leave room there: at least 0.3 s of silence, and at least half the phrase's own duration
  (0.15 s per word for captions). A word that one engine heard where the other leaves no room is a wording
  difference, not a gap.
- Gaps of different sources in the same primary silence are one gap when they lie within 1 s of each other, or when
  they are the same phrase (60% of the shorter one's tokens in the other) anywhere in that silence. A gap is
  `confirmed` when two or more sources have it and every word to be filled (normalised) is in at least two sources
  within the time tolerance of the fill (3 s; for captions 5 s beyond the cue); otherwise it is `unconfirmed`.
- A gap is `rejected`, not filled, and listed with its reason code when
  - `repeats-adjacent`: every one of its words is one of the 3 primary words on either side of it, a word counting
    as the same when equal after normalisation or, on words of 4+ letters, one letter apart: a word heard again in
    the pause next to itself, often in another spelling;
  - `fragment`: one source has it and it is only single letters or sounds that are no word (no vowel, or one letter
    repeated; a word in capitals is an abbreviation and counts as a word), so "Yes.", "No." and "Okay." are filled;
  - `no-speech`: only WhisperKit has it and less than 10% (or 0.15 s) of the span's 10 ms frames are above the pause
    threshold: Whisper invents text in silence.
- Every confirmed and unconfirmed gap is filled: its words are inserted between the primary words around it, timed
  inside that silence, as `TranscriptWord{..., source, filled: true, confirmed}`. The text is taken from the source
  with the most words in the gap (ties: second pass, WhisperKit, captions). Primary words are never altered or
  removed, and word order stays monotonic in time.
- Filled text is marked everywhere it appears: `[+ ... +]` for confirmed, `[? ... ?]` for unconfirmed, in
  `transcript.md`, in the segment text of `transcript.json` and `timeline.json`, and in `report.md`, whose header
  explains the marks once (as does `ANALYSIS_PROMPT.md`).
- Limit: when filled words exceed `max_filled_share` (default 3%, `--max-filled-share`) of the transcript, the stage
  still succeeds (filling in is the remedy; refusing the result afterwards leaves the user nothing) and records
  `above_limit`, `limit` and `warning` in the figures: how many words and what share were filled in, that this is more
  than usual, and that the marked words should be checked against the recording. With `--strict-completeness` the
  stage fails instead, with the figure, and writes the refused transcript, with its figures and gaps, to
  `work/transcript.gate-failed.json`, which the message names; it is not the stage's artifact, so the next run
  computes the stage again (and a later run without the flag succeeds from the cached speech recognition). `run.json`
  keeps the figures. `--allow-incomplete`, which used to keep the result, is accepted and has no effect.
- The figures (words from the primary, filled words per source, confirmed / unconfirmed / rejected counts and the
  rejected ones per reason, which sources were compared and which were unavailable) are in `transcript.json`,
  `run.json`, the `asr` status line and the report header, which name the sources compared. When only the second pass
  was available, the summary says so rather than implying full coverage.
- A transcript above the limit says so first, in bold, in the report header's Completeness row and at the top of
  `transcript.md` ("ABOVE THE USUAL LEVEL: …"), in the `asr` status line and summary ("ABOVE THE USUAL 3.0%: check the
  filled-in words") and in `run_end`'s warnings.

`TranscriptArtifact`: `primary`, `segments: [TranscriptSegment{id, start, end, text, speaker|None, words}]`,
`alternates` (other backends' sentences), `sources`, `completeness` (None for an imported transcript).

Speaker: if embedded captions carry speaker names, each segment gets the speaker with the most time overlap, or the
nearest named caption within 4 s. Otherwise None.

## Visual and text

`VisualArtifact`: contiguous states (start, end, key frame, `settled`, change kind `initial | page | partial | scroll |
minor` with confidence, changed fraction and rectangle, scroll shift), pointer samples, and the safeguards that acted
(`cap`).

- A state starts when its content started to appear: the first sample from which on every frame up to the key frame
  is clearly closer to it than the previous state is. A fade counts from its start; an animation or camera feed that
  never settled before a real change does not move the change back to the animation's start.
- Safeguards against footage that changes all the time: at least 1.5 s between states, at most 20 states in any
  minute (a sliding window, so the cap acts only in the minute that has too many), and a change that does not settle
  within 10 s is cut off at its calmest frame. A change that was held back and stays on screen becomes a state later,
  stamped with the time it appeared.

`OcrArtifact`: per state the text lines with rectangles (key-frame pixels) and confidence, and the lines added and
removed against the previous state, matched on text and position.

## Report

`report.md` is plain Markdown, rendered from `TimelineArtifact` (also written as `timeline.json`): a facts table
(including the completeness figures), "How to read this document", the on-screen vocabulary, a steps index, one section
per step and "Coverage and caveats".

- Steps start at confident page changes, are at least 30 s and at most 240 s long (split at a screen change that meets
  a speech pause, else at the sentence nearest the middle). Each has a main key frame, its on-screen text, up to three
  other frames and a timeline.
- Timeline events: SAID (verbatim transcript, consecutive sentences merged until a screen change, a speaker change or
  30 s; every segment exactly once), SCREEN (each state with its frame link and the lines that appeared and went), and
  sparse POINTER hints.
- A minor change that brings at least 2 new non-chrome lines where the pixels changed is a SCREEN entry of its own.
  Other runs of minor changes collapse into one line that links each of their frames, so every key frame is linked
  from `report.md` at least once.
- Deterministic; no model is called.
- "How to read this document" names only the cross-check sources that were compared in the run (second decode, WhisperKit,
  captions), and explains `[+ +]` only when at least two sources were compared. When the speech stage warned about
  chunks kept without punctuation or chunks that stayed empty, the header has one row with the two counts (read from
  the `asr.parakeet` artifact, which is unchanged; the spans stay in the stage log).

## Known limitations

- Casing at cuts. After the arbitration, a capital without a sentence stop before it remains at about 12-13% of the
  cuts on the measured recording (24% before it), against about 2% for words elsewhere. It remains mostly where the model left out
  a full stop before the cut or where the other pass has no matching word at that time, so the word keeps its own
  casing. Only a cut word that has a counterpart is guaranteed the other pass's casing.
- Single words are often not filled and not recorded. A word that another source heard is filled only where the
  primary leaves a silence of at least 0.3 s (`MIN_GAP_SILENCE`) and at least half the heard words' own duration (0.15 s
  per word for captions); there is no separate minimum number of words. A single word squeezed between two primary
  words almost never has that room, so such a candidate is dropped without an entry in `gaps` or in the rejected
  counts. On the measured recording, with the second pass and the captions as sources, 74 candidates were dropped for lack of room
  (67 of them single words).
- Two more filters drop candidates without a record. A candidate that has room is still dropped silently when it only
  repeats neighbouring primary words, or when it comes from a caption cue whose span the primary already covers. On the
  measured recording that is 48 candidates (42 and 6; 47 of them single words). 54 single-word candidates passed all three and went
  through the normal checks.
- A fill can be partial, and a short phrase can be missed entirely. When the same short word occurs on both sides of a
  gap, the alignment anchors on the repeat; only the part of the other source's phrase between the anchors is seen as
  the gap, and if the anchor leaves no silence for it the whole candidate is dropped for lack of room, unrecorded.
