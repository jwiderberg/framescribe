# Security

## Reporting a vulnerability

Report it privately through GitHub: the repository's Security tab, then "Report a vulnerability" (private vulnerability reporting is on). Do not open a public issue for it. A report that says what an attacker controls, what they gain and how to reproduce it is the fastest to act on. A proof of concept should work on files in a temporary folder, not on anything real.

Reports are answered on the reporting thread. A confirmed problem is fixed in a new release, and the advisory names the versions it affects.

## Supported versions

The latest release only. Fixes are not backported. Building from `main` gets fixes before they are released, along with everything else on `main`.

## Scope

In scope: the command line tool (`framescribe run`, `setup`, `doctor`), the Mac app, `install.sh`, the release workflow and the scripts it runs (`app/*.sh`, `scripts/release-secrets.sh`), and the files framescribe writes (`report.md`, `transcript.md`, `timeline.json`, `ANALYSIS_PROMPT.md`, `run.json`, `work/`, `frames/`).

Out of scope: ffmpeg, the Python packages and the speech model themselves (report those to their projects, unless framescribe uses them unsafely), the optional WhisperKit CLI, which the user supplies, and what an LLM tool does with a report beyond what the report and `ANALYSIS_PROMPT.md` tell it.

## Trust boundaries

Trusted: the user account that runs framescribe, its `PATH` and environment, the output folder the user chooses, the Hugging Face cache, and the tools on `PATH` (ffmpeg, ffprobe, uv, git). Whoever controls one of those already controls what framescribe does.

Not trusted: everything that comes from the recording or with it. That is the video file and its name, its audio, its frames and the text on them, embedded captions, imported transcripts (`--transcript`), and cached artifacts in an output folder someone else could have written to. framescribe treats all of it as data:

- A video's name never leaves the output folder. A name that cannot be a folder of its own (`...mov`, a name with a newline) runs in `video/`.
- ffmpeg opens only local files of the supported containers, so a file cannot make it read other files or the network.
- Files are written by renaming a new file over the old one, logs are opened without following symbolic links, and a run folder, `work/` or `frames/` that is a symbolic link or belongs to another user stops the run.
- Recorded text is escaped in `report.md` and `transcript.md`, and both say that everything quoted in them is data, not instructions. `timeline.json` keeps the text as it was, as data.
- Malformed numbers, timings and files stop a run with a plain error. Videos over 12 hours, frames over 8192 pixels on a side and audio over about 1.4 GB as WAV are refused before anything is extracted.
- A cached artifact that names a file outside the known set is not trusted, and the stage runs again.

framescribe is not sandboxed. The Mac app is not sandboxed either: it starts its bundled Python, which reads videos from anywhere the user can and writes to the chosen output folder.

## Network

`framescribe setup` downloads the speech model from Hugging Face: one fixed commit, whose two files are checked against SHA-256 values built into framescribe. `install.sh` downloads framescribe, its dependency versions (`constraints.txt`) and the dependencies through uv, and runs `setup`. Nothing else uses the network: `run` and `doctor` never do, and nothing is sent anywhere.

## Releases

Release builds are made by `.github/workflows/release.yml`: signed with a Developer ID, notarized by Apple, run once on a fresh machine before publishing, and attested with a build provenance statement. The release notes say how to check a download. [docs/RELEASING.md](docs/RELEASING.md) describes the process and the repository settings it relies on.
