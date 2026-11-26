#!/usr/bin/env bash
# Packs a built Framescribe.app for download: Framescribe-VERSION.zip, made with `ditto -c -k --keepParent` (the form
# notarytool accepts and the Finder unpacks with the bundle intact), and Framescribe-VERSION.zip.sha256 in the format
# `shasum -a 256 -c` reads. VERSION is the app's CFBundleShortVersionString. Replaces both files if they exist.
#
#   app/package_app.sh [--out DIR] APP     DIR defaults to the folder that holds APP
#
# The bundle must pass app/bundle_check.sh first (no symbolic link at its fixed places, no control character in a
# path, every link inside it, CFBundleName and the version plain names), so the zip's name comes from checked values
# only and is written inside DIR.
#
# Prints the zip's path, its size and its SHA-256.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=app/bundle_check.sh
source "$HERE/bundle_check.sh"

die() { printf 'package_app.sh: %s\n' "$*" >&2; exit 1; }

main() {
    local out="" app=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --out) [[ $# -ge 2 ]] || die "--out needs a folder"; out="$2"; shift ;;
            -h|--help) sed -n '2,/^set -euo pipefail$/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
            -*) die "unknown option: $1 (see --help)" ;;
            *) [[ -z "$app" ]] || die "one app at a time"; app="$1" ;;
        esac
        shift
    done
    [[ -n "$app" ]] || die "usage: package_app.sh [--out DIR] APP (an app bundle)"
    physical_bundle "$app" || die "refused, nothing was written: $BUNDLE_PROBLEM"
    app="$BUNDLE_PATH"
    check_bundle "$app" || die "refused, nothing was written: $BUNDLE_PROBLEM"
    [[ -n "$out" ]] || out="$(dirname "$app")"
    mkdir -p "$out"
    out="$(cd "$out" && pwd)"

    local version name zip
    version="$(plutil -extract CFBundleShortVersionString raw -o - "$app/Contents/Info.plist")"
    name="$(plutil -extract CFBundleName raw -o - "$app/Contents/Info.plist")"
    [[ "$version" =~ ^[0-9]+(\.[0-9]+)*([a-z0-9.+-]*)$ ]] || die "unexpected version in Info.plist: $version"
    # both checked by check_bundle against ^[A-Za-z0-9][A-Za-z0-9._-]*$: a plain file name, no slash
    [[ "$name" =~ $PLIST_VALUE_PATTERN && "$version" =~ $PLIST_VALUE_PATTERN ]] || die "unexpected CFBundleName or version"
    zip="$name-$version.zip"
    [[ "$zip" != */* ]] || die "unexpected zip name: $zip"
    rm -f "$out/$zip" "$out/$zip.sha256"
    # --keepParent: the archive holds Framescribe.app itself, not its contents
    ditto -c -k --keepParent "$app" "$out/$zip"
    (cd "$out" && shasum -a 256 "$zip" > "$zip.sha256")
    printf '%s\n' "$out/$zip"
    printf 'size: %s bytes (%s MB)\n' "$(stat -f %z "$out/$zip")" "$(( $(stat -f %z "$out/$zip") / 1000000 ))"
    printf 'sha256: %s\n' "$(cut -d' ' -f1 "$out/$zip.sha256")"
}

main "$@"
