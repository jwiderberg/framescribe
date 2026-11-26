# framescribe

Turns a long screen recording with spoken commentary into a Markdown evidence report: the full transcript, key frames, the text that was on screen and a timeline of what was said and what changed. You give the report to an LLM tool, and that tool writes the walkthrough or analysis. Runs locally on a Mac with Apple Silicon. No data leaves the machine.

Built for the use case of narrated recordings of someone using or testing software. It should also suit other screen-based video with commentary, like demos, slide talks or lectures. The report it produces has no model-written interpretation in it, only evidence in time order with links to the frames. Tested on a Mac (M3 Max, 36 GB), in English only.

## What you get

`framescribe run recording.mp4 --out out` writes `out/recording/`:

```
out/recording/
  report.md                the evidence report, links into frames/
  frames/                  one key frame (JPEG) per distinct screen state
  timeline.json            the same evidence as data (steps and their events, the video by file name)
  ANALYSIS_PROMPT.md       instructions for the LLM that writes the walkthrough from report.md
  transcript.md            the plain transcript with timestamps
  run.json                 status, wall time and cache key per stage, plus the configuration
  work/                    one JSON artifact per stage, logs, the extracted audio
```

`report.md` starts with a facts table and a "How to read this document" section. Then the on-screen vocabulary, an index of steps and one section per step. One step looks like this (the content is made up, the layout is real):

```markdown
## Step 2 · 00:04:10–00:06:45 · Overdue loans

![Step 2 main frame, state s0007 at 00:04:12](frames/s0007_t00-04-12.jpg)

### On screen

- Shelves | Borrowers | Loans
- Overdue loans | Print
- Sorted by due date

### Timeline

- **SCREEN [00:04:10]** new page ([s0007](frames/s0007_t00-04-12.jpg)). appeared: "Overdue loans"
- **SAID [00:04:14]** Right, here are the overdue books. The oldest loan should be `[+` listed first `+]` but the list starts with the newest.
- **POINTER [00:04:21]** pointer near "Due date"
- **SAID [00:04:22]** There is an arrow here, and it can `[?` flip the order `?]` but it resets on reload.
- **SCREEN [00:04:31]** part of the screen changed ([s0008](frames/s0008_t00-04-31.jpg)). appeared: "Sorted by title"
```

