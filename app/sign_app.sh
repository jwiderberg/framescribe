#!/usr/bin/env bash
# Signs a built Framescribe.app inside out: every Mach-O file of the embedded runtime (extension modules, libraries),
# then the runtime's Python executable with its entitlements, then the app. Works on any finished bundle, so the same
# code signs a local build (build_app.sh calls it) and, in the release workflow, a bundle built by another job.
# Runs nothing from the bundle and needs only codesign, find and file.
#
#   app/sign_app.sh APP                       ad hoc, no hardened runtime, no entitlements (the default local build)
#   app/sign_app.sh --hardened APP            ad hoc with the hardened runtime, no timestamp (ad hoc has none), the
#                                             Python executable with Support/runtime-adhoc-test.entitlements
#   app/sign_app.sh --identity "Developer ID Application: NAME (TEAMID)" [--keychain PATH] APP
#                                             that identity, the hardened runtime and a secure timestamp, the Python
#                                             executable with Support/runtime.entitlements (the release)
#   --entitlements FILE                       other entitlements for the Python executable (hardened builds only)
#
# Entitlements apply to the process an executable starts, so only the Python executable gets them and the app's own
# executable none. (The files cannot explain themselves: codesign refuses comments in them.)
# runtime.entitlements, the release: allow-unsigned-executable-memory. numba compiles with llvmlite's MCJIT, which makes
# read-write memory executable. Without it the process dies with SIGKILL (Code Signature Invalid, "Invalid Page") as
# soon as librosa is imported. allow-jit does not help: LLVM does not map that memory with MAP_JIT.
# runtime-adhoc-test.entitlements, --hardened only: also disable-library-validation, because an ad-hoc signature has
# no Team ID and library validation refuses every bundled library ("different Team IDs"). A Developer ID build signs
# every file with one Team ID and needs no such exception. docs/DESIGN.md (Release) has the measurements.
set -euo pipefail

die() { printf 'sign_app.sh: %s\n' "$*" >&2; exit 1; }
note() { printf '%s\n' "$*"; }

main() {
    local identity="-" keychain="" hardened=0 entitlements="" app=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --identity) [[ $# -ge 2 && -n "$2" ]] || die "--identity needs a signing identity"; identity="$2"; shift ;;
            --keychain) [[ $# -ge 2 && -f "$2" ]] || die "--keychain needs the path of a keychain file"; keychain="$2"; shift ;;
            --hardened) hardened=1 ;;
            --entitlements) [[ $# -ge 2 && -f "$2" ]] || die "--entitlements needs a file"; entitlements="$2"; shift ;;
            -h|--help) sed -n '2,/^set -euo pipefail$/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
            -*) die "unknown option: $1 (see --help)" ;;
            *) [[ -z "$app" ]] || die "one app at a time"; app="$1" ;;
        esac
        shift
    done
    [[ -n "$app" && -f "$app/Contents/Info.plist" ]] || die "usage: sign_app.sh [options] APP (see --help)"
    APP="$(cd "$app" && pwd)"
    local support py
    support="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/Support"
    py="$(find "$APP/Contents/Resources/runtime/bin" -maxdepth 1 -type f -name 'python3.*' | head -1)"
    [[ -n "$py" ]] || die "no runtime/bin/python3.* in $APP"
    if [[ "$identity" != "-" ]]; then hardened=1; fi
    if [[ -n "$keychain" && "$identity" == "-" ]]; then die "--keychain needs --identity"; fi
    if [[ -n "$entitlements" && $hardened -eq 0 ]]; then die "--entitlements needs --hardened or --identity"; fi

    local args=(--force --sign "$identity")
    if [[ -n "$keychain" ]]; then args+=(--keychain "$keychain"); fi
    if [[ $hardened -eq 0 ]]; then
        note "sign ad hoc, inside out"
    elif [[ "$identity" == "-" ]]; then
        # a secure timestamp comes from Apple's timestamp server for a certificate; an ad-hoc signature has none
        note "sign ad hoc with the hardened runtime (a local test of the release signing; no timestamp), inside out"
        args+=(--options runtime --timestamp=none)
        [[ -n "$entitlements" ]] || entitlements="$support/runtime-adhoc-test.entitlements"
    else
        note "sign with \"$identity\", the hardened runtime and a secure timestamp, inside out"
        args+=(--options runtime --timestamp)
        [[ -n "$entitlements" ]] || entitlements="$support/runtime.entitlements"
    fi

    LIST="$(mktemp "${TMPDIR:-/tmp}/framescribe-sign.XXXXXX")"
    ERRS="$(mktemp "${TMPDIR:-/tmp}/framescribe-sign.XXXXXX")"
    trap 'rm -f "$LIST" "$ERRS"' EXIT
    local list="$LIST" errs="$ERRS"
    # every Mach-O file of the runtime (extension modules, libraries), the Python executable last
    find "$APP/Contents/Resources/runtime" -type f ! -name '*.py' ! -name '*.pyc' ! -name '*.txt' ! -name '*.json' -print0 \
        | xargs -0 file --no-pad | grep -E ': +Mach-O' | grep -v ' (for architecture ' | sed 's/: *Mach-O.*$//' \
        | grep -vxF "$py" > "$list" || true
    note "$(wc -l < "$list" | tr -d ' ') libraries and extension modules"
    REPLACED=0
    local -a batch=()
    local f
    while IFS= read -r f; do
        batch+=("$f")
        if [[ ${#batch[@]} -ge 64 ]]; then codesign_quiet "$errs" "${args[@]}" "${batch[@]}"; batch=(); fi
    done < "$list"
    if [[ ${#batch[@]} -gt 0 ]]; then codesign_quiet "$errs" "${args[@]}" "${batch[@]}"; fi
    if [[ -n "$entitlements" ]]; then
        note "entitlements of ${py#"$APP"/}: $(basename "$entitlements")"
        codesign_quiet "$errs" "${args[@]}" --entitlements "$entitlements" "$py"
    else
        codesign_quiet "$errs" "${args[@]}" "$py"
    fi
    codesign_quiet "$errs" "${args[@]}" "$APP"
    if [[ $REPLACED -gt 0 ]]; then note "replaced the existing signature of $REPLACED files"; fi
    note "codesign --verify --deep --strict"
    codesign --verify --deep --strict --verbose=1 "$APP" 2>&1 | sed 's/^/  /'
}

codesign_quiet() {  # ERRFILE ARGS...: codesign, counting its "replacing existing signature" lines instead of printing each
    local errs="$1" rc=0
    shift
    codesign "$@" 2> "$errs" || rc=$?
    REPLACED=$((REPLACED + $(grep -c 'replacing existing signature' "$errs" || true)))
    grep -v 'replacing existing signature' "$errs" >&2 || true
    return $rc
}

main "$@"
