#!/usr/bin/env bash
# Installs framescribe as a uv tool on a Mac with Apple Silicon.
#
#   ./install.sh                        from a clone of the repository
#   curl -fsSL https://raw.githubusercontent.com/jwiderberg/framescribe/v0.1.0/install.sh | bash
#                                       the release v0.1.0 (the README shows how to check the file first)
#   ./install.sh --help
#
# What it installs: the clone it is in, or, when piped, the release tag v$VERSION from GitHub (VERSION below, the
# version in pyproject.toml), or with --from-main the branch main. The dependency versions of uv.lock apply in every
# case, through constraints.txt (from the clone, or from the same tag or branch).
#
# It never uses sudo, never edits a shell profile and sends no telemetry. The network is used by `uv` (the package
# and its Python dependencies) and by `framescribe setup` (the speech model, from Hugging Face). Safe to run again:
# a second run upgrades the installed tool.
#
# The script does nothing until its last line: everything above only defines functions and variables, and the
# work happens in `main`, called on the last line. A truncated download therefore runs nothing.
set -euo pipefail

VERSION="0.1.0"  # the release this script installs when piped; equal to pyproject.toml (tests check it)
REPO_URL="https://github.com/jwiderberg/framescribe"
RAW_URL="https://raw.githubusercontent.com/jwiderberg/framescribe"
CONSTRAINTS_FILE="constraints.txt"  # uv.lock's versions as constraints (uv export --no-hashes, see the README)
UV_INSTALL_URL="https://astral.sh/uv/install.sh"
MODEL_REPO="mlx-community/parakeet-tdt-0.6b-v3"
PYTHON_VERSION="3.12"  # the only version framescribe supports; uv fetches it when it is not installed

DRY_RUN=0
WITH_MODEL=1
INSTALL_FFMPEG=0
INSTALL_UV=0
FROM_MAIN=0
ALLOW_ROOT=0
SOURCE=""
CONSTRAINTS=""

say() { printf '%s\n' "$*"; }
err() { printf 'error: %s\n' "$*" >&2; }
die() { err "$*"; exit 1; }

# run CMD [ARG...]: show the command and run it; with --dry-run only show it.
run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] would run:'
    printf ' %q' "$@"
    printf '\n'
    return 0
  fi
  printf '+'
  printf ' %q' "$@"
  printf '\n'
  "$@"
}

