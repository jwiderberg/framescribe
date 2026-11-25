#!/usr/bin/env bash
# The structure check of a Framescribe.app bundle and the enumeration of its files, shared by app/sign_app.sh,
# app/preflight_app.sh, app/package_app.sh and app/build_app.sh (which source this file) and the release workflow
# (which runs it on the unpacked artifact before any secret is used). Reads the bundle only.
#
#   app/bundle_check.sh APP              check one bundle; exit 0 when it passes, 1 with the reason otherwise
#   app/bundle_check.sh --unpacked DIR   DIR must hold exactly one entry, the directory Framescribe.app, which is then
#                                        checked like APP
#
# The check: APP is a directory called Framescribe.app, not a symbolic link, and so are Contents, Contents/MacOS,
# Contents/Resources, Contents/Resources/runtime, runtime/bin, runtime/lib and runtime/lib/python3.12, and
# Contents/Info.plist and runtime/bin/python3.12 are regular files. No path in the bundle holds a newline or another
# control character (C0, DEL or C1). Every entry is a directory, a regular file or a symbolic link (no device, pipe or socket), no
# regular file has a second hard link, and every symbolic link resolves (realpath) to an existing entry inside the
# bundle. CFBundleName, CFBundleExecutable and CFBundleShortVersionString match ^[A-Za-z0-9][A-Za-z0-9._-]*$ and
# CFBundleIdentifier is io.github.jwiderberg.framescribe.
#
# Mach-O files are found by their content, never by their name: macho_by_magic reads the first four bytes of every
# regular file, macho_by_file asks `file` about every regular file. The two must agree.
set -euo pipefail

BUNDLE_NAME="Framescribe.app"
RUNTIME_PYTHON="Contents/Resources/runtime/bin/python3.12"  # the embedded Python, relative to the bundle
EXPECTED_BUNDLE_ID="io.github.jwiderberg.framescribe"
PLIST_VALUE_PATTERN='^[A-Za-z0-9][A-Za-z0-9._-]*$'
BUNDLE_PROBLEM=""

bundle_refuse() { BUNDLE_PROBLEM="$*"; return 1; }

has_control_char() {  # TEXT: true when it holds a newline, a tab or any other C0 control character, DEL, or a C1
    local LC_ALL=C   # control character (U+0080 to U+009F, in UTF-8 the bytes C2 80 to C2 9F)
    [[ "$1" == *[[:cntrl:]]* || "$1" == *$'\xc2'[$'\x80'-$'\x9f']* ]]
}

