#!/usr/bin/env bash
# Signs a built Framescribe.app inside out: every Mach-O file of the embedded runtime (extension modules, libraries),
# then the runtime's Python executable with its entitlements, then the app. Works on any finished bundle, so the same
# code signs a local build (build_app.sh calls it) and, in the release workflow, a bundle built by another job.
# Runs nothing from the bundle and needs only codesign, find, file, plutil and realpath.
#
# Before anything is signed the bundle passes app/bundle_check.sh (the same function the release workflow runs on the
# unpacked artifact): it is a real Framescribe.app with no symbolic link at its fixed places, no path with a control
# character, every link resolving inside it and plain Info.plist values. Mach-O files are found by their content
# (every regular file, whatever its name), by two independent methods that must agree, and only regular files at
# physical paths inside the bundle are signed: no symbolic link is ever followed.
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

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=app/bundle_check.sh
source "$HERE/bundle_check.sh"

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
    [[ -n "$app" ]] || die "usage: sign_app.sh [options] APP (see --help)"
    APP="$(physical_bundle "$app")" || die "$BUNDLE_PROBLEM"
    check_bundle "$APP" || die "refused, nothing was signed: $BUNDLE_PROBLEM"
    local support py
    support="$HERE/Support"
    py="$APP/$RUNTIME_PYTHON"  # a regular file: check_bundle checked it
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

    TMPD="$(mktemp -d "${TMPDIR:-/tmp}/framescribe-sign.XXXXXX")"
    trap 'rm -rf "$TMPD"' EXIT
    local all="$TMPD/macho.txt" second="$TMPD/macho-file.txt" list="$TMPD/inner.txt" errs="$TMPD/errs.txt"
    local main_exe
    main_exe="$APP/Contents/MacOS/$(plutil -extract CFBundleExecutable raw -o - "$APP/Contents/Info.plist")"
    # every Mach-O file anywhere in the bundle, by content; then the same by an independent method
    macho_by_magic "$APP" | LC_ALL=C sort > "$all"
    macho_by_file "$APP" | LC_ALL=C sort > "$second"
    if ! cmp -s "$all" "$second"; then
        diff "$all" "$second" | sed "s|$APP/||" | head -10 >&2 || true
        die "refused, nothing was signed: the two ways of finding Mach-O files disagree (above)"
    fi
    grep -qxF "$py" "$all" || die "refused, nothing was signed: $RUNTIME_PYTHON is not a Mach-O file"
    # inside out: every other Mach-O file (extension modules, libraries, helper executables), then the Python
    # executable with its entitlements, then the app (which signs its main executable)
    grep -vxF -e "$py" -e "$main_exe" "$all" > "$list" || true
    local n_all n_inner
    n_all="$(wc -l < "$all" | tr -d ' ')"
    n_inner="$(wc -l < "$list" | tr -d ' ')"
    note "$n_inner libraries, extension modules and other Mach-O files"
    REPLACED=0
    SIGNED=0
    local -a batch=()
    local f
    while IFS= read -r f; do
        [[ -f "$f" && ! -L "$f" && "$f" == "$APP"/* ]] || die "not a regular file inside the bundle: $f"
        batch+=("$f")
        if [[ ${#batch[@]} -ge 64 ]]; then codesign_quiet "$errs" "${args[@]}" "${batch[@]}"; SIGNED=$((SIGNED + ${#batch[@]})); batch=(); fi
    done < "$list"
    if [[ ${#batch[@]} -gt 0 ]]; then codesign_quiet "$errs" "${args[@]}" "${batch[@]}"; SIGNED=$((SIGNED + ${#batch[@]})); fi
    if [[ -n "$entitlements" ]]; then
        note "entitlements of ${py#"$APP"/}: $(basename "$entitlements")"
        codesign_quiet "$errs" "${args[@]}" --entitlements "$entitlements" "$py"
    else
        codesign_quiet "$errs" "${args[@]}" "$py"
    fi
    SIGNED=$((SIGNED + 1))
    codesign_quiet "$errs" "${args[@]}" "$APP"
    if [[ -f "$main_exe" ]] && grep -qxF "$main_exe" "$all"; then SIGNED=$((SIGNED + 1)); fi
    if [[ $REPLACED -gt 0 ]]; then note "replaced the existing signature of $REPLACED files"; fi
    # the count signed must be the count an independent enumeration of the signed bundle finds
    local after
    after="$(macho_by_file "$APP" | grep -c . || true)"
    [[ $SIGNED -eq $n_all && $after -eq $n_all ]] \
        || die "signed $SIGNED Mach-O files, but the bundle holds $after (found $n_all before signing)"
    note "signed $SIGNED Mach-O files, every one the bundle holds"
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