usage() {
  cat <<'EOF'
Usage: install.sh [options]

Installs framescribe with `uv tool install`, downloads the speech model
(about 2.5 GB, one time) and runs `framescribe doctor`.

Options:
  --from-main       install the branch main from GitHub instead of the release tag
                    (before the first release, the tag does not exist yet)
  --no-model        do not download the speech model (run `framescribe setup` later)
  --install-ffmpeg  install ffmpeg with Homebrew if it is missing (`brew install ffmpeg`)
  --install-uv      install uv with its official installer if it is missing
                    (without touching your shell profile)
  --dry-run         print every action without doing it
  --allow-root      run even as root (refused otherwise: uv tools belong to a user)
  --help            show this text

What is installed: the clone this script file is in; when the script is piped into bash, or read from
anywhere else, the release tag on GitHub (git+https://github.com/jwiderberg/framescribe@vVERSION), or main
with --from-main. The versions locked in uv.lock apply through constraints.txt, read from the clone or
from the same tag or branch.

Environment:
  FRAMESCRIBE_SOURCE   install this instead: a directory or a pip/uv requirement such as
                          git+https://github.com/jwiderberg/framescribe@main
                          (installed as given, without constraints.txt)

Needs macOS on Apple Silicon, ffmpeg and uv. No sudo, no shell profile edits, no telemetry.
EOF
}

# The directory this script file is in, empty when it is read from a pipe.
script_dir() {
  local src="${BASH_SOURCE[0]:-}"
  if [ -n "$src" ] && [ -f "$src" ]; then
    (cd "$(dirname "$src")" && pwd)
  fi
}

is_clone() {
  [ -n "${1:-}" ] && [ -f "$1/pyproject.toml" ] && grep -q '^name = "framescribe"' "$1/pyproject.toml" 2>/dev/null
}

resolve_source() {
  local here
  if [ -n "${FRAMESCRIBE_SOURCE:-}" ]; then
    SOURCE="$FRAMESCRIBE_SOURCE"
    CONSTRAINTS=""
    return
  fi
  if [ "$FROM_MAIN" -eq 1 ]; then
    SOURCE="git+$REPO_URL@main"
    CONSTRAINTS="$RAW_URL/main/$CONSTRAINTS_FILE"
    return
  fi
  here="$(script_dir)"
  # Only the clone that holds this script file counts. A piped script has no file, and the directory the user happens
  # to stand in is not a reason to install from it.
  if is_clone "$here"; then
    SOURCE="$here"
    CONSTRAINTS="$here/$CONSTRAINTS_FILE"
    [ -f "$CONSTRAINTS" ] || die "$CONSTRAINTS is missing from this clone"
  else
    SOURCE="git+$REPO_URL@v$VERSION"
    CONSTRAINTS="$RAW_URL/v$VERSION/$CONSTRAINTS_FILE"
  fi
}

# Root installs a tool into root's home and a fresh shell of the user never sees it; worse, a script run with sudo
# can do anything. Refused unless asked for.
check_not_root() {
  if [ "$(id -u)" = "0" ] && [ "$ALLOW_ROOT" -eq 0 ]; then
    die "refusing to run as root: run it as the user who will use framescribe (or pass --allow-root)."
  fi
}

# Names of the required checks that failed, one per line, read from the output of `framescribe doctor --json` on
# stdin (a check fails when it is required and its status is neither "ok" nor "warning"). Prints nothing when
# nothing failed or the input is not that output.
failed_checks() {
  awk '
    /^[[:space:]]*"name": "/ { name = $0; sub(/^[[:space:]]*"name": "/, "", name); sub(/",?[[:space:]]*$/, "", name) }
    /^[[:space:]]*"status": "/ { status = $0; sub(/^[[:space:]]*"status": "/, "", status); sub(/",?[[:space:]]*$/, "", status) }
    /^[[:space:]]*"required": / { if ($0 ~ /true/ && status != "ok" && status != "warning") print name }
  '
}

# With --no-model a failing doctor is expected when the speech model is the only thing it reports as missing.
only_the_model_is_missing() {
  local failed
  # doctor exits 1 whenever something is missing; only its output matters here
  failed="$({ "$1" doctor --json 2>/dev/null || true; } | failed_checks)"
  [ "$failed" = "speech model" ]
}

check_platform() {
  local os arch
  os="$(uname -s)"
  arch="$(uname -m)"
  if [ "$os" != "Darwin" ]; then
    die "framescribe needs macOS on Apple Silicon (MLX and Apple Vision); this is $os $arch."
  fi
  if [ "$arch" != "arm64" ]; then
    die "framescribe needs Apple Silicon (arm64); this machine reports $arch. If this terminal runs under Rosetta, open a native one."
  fi
  say "ok: macOS on Apple Silicon"
}

check_ffmpeg() {
  local missing="" tool
  for tool in ffmpeg ffprobe; do
    command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
  done
  if [ -z "$missing" ]; then
    say "ok: ffmpeg and ffprobe found"
    return
  fi
  if [ "$INSTALL_FFMPEG" -eq 1 ]; then
    command -v brew >/dev/null 2>&1 || die "--install-ffmpeg needs Homebrew (https://brew.sh), which was not found. Install ffmpeg yourself, then run this script again."
    run brew install ffmpeg
    if [ "$DRY_RUN" -eq 0 ]; then
      if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
        die "ffmpeg is still missing after brew install ffmpeg."
      fi
    fi
    return
  fi
  err "missing:$missing (framescribe needs ffmpeg and ffprobe)"
  err "install them with Homebrew:  brew install ffmpeg"
  err "(Homebrew itself: https://brew.sh) or run this script again with --install-ffmpeg"
  exit 1
}

check_uv() {
  if command -v uv >/dev/null 2>&1; then
    say "ok: uv found"
    return
  fi
  if [ "$INSTALL_UV" -eq 1 ]; then
    run sh -c "curl -LsSf $UV_INSTALL_URL | env UV_NO_MODIFY_PATH=1 sh"
    PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$HOME/.local/bin:$PATH"
    export PATH
    if [ "$DRY_RUN" -eq 0 ]; then
      command -v uv >/dev/null 2>&1 || die "uv is still not found after its installer ran."
    fi
    return
  fi
  err "missing: uv (https://docs.astral.sh/uv/)"
  err "install it with:  curl -LsSf $UV_INSTALL_URL | sh   (or: brew install uv)"
  err "or run this script again with --install-uv (it does not touch your shell profile)"
  exit 1
}

check_git_if_needed() {
  case "$SOURCE" in
    git+*) command -v git >/dev/null 2>&1 || die "git is needed to install from $SOURCE. Install the command line tools with: xcode-select --install" ;;
  esac
}

