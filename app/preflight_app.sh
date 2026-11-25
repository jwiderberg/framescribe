#!/usr/bin/env bash
# Checks a built Framescribe.app for what Apple's notary service rejects, before uploading it. Prints one line per
# check (PASS, FAIL or SKIP) and exits 1 if any check fails. Reads the bundle only.
#
#   app/preflight_app.sh APP                 the release gate: every Mach-O file signed with one Developer ID
#                                            Application identity (one Team ID), a secure timestamp and the hardened
#                                            runtime
#   app/preflight_app.sh --allow-adhoc APP   for a `build_app.sh --hardened` test build: ad-hoc signatures with the
#                                            hardened runtime pass; the identity, Team ID and timestamp checks are
#                                            skipped (an ad-hoc signature has none of them)
#
# First the structure check of app/bundle_check.sh (a real Framescribe.app, no symbolic link at its fixed places, no
# path with a control character, plain Info.plist values): a bundle that fails it is refused before anything else.
# Mach-O files are found by their content, whatever their name, twice: by their first bytes and by `file`. The two
# lists must be the same.
#
# Checked: every Mach-O file (in Contents/MacOS and in Contents/Resources alike) has a valid signature of its own and
# the hardened runtime flag; with a Developer ID, every one has the authority chain Developer ID Application,
# Developer ID Certification Authority, Apple Root CA, a leaf certificate whose Team ID is the signature's
# TeamIdentifier, the same Team ID on all of them and a secure timestamp; no file asks for
# com.apple.security.get-task-allow; the Python executable carries exactly
# the entitlements of app/Support/runtime.entitlements (runtime-adhoc-test.entitlements with --allow-adhoc) and every
# other Mach-O file, the app's executable included, none; every Mach-O file was built
# against the macOS 10.9 SDK or newer; Contents/MacOS holds Mach-O files only; no static library (.a or ar archive);
# no symbolic link that points outside the bundle or nowhere; no file with a second hard link; no quarantine, Finder
# information or resource fork
# attribute; `codesign --verify --deep --strict` passes on the whole bundle.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=app/bundle_check.sh
source "$HERE/bundle_check.sh"

main() {
    local allow_adhoc=0 app=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --allow-adhoc) allow_adhoc=1 ;;
            -h|--help) sed -n '2,/^set -euo pipefail$/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
            -*) die "unknown option: $1 (see --help)" ;;
            *) [[ -z "$app" ]] || die "one bundle at a time"; app="$1" ;;
        esac
        shift
    done
    [[ -n "$app" ]] || die "usage: preflight_app.sh [--allow-adhoc] APP"
    [[ -d "$app/Contents" ]] || die "not an app bundle: $app"
    APP="$(physical_bundle "$app")" || die "$BUNDLE_PROBLEM"
    SUPPORT="$HERE/Support"
    TMP="$(mktemp -d "${TMPDIR:-/tmp}/framescribe-preflight.XXXXXX")"
    trap 'rm -rf "$TMP"' EXIT
    FAILED=0

    printf 'preflight of %s (%s)\n' "$APP" "$([[ $allow_adhoc -eq 1 ]] && echo 'ad-hoc test build' || echo 'release gate')"
    if ! check_bundle_enumerable "$APP"; then
        fail "bundle structure: $BUNDLE_PROBLEM"
        printf 'RESULT: refused before any other check\n'
        exit 1
    fi
    pass "bundle structure: $BUNDLE_NAME, no symbolic link at its fixed places, no control character in a path, Info.plist values"
    list_macho
    check_signatures "$allow_adhoc"
    check_entitlements "$allow_adhoc"
    check_sdk
    check_macos_folder
    check_static_libraries
    check_symlinks
    check_hardlinks
    check_attributes
    check_deep_verify
    if [[ $FAILED -gt 0 ]]; then printf 'RESULT: %d check(s) failed\n' "$FAILED"; exit 1; fi
    printf 'RESULT: every check passed\n'
}