# check_bundle_enumerable APP: what must hold before any path of the bundle is listed, signed or packed. On a
# failure BUNDLE_PROBLEM says why. APP must be an absolute, physical path (cd APP && pwd -P).
check_bundle_enumerable() {
    local app="$1" rel p
    [[ "$(basename "$app")" == "$BUNDLE_NAME" ]] || { bundle_refuse "the bundle must be called $BUNDLE_NAME: $app"; return 1; }
    for rel in "" Contents Contents/MacOS Contents/Resources Contents/Resources/runtime Contents/Resources/runtime/bin \
               Contents/Resources/runtime/lib Contents/Resources/runtime/lib/python3.12; do
        p="$app${rel:+/$rel}"
        if [[ -L "$p" ]]; then bundle_refuse "${rel:-the bundle} is a symbolic link"; return 1; fi
        if [[ ! -d "$p" ]]; then bundle_refuse "${rel:-the bundle} is not a directory"; return 1; fi
    done
    for rel in Contents/Info.plist "$RUNTIME_PYTHON"; do
        p="$app/$rel"
        if [[ -L "$p" || ! -f "$p" ]]; then bundle_refuse "$rel is not a regular file"; return 1; fi
    done
    # find without -L never follows a link; a name with a newline is refused before any other use of it
    while IFS= read -r -d '' p; do
        if has_control_char "$p"; then
            bundle_refuse "a path holds a newline or another control character: $(printf '%q' "${p#"$app"/}")"; return 1
        fi
        if [[ ! -L "$p" && ! -f "$p" && ! -d "$p" ]]; then
            bundle_refuse "not a file, directory or symbolic link: ${p#"$app"/}"; return 1
        fi
    done < <(find "$app" -print0)
    check_plist_values "$app/Contents/Info.plist"
}

check_plist_values() {  # INFO.PLIST
    local key value
    for key in CFBundleName CFBundleExecutable CFBundleShortVersionString CFBundleIdentifier; do
        value="$(plutil -extract "$key" raw -o - "$1" 2>/dev/null)" || { bundle_refuse "Info.plist has no $key"; return 1; }
        if [[ ! "$value" =~ $PLIST_VALUE_PATTERN ]]; then
            bundle_refuse "Info.plist: $key is not of the form ${PLIST_VALUE_PATTERN}: $(printf '%q' "$value")"; return 1
        fi
    done
    value="$(plutil -extract CFBundleIdentifier raw -o - "$1")"
    [[ "$value" == "$EXPECTED_BUNDLE_ID" ]] || { bundle_refuse "CFBundleIdentifier is $value, expected $EXPECTED_BUNDLE_ID"; return 1; }
}

# check_bundle_links APP: no hard-linked regular file, every symbolic link resolves to an existing entry inside APP.
check_bundle_links() {
    local app="$1" l resolved
    command -v realpath >/dev/null || { bundle_refuse "realpath not found (macOS 13 or later has /bin/realpath)"; return 1; }
    l="$(find "$app" -type f -links +1 -print -quit)"
    if [[ -n "$l" ]]; then bundle_refuse "a file has more than one hard link: ${l#"$app"/}"; return 1; fi
    while IFS= read -r -d '' l; do
        if ! resolved="$(realpath -q "$l")"; then bundle_refuse "a symbolic link points nowhere: ${l#"$app"/}"; return 1; fi
        if [[ "$resolved" != "$app"/* ]]; then
            bundle_refuse "a symbolic link points outside the bundle: ${l#"$app"/} -> $(readlink "$l")"; return 1
        fi
    done < <(find "$app" -type l -print0)
}

# check_bundle APP: both of the above. On success prints nothing; on a failure BUNDLE_PROBLEM says why.
check_bundle() {
    check_bundle_enumerable "$1" && check_bundle_links "$1"
}

# physical_bundle PATH: the absolute path of the bundle with no symbolic link in it, refusing a link at PATH itself
physical_bundle() {
    [[ -n "$1" && ! -L "$1" && -d "$1" ]] || { bundle_refuse "not a directory (or a symbolic link): $1"; return 1; }
    (cd "$1" && pwd -P)
}

# ---- finding Mach-O files by content --------------------------------------------------------------------------------

# macho_by_magic APP: every regular file whose first four bytes are a Mach-O or universal (fat) magic number, one
# absolute path per line, sorted. Reading with the shell's own read: no process per file.
macho_by_magic() {
    local f m LC_ALL=C
    while IFS= read -r -d '' f; do
        m=""
        IFS= read -r -n 4 -d '' m < "$f" || true
        case "$m" in
            $'\xcf\xfa\xed\xfe' | $'\xce\xfa\xed\xfe' | $'\xfe\xed\xfa\xcf' | $'\xfe\xed\xfa\xce' \
            | $'\xca\xfe\xba\xbe' | $'\xca\xfe\xba\xbf' | $'\xbe\xba\xfe\xca' | $'\xbf\xba\xfe\xca')
                printf '%s\n' "$f" ;;
        esac
    done < <(find "$1" -type f -print0)
}

# macho_by_file APP: every regular file that `file` calls Mach-O, one absolute path per line. Independent of
# macho_by_magic. `file --print0` ends each name with a NUL; the extra lines it prints for each architecture of a
# universal binary carry no NUL, so after the first NUL each chunk is "DESCRIPTION\n[...\n]NEXT NAME". Names hold no
# control character (check_bundle_enumerable), so the next name is what follows the last newline.
macho_by_file() {
    local chunk name="" first=1 desc
    while IFS= read -r -d '' chunk; do
        if [[ $first -eq 1 ]]; then name="$chunk"; first=0; continue; fi
        desc="${chunk%%$'\n'*}"
        desc="${desc#:}"
        desc="${desc#"${desc%%[! ]*}"}"
        if [[ "$desc" == Mach-O* ]]; then printf '%s\n' "$name"; fi
        if [[ "$chunk" == *$'\n'* ]]; then name="${chunk##*$'\n'}"; else name=""; fi
    done < <(find "$1" -type f -print0 | xargs -0 file --print0 --no-pad --)
}

# ar_by_magic APP: every regular file that starts like an ar archive (a static library), whatever its name
ar_by_magic() {
    local f m LC_ALL=C
    while IFS= read -r -d '' f; do
        m=""
        IFS= read -r -n 8 -d '' m < "$f" || true
        if [[ "$m" == '!<arch>'$'\n' ]]; then printf '%s\n' "$f"; fi
    done < <(find "$1" -type f -print0)
}

# ---- reading a signature -------------------------------------------------------------------------------------------

# entitlement_names FILE: the entitlements of a signed file, or of an .entitlements plist, one name per line, sorted.
# Every value is a boolean true; anything else is named with its value, so that it cannot compare equal.
entitlement_names() {
    local json
    if [[ "$1" == *.entitlements ]]; then json="$(plutil -convert json -o - "$1")"
    else json="$(codesign -d --entitlements - --xml "$1" 2>/dev/null | plutil -convert json -o - - 2>/dev/null || true)"; fi
    [[ -n "$json" ]] || return 0
    tr -d '{}\n ' <<< "$json" | tr ',' '\n' \
        | awk -F: 'NF { k = $1; gsub(/"/, "", k); print ($2 == "true") ? k : k "=" $2 }' | LC_ALL=C sort
}

# devid_team: the output of `codesign -dvv FILE` on stdin; prints the Team ID when the signature's authority chain is
# exactly "Developer ID Application: NAME (TEAMID)", "Developer ID Certification Authority", "Apple Root CA" and
# TeamIdentifier names the same TEAMID. Prints nothing and returns 1 otherwise.
devid_team() {
    local line authorities=() team="" leaf_team
    while IFS= read -r line; do
        case "$line" in
            Authority=*) authorities+=("${line#Authority=}") ;;
            TeamIdentifier=*) team="${line#TeamIdentifier=}" ;;
        esac
    done
    [[ ${#authorities[@]} -eq 3 ]] || return 1
    [[ "${authorities[1]}" == "Developer ID Certification Authority" && "${authorities[2]}" == "Apple Root CA" ]] || return 1
    [[ "${authorities[0]}" =~ ^Developer\ ID\ Application:\ .+\ \(([A-Z0-9]{10})\)$ ]] || return 1
    leaf_team="${BASH_REMATCH[1]}"
    [[ "$team" == "$leaf_team" ]] || return 1
    printf '%s\n' "$team"
}

# ---- run as a command ----------------------------------------------------------------------------------------------

bundle_check_main() {
    local app="" unpacked=""
    case "${1:-}" in
        -h|--help) sed -n '2,/^set -euo pipefail$/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; return 0 ;;
        --unpacked) [[ $# -eq 2 ]] || { echo "usage: bundle_check.sh --unpacked DIR" >&2; return 2; }; unpacked="$2" ;;
        ""|-*) echo "usage: bundle_check.sh APP | --unpacked DIR" >&2; return 2 ;;
        *) [[ $# -eq 1 ]] || { echo "usage: bundle_check.sh APP" >&2; return 2; }; app="$1" ;;
    esac
    if [[ -n "$unpacked" ]]; then
        local dir entries=()
        dir="$(physical_bundle "$unpacked")" || { echo "bundle_check.sh: $BUNDLE_PROBLEM" >&2; return 1; }
        while IFS= read -r -d '' e; do entries+=("$e"); done < <(find "$dir" -mindepth 1 -maxdepth 1 -print0)
        if [[ ${#entries[@]} -ne 1 || "${entries[0]}" != "$dir/$BUNDLE_NAME" ]]; then
            echo "bundle_check.sh: $unpacked must hold exactly one entry, $BUNDLE_NAME (found ${#entries[@]})" >&2
            return 1
        fi
        app="$dir/$BUNDLE_NAME"
    fi
    app="$(physical_bundle "$app")" || { echo "bundle_check.sh: $BUNDLE_PROBLEM" >&2; return 1; }
    if ! check_bundle "$app"; then echo "bundle_check.sh: refused: $BUNDLE_PROBLEM" >&2; return 1; fi
    local by_magic by_file
    by_magic="$(macho_by_magic "$app" | LC_ALL=C sort)"
    by_file="$(macho_by_file "$app" | LC_ALL=C sort)"
    if [[ "$by_magic" != "$by_file" ]]; then
        echo "bundle_check.sh: refused: the two ways of finding Mach-O files disagree:" >&2
        diff <(printf '%s\n' "$by_magic") <(printf '%s\n' "$by_file") | sed "s|$app/||" | head -10 >&2 || true
        return 1
    fi
    printf 'bundle_check.sh: %s passes the structure check (%s Mach-O files)\n' "$app" "$(grep -c . <<< "$by_magic" || true)"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then bundle_check_main "$@"; fi