- `SAID` is what the speaker said, verbatim.
- `SCREEN` is a change of the picture, with a link to the frame and the text that appeared.
- `POINTER` is a sparse hint of where the mouse pointer was while a pointing word ("this", "here", "click") was spoken.
- `[+` ... `+]` marks words the main speech recognition missed and two or more other sources agree on. `[?` ... `?]` marks filled-in words with less support, for example words only one other source heard. The marks are code spans (in backticks). Recorded text cannot make a code span, so brackets in it that look like a mark stay plain text. See [Accuracy and completeness](#accuracy-and-completeness).

## Requirements

- A Mac with Apple Silicon (M1 or later). The speech model runs on MLX and the text recognition uses Apple Vision, so Intel Macs and other systems are not supported.
- macOS 15 (Sequoia) or later for the Mac app. The command line tool does not declare a minimum macOS version. Which older versions it works on is unchecked.
- `ffmpeg` and `ffprobe` (`brew install ffmpeg`).
- Videos in MP4, MOV, M4V, MKV or WebM files, up to 12 hours long and up to 8192 pixels on a side. Other containers (AVI, MPEG-TS and so on) are refused with a message. Convert them first, for example with `ffmpeg -i in.avi -c copy out.mkv`.
- [uv](https://docs.astral.sh/uv/), for the command line install and for building the app. It installs Python 3.12 itself if needed.
- About 6 GB of free disk space for the first install. The speech model is about 2.5 GB, downloaded once.

## Mac app

Drop a video on the window or the app icon. It runs the whole pipeline, shows each stage's progress and ends with buttons to open the report, show the folder in the Finder or copy its path. It carries its own Python, so apart from `ffmpeg` it needs nothing from the command line install.

### Download

Releases are on the [releases page](https://github.com/jwiderberg/framescribe/releases). Each one is a zip of `Framescribe.app`, built from the tagged commit by this repository's release workflow, signed with a Developer ID and notarized by Apple. If the page lists no release yet, build the app from source (below).

1. Download `Framescribe-<version>.zip` from the latest release and unzip it (double-click it in the Finder).
2. Move `Framescribe.app` to `/Applications`.
3. Install `ffmpeg` if it is missing: `brew install ffmpeg`.
4. Open the app. The first launch runs the same checks as [doctor](#doctor) and says what is missing. If the speech model is missing, a button downloads it (about 2.5 GB, once). Then drop a video on the window.

The release notes say how to check a download: its SHA-256 (`shasum -a 256 -c`), that this repository's workflow built it (`gh attestation verify`) and that Apple notarized it (`spctl --assess`). [docs/RELEASING.md](docs/RELEASING.md) describes how a release is made.

### Build from source

```
git clone https://github.com/jwiderberg/framescribe.git
cd framescribe
app/build_app.sh                # writes app/build/Framescribe.app (about 640 MB)
open app/build/Framescribe.app
```

`build_app.sh` needs the Swift compiler (the Command Line Tools are enough, `xcode-select --install`) and uv. It downloads a standalone Python 3.12 and the locked dependencies through uv and writes only to `app/build/` (or `--out DIR`), `app/.build/` and uv's cache. The minimum is macOS 15.0 because one locked dependency, `opencv-python-headless` 5.1.0.95, only has wheels for macOS 15 and later. `app/build_app.sh --help` lists the options. Move the app anywhere you like, for example to `/Applications`.

A source build is signed ad hoc, which is enough on the Mac that built it. On another Mac, Gatekeeper refuses to open it the first time because it is not from an identified developer or notarized. That Mac's user can allow it under System Settings ▸ Privacy & Security ▸ Open Anyway. A source build is also a different app from the download as far as macOS is concerned, so permissions granted to one do not carry over to the other.

To sign and notarize a build with your own Developer ID (`PROFILE` is a keychain profile made with `xcrun notarytool store-credentials PROFILE`):

```
app/build_app.sh --sign "Developer ID Application: Your Name (TEAMID)" --notarize PROFILE --zip
```

`--sign` turns on the hardened runtime and a secure timestamp and checks the result with `app/preflight_app.sh` before anything is uploaded. `app/build_app.sh --hardened` signs ad hoc with the hardened runtime, to try that out without a certificate.

### Using it

- **Adding videos.** Drop them on the window or on the app icon in the Dock or the Finder, use File ▸ Open… (⌘O), or run `open -a Framescribe path/to/video.mp4`. They run one at a time, in the order they came.
- **Stop** ends a run cleanly. Finished stages stay cached, so adding the same video again continues from there.
- **Closing the window** while something runs (a video or the model download) does not stop it. Click the Dock icon to get the window back. With nothing running, closing the window quits the app. Quit (⌘Q) asks first if something runs.
- **Output** goes to `~/Documents/framescribe/<video name>/`: the same files as `framescribe run`, plus `framescribe-app.log`. If that folder holds results of a different video with the same name, the app uses `<video name> 2`, `<video name> 3` and so on. Settings (⌘,) changes the output folder.
- **When more than usual was filled in** (see [Accuracy and completeness](#accuracy-and-completeness)) the finished job shows a warning with the share. The report is ready as usual.
- **First start.** The app runs the same checks as [doctor](#doctor). It looks for `ffmpeg` in `/opt/homebrew/bin`, `/usr/local/bin` and the `PATH` of your login shell, because apps started from the Finder do not see your shell's `PATH`. If the speech model is missing, a button downloads it (about 2.5 GB, into the Hugging Face cache, shared with the command line tool). Nothing is downloaded until you click it.

The app is not sandboxed (it starts the bundled Python and reads videos from anywhere). It collects nothing and does not update itself. Build and release details are in [docs/DESIGN.md](docs/DESIGN.md#app).

## Command line install

The installer of the release `v0.1.0`, fetched from that tag:

```
curl -fsSL https://raw.githubusercontent.com/jwiderberg/framescribe/v0.1.0/install.sh | bash
```

The same with a check of the script before it runs. The release notes give the SHA-256 of `install.sh` at that tag. Compare the two values by eye, and run the script only when they are equal:

```
curl -fsSLO https://raw.githubusercontent.com/jwiderberg/framescribe/v0.1.0/install.sh
shasum -a 256 install.sh
bash install.sh
```

The tagged URL works from the first release on. Until a release exists, the tag does not resolve and the one-liner fails with a 404. Install from the branch `main` then, knowing that it is whatever was pushed last:

```
curl -fsSL https://raw.githubusercontent.com/jwiderberg/framescribe/main/install.sh | bash -s -- --from-main
```

Or from a clone, so you can read the script first:

```
git clone https://github.com/jwiderberg/framescribe.git
cd framescribe
./install.sh
```

The installer checks for macOS on Apple Silicon, `ffmpeg`, `ffprobe` and `uv`. If one is missing it prints the install command and stops, unless you pass `--install-ffmpeg` (runs `brew install ffmpeg`) or `--install-uv` (uv's official installer, without touching your shell profile). Then it runs `uv tool install --force --reinstall --python 3.12 -c constraints.txt` on the clone that holds the script. A piped script installs the release tag (`git+https://github.com/jwiderberg/framescribe@v0.1.0`) instead, or `main` with `--from-main`. Running it again upgrades. Then `framescribe setup` downloads the speech model (`--no-model` skips this) and `framescribe doctor` checks the result. After `--no-model`, a doctor failure caused only by the missing model is expected and the script exits 0. Any other failure exits 1.

`constraints.txt` holds the dependency versions of `uv.lock` (`uv export --locked --no-dev --no-emit-project --no-hashes`, kept equal to the lock by a test). The installer reads it from the clone, or from the same tag or branch on GitHub, so an install gets exactly the locked versions. It has no hashes: `uv tool install` (0.11) accepts hash lines in a constraints file but does not check them, so they would only look like a check. The lock's hashes are checked where they are enforced, in the app build (`uv pip install --require-hashes`).

It never uses `sudo`, refuses to run as root (`--allow-root` overrides that), never edits a shell profile (it prints the `PATH` line to add if needed), sends no telemetry and installs nothing else. `./install.sh --dry-run` prints every action without doing it. `./install.sh --help` lists the options. `FRAMESCRIBE_SOURCE` installs a different directory or Git URL, as given and without `constraints.txt`.

By hand: `uv tool install --python 3.12 -c constraints.txt .` in a clone, then `framescribe setup`.

### doctor

`framescribe doctor` prints one line per check, with how to fix what is wrong. Example, with paths and one long line cut short with `...`:

```
ok       platform          macOS on Apple Silicon (arm64)
ok       python            3.12.13
ok       ffmpeg            9.0.2 (/opt/homebrew/bin/ffmpeg)
ok       ffprobe           9.0.2 (/opt/homebrew/bin/ffprobe)
missing  speech model      mlx-community/parakeet-tdt-0.6b-v3 is not in the Hugging Face cache (...) ...  -> fix: framescribe setup
ok       apple vision ocr  importable (pyobjc Vision framework)
ok       disk space        78.8 GB free where the model cache is (...)
ok       memory            36 GB installed, 89% free
ok       whisperkit        not configured (optional)

something required is missing: see the lines marked 'missing'
```

Platform, Python, `ffmpeg`, `ffprobe`, the speech model and Apple Vision are required. Exit status 0 when all are present, 1 otherwise. Low disk space (under about 6 GB before the model download, 2 GB after), low memory (under 8 GB installed or under 20% free) and a broken WhisperKit setup are warnings only. `--json` prints JSON. `doctor` never uses the network.

## Quick start

```
framescribe run recording.mp4 --out out
```

Writes everything to `out/recording/`, with live status on stderr. Open `out/recording/report.md`. `transcript.md` next to it is the plain transcript.

A two-hour recording takes about 12 minutes on an M3 Max, of which the speech stage is about 5 (see [Memory and speed](#memory-and-speed)). A second run with the same inputs loads every stage from the cache.

```
framescribe run recording.mp4 --asr parakeet            # speech with parakeet only, even if WhisperKit is configured
framescribe run recording.mp4 --transcript captions.srt # use an existing transcript instead of running speech recognition
```

`--transcript` accepts `.srt`, `.vtt` and the JSON this tool writes as `work/transcript.json`. Any other extension is an error. A JSON file must name only sources and reasons this tool writes, or the run stops with an error. An imported transcript is not cross-checked by the run. Its segments, speakers and word timings are kept, its completeness figures and fill flags are dropped. So the report and `transcript.md` show none of its words in the tool's marks, and brackets like `[+ ... +]` in its text stay plain text. A round trip of the tool's own `transcript.json` keeps the words and their timings.

`run` stops before it reads the video if the platform, `ffmpeg`, `ffprobe` or the speech model is missing (the model is not needed with `--transcript` or `--asr whisperkit`). It never downloads anything. A missing model gives the message to run `framescribe setup`.

`--progress` picks the status display: `rich` (a live panel), `plain` (timestamped lines, progress at most every 5 s per stage) or `json` (one event per line on stdout, nothing else on stdout). The default, `auto`, is `rich` on a terminal and `plain` otherwise. Ctrl-C, SIGTERM and SIGHUP stop the run cleanly, write `run.json` with status `interrupted` and keep finished stages in the cache.

A host application (the Mac app is one) should use `--progress json` and `--run-dir DIR`. Failure events carry `error_kind` (`no_audio`, `incomplete_transcript` or null), and `run_end` carries the completeness figures and warnings, so a host never has to read message text. `setup --progress json` reports the model download the same way. The fields are in [docs/DESIGN.md](docs/DESIGN.md#live-status).

## Using the report with an LLM

1. Run framescribe on the recording.
2. Give an LLM tool that can read files and open images these files from `out/recording/`, together: `report.md`, `ANALYSIS_PROMPT.md` and the folder `frames/`. Add `timeline.json` if the tool should have the same evidence as data. Nothing else is needed.
3. Tell it to follow `ANALYSIS_PROMPT.md`. That file asks for a summary, a findings table, a step-by-step walkthrough and open questions, with a timestamp, a step number and a quote or frame as evidence for every finding.

Do not share `run.json`, `work/` or the app's `framescribe-app.log`. They are local diagnostics and contain the full paths of the video and the output folder on this Mac, which name the user account and the folders. `report.md`, `transcript.md` and `timeline.json` name the video by its file name only.

Everything in the report is recorded content: what someone said and what a screen showed. Some of it may be written to steer an AI ("ignore your instructions", a link to open). `ANALYSIS_PROMPT.md` and the report's "How to read this document" tell the tool to treat all of it as data, to report such text as a finding instead of acting on it, to open only the files under `frames/` and to output no link or image that points elsewhere. Special characters in recorded text, and in every value read from an artifact or an imported transcript, are escaped in `report.md` and `transcript.md`. Rendered by a GitHub Flavored Markdown renderer (GitHub, cmark-gfm), no recorded text becomes a link, an image, a heading, a list, a quote, a code block or HTML. Web and e-mail addresses are covered too. A backslash before the colon of `http://` (in any case) and the dot of `www.`, and an invisible word joiner before the `@` of an e-mail address, keep them from becoming links. That is the guarantee. Not covered: a bare domain such as `example.com/x` stays as it is, and a viewer that links bare domains on its own (beyond GFM) can still turn it into a link. Invisible format characters are removed from recorded text in both files and in the terminal. The joiners that emoji and several scripts need stay. These measures make misuse harder. They do not make an LLM immune to it, so check what the tool does with a recording from an untrusted source.

`ANALYSIS_PROMPT.md` is fixed text. Edit it for another purpose. A tool that cannot open images can still use the report, but the frames are the authority where on-screen text was misread. The report is long (about 67,000 words for a two-hour recording) because it keeps every transcript segment and every visible change.

## How it works

A fixed list of stages. Each reads earlier artifacts and writes one JSON artifact under `work/`. In order:

- **ingest** probes the video, extracts 16 kHz mono audio, reads the first embedded subtitle track if there is one and finds the shared screen in the frame (the largest rectangle of pixels that are ever non-black, from 40 sampled frames). If it is not confident it uses the full frame and records a warning.
- **asr.parakeet** runs parakeet-mlx in-process, in two passes. Chunks are about 30 s (never more than 40 s), cut at pauses, each decoded with 1 s of audio before and 2 s after as context. The second pass cuts in different places and is kept as a cross-check. These numbers were chosen by measurement. Without chunks the model needs about 15 GB of memory for 10 minutes of audio, so long audio is always chunked.
- **asr.whisperkit** (optional) runs an external `whisperkit-cli`. See [Optional WhisperKit](#optional-whisperkit).
- **asr** builds the final transcript, cross-checks it against the other sources, fills in and marks what they heard and the primary did not, and assigns speakers.
- **visual** compares gray frames of the content area at 2 samples per second and keeps a key frame per distinct screen state, with a kind (`initial`, `page`, `partial`, `scroll` or `minor`, a heuristic) and pointer positions where it finds them reliably. It keeps at most 20 states per minute, at least 1.5 s apart, and cuts off a change that does not settle within 10 s.
- **ocr** reads every key frame with Apple Vision (accurate level, 2x upscale) and lists the lines added and removed since the previous state.
- **report** is deterministic and calls no model. It splits the recording into steps (each starts at a confident page change and is 30 s to 240 s long), picks a main frame and up to three extra frames per step, and merges speech, screen changes and pointer hints into one timeline. Step titles are the tallest on-screen line of the main frame, exactly as read.

Thresholds and the reasons for them are in [docs/DESIGN.md](docs/DESIGN.md) and [docs/MEASUREMENTS.md](docs/MEASUREMENTS.md).

Every ffmpeg and ffprobe call that opens the video allows only local files (`-protocol_whitelist file`) and only the MP4 family and Matroska demuxers (`-format_whitelist mov,matroska`), with the absolute path of the file. A file that is really an ffconcat script, an HLS playlist or another description of other media is refused before anything is read from it. Before extracting anything, `ingest` refuses a video longer than 12 hours, a frame larger than 8192 pixels on a side, and audio whose 16 kHz WAV would be larger than about 1.4 GB.

A stage is loaded from the cache when its key matches. The key covers a fingerprint of the video (size, modification time, hashes of the first and last 4 MB), the stage and its version, the configuration it uses and the stages it depends on. `--force STAGE` recomputes that stage and everything downstream (`--force visual` redoes `visual`, `ocr` and `report`). It can be repeated, and `--force all` redoes everything. Stage names: `ingest`, `asr.parakeet`, `asr.whisperkit`, `asr`, `visual`, `ocr`, `report`.

## Accuracy and completeness

No speech recognition hears everything, and a sentence it skips leaves no trace in its own output. So the `asr` stage compares the primary transcript with every other transcript of the same audio it has: the second parakeet pass (always on, costs about one more pass), the embedded captions if there are any (matched more loosely, since a cue has only one time) and WhisperKit if configured.

- **Gaps.** A gap is a run of another source's words where the primary has none and leaves room for them (at least 0.3 s of silence, and at least half the phrase's own duration). Gaps are filled in at their time. `[+` ... `+]` means two or more sources have it and every word is in at least two sources. `[?` ... `?]` means less certain. Primary words are never changed or removed. A misheard word or a different spelling is not a gap. Rejected gaps are listed with a reason in `work/transcript.json` (`repeats-adjacent`, `fragment`, and `no-speech` for WhisperKit, which invents text in silence).
- **More filled in than usual.** If more than 3% of the words had to be filled in (`--max-filled-share`), the run still finishes, with a warning. Filling in is the remedy, and refusing the result would leave you with nothing. The warning is at the top of `transcript.md`, first in the report's Completeness row, in the status line and in `run_end` (`above_limit`). Check the marked words against the recording. On a short recording a few missed words already make a large share. `--strict-completeness` makes the run fail instead and writes the refused transcript to `work/transcript.gate-failed.json`. `--allow-incomplete` is deprecated and does nothing.
- **Casing at cuts.** The model tends to start a sentence at a cut, so the first word after a cut takes its casing from the other pass when that pass heard the same word. Only the case changes, never the word.
- **Names.** In testing, no engine got product and personal names right reliably. The report keeps the spoken forms, shows the on-screen vocabulary next to them, and `ANALYSIS_PROMPT.md` says the on-screen spelling is the authority.
- **Speakers.** If a caption cue has a speaker name (`(Name)` as its first line, or a VTT `<v Name>` tag), each transcript segment gets the speaker whose captions overlap it most. No speaker identification from audio.

### Optional WhisperKit

Parakeet with its second pass is the supported path. Nothing needs WhisperKit. It can be one more cross-check source. It is much slower (about 7x realtime in a debug build) and invents text in silence, but in tests it gave better sentences than parakeet on contested words. framescribe does not install or download it. You need a `whisperkit-cli` built from the open-source WhisperKit project and a WhisperKit CoreML model directory (for example `openai_whisper-large-v3_turbo` from the `argmaxinc/whisperkit-coreml` Hugging Face repository):

```
export FRAMESCRIBE_WHISPERKIT_CLI=/path/to/whisperkit-cli
export FRAMESCRIBE_WHISPERKIT_MODEL=/path/to/openai_whisper-large-v3_turbo
framescribe run recording.mp4            # --asr auto: parakeet, plus WhisperKit
```

or with options:

```
framescribe run recording.mp4 --asr both \
  --whisperkit-cli /path/to/whisperkit-cli \
  --whisperkit-model /path/to/openai_whisper-large-v3_turbo
```

With `--asr auto` and no CLI or model, WhisperKit is skipped and `run.json` records why. With `--asr whisperkit` or `--asr both` the run fails instead. The first run of a model can take minutes while CoreML compiles it.

## Options

`framescribe run VIDEO [OPTIONS]`:

| Option | Default | Meaning |
|---|---|---|
| `VIDEO` | required | Source video file. |
| `--out DIR` | `out` | Output root. Results go to `DIR/<video-stem>/`, or to `DIR/video/` when the stem cannot be a folder name of its own (empty, `.` or `..`, or holding a control character). |
| `--run-dir DIR` | none | Put the results in exactly this folder instead. It must not be a symbolic link. |
| `--asr parakeet\|whisperkit\|both\|auto` | `auto` | Speech backend. `auto` is parakeet, plus WhisperKit only when its CLI and model are both configured. `whisperkit` and `both` fail when WhisperKit does not resolve. |
| `--transcript FILE` | none | Use this SRT, VTT or transcript JSON instead of speech recognition. Not cross-checked. Only with `--asr auto`. |
| `--content-rect x,y,w,h` | none | Skip layout detection and use this rectangle, in pixels. It must fit inside the frame. |
| `--whisperkit-cli PATH` | none | `whisperkit-cli` executable. |
| `--whisperkit-model DIR` | none | WhisperKit model directory. |
| `--force STAGE` | none | Recompute this stage and everything downstream. Repeatable. `all` recomputes everything. |
| `--max-filled-share X` | `0.03` | Share (0 to 1) of words filled in from other sources above which the result carries a warning. |
| `--strict-completeness` | off | Fail instead when the filled-in share is above `--max-filled-share`. |
| `--allow-incomplete` | off | Deprecated, no effect. Still accepted so old command lines work. |
| `--progress auto\|rich\|plain\|json` | `auto` | Status display. `auto` is `rich` when stderr is a terminal, `plain` otherwise. |

| Command | What it does |
|---|---|
| `framescribe setup` | Downloads the speech model (about 2.5 GB) into the Hugging Face cache, after checking that 6 GB are free. The only command that uses the network. |
| `framescribe setup --check` | Only reports whether the model is present. |
| `framescribe doctor [--json]` | Checks the machine. See [doctor](#doctor). |
| `framescribe --version` | Prints the version. |

| Variable | Used by | Meaning |
|---|---|---|
| `FRAMESCRIBE_WHISPERKIT_CLI` | `run`, `doctor` | Path or name of `whisperkit-cli`. `--whisperkit-cli` wins over it. |
| `FRAMESCRIBE_WHISPERKIT_MODEL` | `run`, `doctor` | WhisperKit model directory. `--whisperkit-model` wins over it. |
| `HF_HOME`, `HF_HUB_CACHE` | `setup`, `run`, `doctor` | Where the Hugging Face cache (the speech model) lives. The Mac app passes these and `HF_ENDPOINT` from the login shell, never `HF_TOKEN`: the model is public. |
| `HF_HUB_OFFLINE` | `setup` | `run` and the speech-model stage set it to `1` for their process. If you set it, `setup` refuses to download. |
| `FRAMESCRIBE_SOURCE` | `install.sh` | What to install instead of the clone or the release tag: a directory or a Git URL, installed as given. |

| Command | Exit status |
|---|---|
| `run` | 0 on success. 2 when a stage fails, the request is invalid or a start-up check fails (message on stderr). 130 after Ctrl-C. 128 plus the signal number after a signal (143 after SIGTERM). |
| `doctor` | 0 when everything required is present, 1 otherwise. |
| `setup` | 0 on success or when the model is already there. 1 on failure, or with `--check` when the model is missing. 130 after Ctrl-C. |

Everything else is fixed in code, as fields of `Config` in `src/framescribe/config.py`: language `en`, chunk lengths, layout sampling, the `visual` thresholds, the OCR upscale and the step lengths.

### Files in `work/`

```
ingest.json               stream facts, embedded captions, detected layout
audio.wav                 16 kHz mono PCM (absent when the video has no audio)
asr.parakeet.json         words, sentences, cuts, warnings and the second pass
asr.whisperkit.json       only when WhisperKit ran (plus whisperkit-report/)
transcript.json           the final transcript, with the completeness figures and every gap
transcript.gate-failed.json  only after a run with --strict-completeness that refused the transcript
visual.json               visual states: times, key frame path, change into each state, pointer samples
ocr.json                  per state: text lines with rectangles, added and removed lines
timeline.json             the report stage's artifact (the same content as ../timeline.json)
<stage>.log               written by a stage that logs something
<artifact>.key            cache key and side-file sizes
```

With `--transcript` the `asr.*` files are not written.

## Memory and speed

Measured on an M3 Max with 36 GB, so other machines will probably differ. Details and caveats are in [docs/MEASUREMENTS.md](docs/MEASUREMENTS.md).

A two-hour recording takes about 12 minutes when the machine is otherwise quiet, of which the speech stage is about 5. A full run of a 1 h 53 min recording on a busy machine (load average 6 to 17) took about 14 minutes: `asr.parakeet` 429 s (both passes, model load included), `visual` 164 s, `ocr` 261 s for 763 key frames, `asr` and `report` about a second each.

MLX's buffer cache is unbounded by default and grew to 26 GB on 10 minutes of audio, while the memory in use stayed at 1.2 GB. So `asr.parakeet` limits it to 512 MB while it decodes. In the measurement the system's free memory then stayed at 81% or more, and the words were the same. The limit is `mlx_cache_limit_mb` in `Config` (0 turns the cache off). It has no command-line option. A run's peak MLX memory is in `run.json`, under `metrics` of the stage's record.

## Known limitations

- **English only.** The language is fixed to `en`, and only English speech has been tested.
- **Misheard names.** Speech recognition keeps filler words and mishears names, product names and jargon. The report does not correct them. The reader is told to use the on-screen spelling.
- **No speaker diarization** beyond the speaker names that embedded captions carry.
- **Stray capitals at cuts.** After the casing fix, about 12 to 13% of cuts on the measured recording still have a capital without a sentence stop before it (24% before the fix), against about 2% for words elsewhere.
- **Single words dropped silently.** A word only another source heard is filled only where the primary leaves enough silence, so a single word squeezed between two primary words is usually dropped without a record. On the measured recording that was 74 candidates (67 single words), plus 48 that only repeated a neighbouring word or lay inside a caption cue the primary already covered. A fill can also be partial. Details in `docs/DESIGN.md`.
- **Layout detection.** The shared screen is taken to be the largest non-black rectangle. One narrower or lower than 40% of the frame, or smaller than a quarter of it, is not accepted. A tile that touches the content and is taller than about three quarters of it can be mistaken for the content. Use `--content-rect` then.
- **Visual states.** About one state in five is a near-duplicate of the one before (an animation or a moving highlight). Scrolling down and back up is one state. Scroll kinds and amounts are hints (shifts of 25 px or less are doubtful). A key frame taken while a page is still filling in can be an early picture of it.
- **Pointer.** Samples exist for a minority of moments and about 70% are accurate (14 of 20 in a hand check). White pointers on dark backgrounds are not found.
- **OCR.** Roughly one on-screen line in five or six has a misread (a capital I read as a lower-case l, small text, merged labels). Icon-only items are not read. A misread splits one term into variants in the vocabulary list.
- **Report structure.** Step titles are the tallest on-screen line, so they sometimes name a button or a field label instead of the page. Steps follow page changes and length, not topics.
- **Filled-in speech** has the wording and the mistakes of its source, and its timing inside the gap is approximate.
- **Memory for long audio.** The speech stages read the whole 16 kHz track into memory and compute its level from 64-bit copies of it: by calculation about 1 GB per hour of audio at the peak, for a few seconds. A 12-hour video, the longest framescribe accepts, needs about 12 GB for that step alone.
- **Containers.** Only MP4, MOV, M4V, MKV and WebM files are read. The Mac app accepts any file macOS calls a movie, and a run of another kind of file fails with that message.
- **Not implemented:** correcting names from on-screen text, resolving what the speaker refers to, automatic call-outs (bugs, confusion, praise), subtitle files, speaker identification from audio, a configuration file.

## Privacy

Everything runs on your machine. The video, audio, frames and text never leave it. No upload, no telemetry, no account.

The only network use is `framescribe setup`, which downloads the speech model from Hugging Face once: a fixed commit of `mlx-community/parakeet-tdt-0.6b-v3` whose two files are checked against SHA-256 values built into framescribe. `doctor` checks them again in the cache. `run` and `doctor` never use the network. `run` and the speech-model stage put Hugging Face's client into offline mode, so a program that uses framescribe as a library cannot trigger a download either (its process is left in offline mode afterwards). `install.sh` uses the network through `uv` (the package, `constraints.txt` and the Python dependencies) and through `setup`. The optional WhisperKit CLI is a separate program you supply.

How to report a security problem, and what framescribe trusts and does not trust, is in [SECURITY.md](SECURITY.md).

## Development

```
git clone https://github.com/jwiderberg/framescribe.git
cd framescribe
uv sync                           # the locked dependencies, with pytest and pyyaml for the tests
uv run pytest -q                  # all tests, including the slow end-to-end test
uv run pytest -q -m "not slow"    # without the end-to-end test
```

The tests need no network and none of your recordings. The slow end-to-end test uses macOS `say` to make speech and runs the real parakeet model, so it needs the model (`framescribe setup`). The Mac app's tests run with `app/run_tests.sh`. For an end-to-end check of a built app, `swift build --package-path app --product framescribe-e2e` builds a driver that runs the app's embedded runtime on a video through the app's own code:

```
app/.build/debug/framescribe-e2e --app app/build/Framescribe.app --video VIDEO --out FOLDER
```

`scripts/` has the measurement scripts the docs refer to (`asr_memory.py`, `asr_casing.py`, `asr_chunking.py`). Run them with `uv run python scripts/<name>.py --help`. The ones that load the speech model never download it.

```
src/framescribe/   the package: CLI, pipeline, stages, report
scripts/              measurement scripts, release-secrets.sh
install.sh            the installer
constraints.txt       uv.lock's versions for the installer (uv export, checked by a test)
SECURITY.md           reporting a vulnerability, trust boundaries
docs/DESIGN.md        design, stage rules, artifact contracts, file layout
docs/MEASUREMENTS.md  measured numbers and what they were measured on
app/                  the Mac app: Swift package, build_app.sh, run_tests.sh, the signing and checking scripts
.github/workflows/    release.yml, the signed and notarized release (docs/RELEASING.md)
```

`timeline.json` is the data contract of the report. `--progress json` and `--transcript` are the integration points for a host application.

## Licence

MIT. See [LICENSE](LICENSE).