install_tool() {
  say "Installing framescribe from $SOURCE"
  if [ -n "$CONSTRAINTS" ]; then
    say "with the dependency versions of uv.lock: $CONSTRAINTS"
    run uv tool install --force --reinstall --python "$PYTHON_VERSION" -c "$CONSTRAINTS" "$SOURCE"
  else
    run uv tool install --force --reinstall --python "$PYTHON_VERSION" "$SOURCE"
  fi
}

main() {
  local bin_dir="" mt="framescribe" doctor_status=0 model_only=0

  while [ $# -gt 0 ]; do
    case "$1" in
      --help | -h) usage; exit 0 ;;
      --no-model) WITH_MODEL=0 ;;
      --install-ffmpeg) INSTALL_FFMPEG=1 ;;
      --install-uv) INSTALL_UV=1 ;;
      --dry-run) DRY_RUN=1 ;;
      --from-main) FROM_MAIN=1 ;;
      --allow-root) ALLOW_ROOT=1 ;;
      *) err "unknown option: $1 (see --help)"; exit 2 ;;
    esac
    shift
  done

  if [ "$DRY_RUN" -eq 1 ]; then
    say "[dry-run] nothing will be installed or downloaded"
  fi

  check_not_root
  check_platform
  check_ffmpeg
  check_uv
  resolve_source
  check_git_if_needed
  install_tool

  if [ "$DRY_RUN" -eq 0 ]; then
    bin_dir="$(uv tool dir --bin)"
    mt="$bin_dir/framescribe"
    [ -x "$mt" ] || die "uv reported success but $mt does not exist."
    case ":$PATH:" in
      *":$bin_dir:"*) ;;
      *)
        say ""
        say "Note: $bin_dir is not on your PATH, so the command 'framescribe' is not found yet."
        say "Add this line to your shell profile (this script does not edit it):"
        say "  export PATH=\"$bin_dir:\$PATH\""
        say ""
        ;;
    esac
  else
    say "[dry-run] would check that the uv tool bin directory (uv tool dir --bin) is on PATH and print the line to add if it is not"
  fi

  if [ "$WITH_MODEL" -eq 1 ]; then
    say ""
    say "Downloading the speech model: $MODEL_REPO, about 2.5 GB, from Hugging Face, one time."
    say "framescribe setup checks first that at least 6 GB of disk space are free and stops with the figure if not."
    run "$mt" setup || die "the model download did not finish. Run 'framescribe setup' to try again; it resumes."
  else
    say "Skipping the model download (--no-model). Run 'framescribe setup' before the first run."
  fi

  say ""
  run "$mt" doctor || doctor_status=$?
  if [ "$doctor_status" -ne 0 ] && [ "$WITH_MODEL" -eq 0 ] && only_the_model_is_missing "$mt"; then
    model_only=1
  fi

  say ""
  if [ "$doctor_status" -eq 0 ]; then
    say "Next step: transcribe a recording."
    say "  framescribe run recording.mp4 --out out"
    say "  Then give the folder out/recording/ (report.md, frames/, ANALYSIS_PROMPT.md) to an LLM tool; see the README."
  elif [ "$model_only" -eq 1 ]; then
    say "The speech model is the only thing missing, as expected with --no-model."
    say "Next step: download it (about 2.5 GB, one time), then check again."
    say "  framescribe setup"
    say "  framescribe doctor"
  else
    say "Next step: fix what 'framescribe doctor' marks as missing, then run it again."
    say "  framescribe setup     # downloads the speech model if that is what is missing"
    say "  framescribe doctor"
    exit 1
  fi
  exit 0
}

main "$@"