die() { printf 'preflight_app.sh: %s\n' "$*" >&2; exit 2; }
pass() { printf 'PASS  %s\n' "$*"; }
skip() { printf 'SKIP  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; FAILED=$((FAILED + 1)); }
detail() {  # up to five offending paths, relative to the bundle, from a file
    local n
    n="$(wc -l < "$1" | tr -d ' ')"
    sed -n '1,5p' "$1" | sed "s|$APP/||; s/^/        /"
    if [[ $n -gt 5 ]]; then printf '        ... and %d more\n' $((n - 5)); fi
}

list_macho() {  # every regular file, by content: its first bytes, and independently what `file` says
    MACHO="$TMP/macho.txt"
    local second="$TMP/macho-file.txt" n_second
    macho_by_magic "$APP" | LC_ALL=C sort > "$MACHO"
    macho_by_file "$APP" | LC_ALL=C sort > "$second"
    N_MACHO="$(wc -l < "$MACHO" | tr -d ' ')"
    n_second="$(wc -l < "$second" | tr -d ' ')"
    [[ $N_MACHO -gt 0 ]] || die "no Mach-O file in $APP"
    if cmp -s "$MACHO" "$second"; then
        pass "Mach-O files: $N_MACHO, found by content (first bytes), the same $n_second that file(1) finds"
    else
        LC_ALL=C comm -3 "$MACHO" "$second" | sed 's/^[[:space:]]*//' > "$TMP/disagree"
        fail "Mach-O files: $N_MACHO by their first bytes, $n_second by file(1), the lists differ"; detail "$TMP/disagree"
        LC_ALL=C sort -u "$MACHO" "$second" -o "$MACHO"  # check every file either method found
        N_MACHO="$(wc -l < "$MACHO" | tr -d ' ')"
    fi
}

check_signatures() {
    local allow_adhoc="$1" f info flags unsigned="$TMP/unsigned" noruntime="$TMP/noruntime" adhoc="$TMP/adhoc"
    local notdevid="$TMP/notdevid" nostamp="$TMP/nostamp" teams="$TMP/teams" team
    : > "$unsigned"; : > "$noruntime"; : > "$adhoc"; : > "$notdevid"; : > "$nostamp"; : > "$teams"
    while IFS= read -r f; do
        if ! codesign --verify --strict "$f" 2>/dev/null || ! info="$(codesign -d --verbose=2 "$f" 2>&1)"; then
            printf '%s\n' "$f" >> "$unsigned"; continue
        fi
        flags="$(sed -nE 's/^CodeDirectory .*flags=(0x[0-9a-f]+).*/\1/p' <<< "$info")"
        if [[ -z "$flags" ]] || (( (flags & 0x10000) == 0 )); then printf '%s\n' "$f" >> "$noruntime"; fi
        if grep -q '^Signature=adhoc' <<< "$info"; then
            printf '%s\n' "$f" >> "$adhoc"
            printf 'none\n' >> "$teams"
        else
            # the leaf certificate's Team ID, read from the authority chain (devid_team in app/bundle_check.sh)
            if team="$(devid_team <<< "$info")"; then printf '%s\n' "$team" >> "$teams"
            else printf '%s\n' "$f" >> "$notdevid"; printf 'none\n' >> "$teams"; fi
            grep -q '^Timestamp=' <<< "$info" || printf '%s\n' "$f" >> "$nostamp"
        fi
    done < "$MACHO"

    if [[ -s "$unsigned" ]]; then fail "signature: $(wc -l < "$unsigned" | tr -d ' ') of $N_MACHO Mach-O files are unsigned or do not verify"; detail "$unsigned"
    else pass "signature: all $N_MACHO Mach-O files carry a valid signature of their own (codesign --verify --strict)"; fi

    if [[ -s "$noruntime" ]]; then fail "hardened runtime: $(wc -l < "$noruntime" | tr -d ' ') Mach-O files lack the runtime flag"; detail "$noruntime"
    else pass "hardened runtime: all $N_MACHO Mach-O files have the runtime flag (0x10000)"; fi

    local n_adhoc n_signed
    n_adhoc="$(wc -l < "$adhoc" | tr -d ' ')"
    n_signed=$((N_MACHO - $(wc -l < "$unsigned" | tr -d ' ')))
    if [[ $allow_adhoc -eq 1 ]]; then
        if [[ $n_adhoc -eq $n_signed ]]; then
            pass "ad-hoc test build: every signed Mach-O file is signed ad hoc"
        else
            fail "ad-hoc test build: $((n_signed - n_adhoc)) Mach-O files carry a non-ad-hoc signature"
        fi
        skip "Developer ID identity, Team ID, secure timestamp (an ad-hoc signature has none; notarization would refuse this build)"
        return
    fi
    if [[ -s "$adhoc" ]]; then fail "Developer ID: $n_adhoc Mach-O files are signed ad hoc"; detail "$adhoc"
    elif [[ -s "$notdevid" ]]; then fail "Developer ID: $(wc -l < "$notdevid" | tr -d ' ') Mach-O files lack the chain Developer ID Application > Developer ID Certification Authority > Apple Root CA with a matching TeamIdentifier"; detail "$notdevid"
    else pass "Developer ID: every Mach-O file is signed by a Developer ID Application certificate (chain up to Apple Root CA, leaf Team ID = TeamIdentifier)"; fi

    if [[ -s "$adhoc" || -s "$nostamp" ]]; then fail "secure timestamp: $(cat "$adhoc" "$nostamp" | sort -u | wc -l | tr -d ' ') Mach-O files have none"; detail "$nostamp"
    else pass "secure timestamp: every Mach-O file has one"; fi

    local distinct
    distinct="$(LC_ALL=C sort -u "$teams" | tr '\n' ' ' | sed 's/ $//')"
    if [[ "$distinct" =~ ^[A-Z0-9]{10}$ ]]; then
        pass "Team ID: one for all $N_MACHO Mach-O files ($distinct)"
    else
        fail "Team ID: expected one, found: ${distinct:-none}"
    fi
}

check_entitlements() {
    local f gta="$TMP/gta" python="$APP/Contents/Resources/runtime/bin/python3.12" main_exe expected
    expected="$SUPPORT/runtime.entitlements"
    if [[ "$1" -eq 1 ]]; then expected="$SUPPORT/runtime-adhoc-test.entitlements"; fi
    local others="$TMP/others" xml
    : > "$gta"; : > "$others"
    while IFS= read -r f; do
        xml="$(codesign -d --entitlements - --xml "$f" 2>/dev/null || true)"
        if grep -q 'com.apple.security.get-task-allow' <<< "$xml"; then printf '%s\n' "$f" >> "$gta"; fi
        # only the Python executable may carry entitlements (the app's executable is checked for none below)
        if [[ "$f" != "$python" && -n "$(entitlement_names "$f")" ]]; then printf '%s\n' "$f" >> "$others"; fi
    done < "$MACHO"
    if [[ -s "$gta" ]]; then fail "get-task-allow: $(wc -l < "$gta" | tr -d ' ') files ask for it (notarization refuses it)"; detail "$gta"
    else pass "get-task-allow: no file asks for it"; fi
    if [[ -s "$others" ]]; then fail "entitlements: $(wc -l < "$others" | tr -d ' ') Mach-O files other than runtime/bin/python3.12 carry some"; detail "$others"
    else pass "entitlements: no Mach-O file but runtime/bin/python3.12 carries any"; fi

    local want have
    want="$(entitlement_names "$expected" | tr '\n' ' ' | sed 's/ $//')"
    have="$(entitlement_names "$python" | tr '\n' ' ' | sed 's/ $//')"
    if [[ "$want" == "$have" ]]; then pass "entitlements of runtime/bin/python3.12: exactly ${want:-none} ($(basename "$expected"))"
    else fail "entitlements of runtime/bin/python3.12: ${have:-none}, expected ${want:-none} ($(basename "$expected"))"; fi

    main_exe="$APP/Contents/MacOS/$(plutil -extract CFBundleExecutable raw -o - "$APP/Contents/Info.plist")"
    have="$(entitlement_names "$main_exe" | tr '\n' ' ' | sed 's/ $//')"
    if [[ -z "$have" ]]; then pass "entitlements of the app's executable: none"
    else fail "entitlements of the app's executable: $have, expected none"; fi
}

check_sdk() {
    local f sdk bad="$TMP/oldsdk" lowest="" lowest_n=999999 n
    : > "$bad"
    while IFS= read -r f; do
        sdk="$(vtool -show-build "$f" 2>/dev/null | awk '$1 == "sdk" {print $2; exit}')"
        if [[ -z "$sdk" || "$sdk" == "n/a" ]]; then printf '%s (no SDK version)\n' "$f" >> "$bad"; continue; fi
        n="$(version_number "$sdk")"
        if (( n < lowest_n )); then lowest_n=$n; lowest="$sdk"; fi
        if (( n < 100900 )); then printf '%s (SDK %s)\n' "$f" "$sdk" >> "$bad"; fi
    done < "$MACHO"
    if [[ -s "$bad" ]]; then fail "SDK: $(wc -l < "$bad" | tr -d ' ') Mach-O files were built against an SDK older than macOS 10.9"; detail "$bad"
    else pass "SDK: every Mach-O file was built against macOS 10.9 or newer (oldest: $lowest)"; fi
}

version_number() {  # 15.0 -> 150000, 10.9.5 -> 100905
    local IFS=. a b c
    read -r a b c <<< "$1"
    echo $(( ${a:-0} * 10000 + ${b:-0} * 100 + ${c:-0} ))
}

check_macos_folder() {
    local f bad="$TMP/notmacho"
    : > "$bad"
    while IFS= read -r -d '' f; do
        grep -qxF "$f" "$MACHO" || printf '%s\n' "$f" >> "$bad"
    done < <(find "$APP/Contents/MacOS" -mindepth 1 -print0)
    if [[ -s "$bad" ]]; then fail "Contents/MacOS: $(wc -l < "$bad" | tr -d ' ') entries are not Mach-O files"; detail "$bad"
    else pass "Contents/MacOS: Mach-O files only ($(find "$APP/Contents/MacOS" -mindepth 1 | wc -l | tr -d ' '))"; fi
    local in_resources
    in_resources="$(grep -c "^$APP/Contents/Resources/" "$MACHO" || true)"
    pass "code in Contents/Resources: $in_resources Mach-O files, each signed (checked above)"
}

check_static_libraries() {
    local bad="$TMP/archives"
    { find "$APP" -type f -name '*.a'
      ar_by_magic "$APP"
    } | LC_ALL=C sort -u > "$bad"
    if [[ -s "$bad" ]]; then fail "static libraries: $(wc -l < "$bad" | tr -d ' ') found"; detail "$bad"
    else pass "static libraries: none (.a or ar archive)"; fi
}

check_symlinks() {
    local l target dir resolved bad="$TMP/links" n=0
    : > "$bad"
    while IFS= read -r -d '' l; do
        n=$((n + 1))
        target="$(readlink "$l")"
        if [[ "$target" == /* ]]; then printf '%s -> %s (absolute)\n' "$l" "$target" >> "$bad"; continue; fi
        dir="$(dirname "$l")"
        if ! resolved="$(cd "$dir" && cd "$(dirname "$target")" 2>/dev/null && pwd -P)"; then
            printf '%s -> %s (points nowhere)\n' "$l" "$target" >> "$bad"; continue
        fi
        resolved="$resolved/$(basename "$target")"
        if [[ "$resolved" != "$(cd "$APP" && pwd -P)"/* || ! -e "$resolved" ]]; then
            printf '%s -> %s (outside the bundle or missing)\n' "$l" "$target" >> "$bad"
        fi
    done < <(find "$APP" -type l -print0)
    if [[ -s "$bad" ]]; then fail "symbolic links: $(wc -l < "$bad" | tr -d ' ') point outside the bundle or nowhere"; detail "$bad"
    else pass "symbolic links: $n, none points outside the bundle"; fi
}

check_hardlinks() {
    local bad="$TMP/hardlinks"
    find "$APP" -type f -links +1 > "$bad"
    if [[ -s "$bad" ]]; then fail "hard links: $(wc -l < "$bad" | tr -d ' ') files have more than one"; detail "$bad"
    else pass "hard links: none"; fi
}

check_attributes() {
    local bad="$TMP/xattr" name
    : > "$bad"
    for name in com.apple.quarantine com.apple.FinderInfo com.apple.ResourceFork; do
        find "$APP" -xattrname "$name" | sed "s/\$/ ($name)/" >> "$bad"
    done
    if [[ -s "$bad" ]]; then fail "extended attributes: $(wc -l < "$bad" | tr -d ' ') files carry quarantine, Finder information or a resource fork (xattr -cr)"; detail "$bad"
    else pass "extended attributes: no quarantine, Finder information or resource fork"; fi
}

check_deep_verify() {
    local out="$TMP/deep"
    if codesign --verify --deep --strict "$APP" > "$out" 2>&1; then pass "codesign --verify --deep --strict on the bundle"
    else fail "codesign --verify --deep --strict on the bundle"; detail "$out"; fi
}

main "$@"
