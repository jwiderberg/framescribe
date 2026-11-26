#!/usr/bin/env bash
# Builds app/build/Framescribe.app: the SwiftUI app plus an embedded, relocatable Python 3.12 with framescribe
# and its locked dependencies. Needs only swift (Command Line Tools are enough), uv and standard macOS tools.
#
#   app/build_app.sh                      build (incremental: the runtime is rebuilt only when its inputs changed)
#   app/build_app.sh --out DIR            put the app and its work files in DIR instead of app/build
#   app/build_app.sh --min-macos VERSION  the oldest macOS the app is for (default 15.0): the wheels are chosen for it,
#                                         every Mach-O file is checked against it, Info.plist declares it
#   app/build_app.sh --clean              remove what earlier builds made first: the app, the zips, the work
#                                         folder in the output folder and app/.build (nothing else in it). A work
#                                         folder counts as the build's only when it holds the marker file
#                                         .framescribe-build (or runtime.stamp, from older builds) or is empty;
#                                         the build refuses to touch any other. The app and the zips are removed
#                                         (or replaced by a build) only in an output folder whose work folder holds
#                                         that marker: with --out /Applications an installed Framescribe.app is
#                                         refused, never deleted. app/.build is removed only next to Package.swift
#                                         in the folder of this script
#   app/build_app.sh --hardened           sign ad hoc, but with the hardened runtime and the release entitlements plus
#                                         disable-library-validation (an ad-hoc signature has no Team ID), then run
#                                         app/preflight_app.sh --allow-adhoc on it: tests locally what a Developer ID
#                                         build runs with (no secure timestamp: ad hoc has none)
#   app/build_app.sh --sign "Developer ID Application: NAME (TEAMID)"
#                                         sign with that identity, the hardened runtime and a secure timestamp instead
#                                         of ad hoc (app/sign_app.sh, which the release workflow also uses), then run
#                                         the signed runtime once and app/preflight_app.sh (fails on any error)
#   app/build_app.sh --sign "..." --keychain PATH
#                                         look for the identity in that keychain only (the release workflow's
#                                         throwaway keychain)
#   app/build_app.sh --sign "..." --notarize PROFILE
#                                         then notarize with `xcrun notarytool` (a keychain profile made with
#                                         `xcrun notarytool store-credentials PROFILE`) and staple the ticket
#   app/build_app.sh --zip                then write Framescribe-VERSION.zip and its .sha256 next to the app
#                                         (app/package_app.sh, after notarizing when --notarize is given)
#
# Downloads: a standalone CPython 3.12 build (uv python install) and the locked wheels (uv pip install), both through
# uv and its cache. Writes only to the output folder (app/build), app/.build (Swift) and uv's cache.
#
# Why 15.0: opencv-python-headless 5.1.0.95 (uv.lock) has wheels for macOS 15.0 and later only; every other locked
# package has one for 14.0. The build fails if a binary needs a newer macOS than the declared one.
set -euo pipefail

# shellcheck source=app/bundle_check.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bundle_check.sh"
WORK_MARKER=".framescribe-build"

main() {
    local clean=0 profile="" out="" zip=0
    identity="-" keychain="" HARDENED=0
    MIN_MACOS="15.0"
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --clean) clean=1 ;;
            --out) [[ $# -ge 2 ]] || die "--out needs a folder"; out="$2"; shift ;;
            --min-macos) [[ $# -ge 2 && "$2" =~ ^[0-9]+\.[0-9]+$ ]] || die "--min-macos needs a version like 15.0"
                MIN_MACOS="$2"; shift ;;
            --hardened) HARDENED=1 ;;
            --sign) [[ $# -ge 2 && -n "$2" ]] || die "--sign needs an identity"; identity="$2"; shift ;;
            --keychain) [[ $# -ge 2 && -f "$2" ]] || die "--keychain needs the path of a keychain file"; keychain="$2"; shift ;;
            --notarize) [[ $# -ge 2 ]] || die "--notarize needs a notarytool keychain profile"; profile="$2"; shift ;;
            --zip) zip=1 ;;
            -h|--help) sed -n '2,/^set -euo pipefail$/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
            *) die "unknown option: $1 (see --help)" ;;
        esac
        shift
    done
    if [[ "$identity" != "-" ]]; then HARDENED=1; fi
    if [[ -n "$profile" && "$identity" == "-" ]]; then die "--notarize needs --sign with a Developer ID identity"; fi
    if [[ -n "$keychain" && "$identity" == "-" ]]; then die "--keychain needs --sign"; fi

    APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    REPO="$(dirname "$APP_DIR")"
    BUILD="$APP_DIR/build"
    if [[ -n "$out" ]]; then mkdir -p "$out"; BUILD="$(cd "$out" && pwd)"; fi
    WORK="$BUILD/work"
    APP="$BUILD/Framescribe.app"
    PY_SERIES="3.12"

    check_tools
    check_work_folder
    check_output_folder
    if [[ $clean -eq 1 ]]; then
        # only what this script makes, never the folder itself: --out may name any folder, even the checkout. The
        # app and the zips only where an earlier build left its marker (check_output_folder refused otherwise)
        step "clean: removing $WORK, $APP, the zips next to it and $APP_DIR/.build"
        if [[ $OUT_IS_OURS -eq 1 ]]; then
            rm -rf "$WORK" "$APP"
            rm -f "$BUILD"/Framescribe-*.zip "$BUILD"/Framescribe-*.zip.sha256
        elif [[ -d "$WORK" && ! -L "$WORK" ]]; then
            rmdir "$WORK"  # empty (check_work_folder): nothing of anyone's is in it
        fi
        if [[ -f "$APP_DIR/Package.swift" && -d "$APP_DIR/.build" && ! -L "$APP_DIR/.build" ]]; then
            rm -rf "$APP_DIR/.build"
        elif [[ -e "$APP_DIR/.build" || -L "$APP_DIR/.build" ]]; then
            die "$APP_DIR/.build is not the Swift build folder of this checkout (no Package.swift next to it, or a symbolic link); refusing to remove it"
        fi
    fi
    mkdir -p "$WORK"
    : > "$WORK/$WORK_MARKER"
    VERSION="$(sed -n 's/^version = "\(.*\)"$/\1/p' "$REPO/pyproject.toml" | head -1)"
    [[ -n "$VERSION" ]] || die "no version in pyproject.toml"
    step "framescribe $VERSION"

    step "deployment target: macOS $MIN_MACOS"
    build_swift
    build_runtime
    assemble_bundle
    check_min_macos
    sign_bundle
    if [[ $HARDENED -eq 1 ]]; then preflight; fi
    if [[ -n "$profile" ]]; then notarize "$profile"; fi
    if [[ $zip -eq 1 ]]; then
        step "zip (app/package_app.sh)"
        "$APP_DIR/package_app.sh" --out "$BUILD" "$APP" | sed 's/^/    /'
    fi
    report
}

step() { printf '==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die() { printf 'build_app.sh: %s\n' "$*" >&2; exit 1; }

check_work_folder() {
    # the work folder is this script's only when an earlier run marked it (runtime.stamp: before the marker
    # existed) or it is empty; any other folder of that name is left alone, with or without --clean
    if [[ -L "$WORK" ]]; then die "$WORK is a symbolic link; refusing to build into it"; fi
    if [[ -e "$WORK" && ! -d "$WORK" ]]; then die "$WORK exists and is not a folder; refusing to build into it"; fi
    if [[ -d "$WORK" && ! -f "$WORK/$WORK_MARKER" && ! -f "$WORK/runtime.stamp" ]] \
        && [[ -n "$(find "$WORK" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
        die "$WORK was not made by build_app.sh (no $WORK_MARKER in it); move it away or choose another --out"
    fi
}

# The output folder is this script's when its work folder carries the marker an earlier build wrote (runtime.stamp:
# older builds). Only then may a Framescribe.app or a Framescribe-*.zip in it be removed or replaced: --out may name
# /Applications or any other folder, and what is there already belongs to someone else.
check_output_folder() {
    OUT_IS_OURS=0
    if [[ -d "$WORK" && ! -L "$WORK" ]] && [[ -f "$WORK/$WORK_MARKER" || -f "$WORK/runtime.stamp" ]]; then
        OUT_IS_OURS=1; return 0
    fi
    local theirs=""
    if [[ -e "$APP" || -L "$APP" ]]; then theirs="$APP"
    elif [[ -d "$BUILD" ]]; then  # a fresh checkout has no output folder yet
        theirs="$(find "$BUILD" -mindepth 1 -maxdepth 1 -name 'Framescribe-*.zip*' -print -quit)"
    fi
    if [[ -n "$theirs" ]]; then
        die "$theirs was not made by build_app.sh here ($WORK has no $WORK_MARKER); refusing to remove or replace it. Move it away or choose another --out"
    fi
}

check_tools() {
    [[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]] || die "needs macOS on Apple Silicon (arm64)"
    local t
    for t in swift uv codesign iconutil sips plutil ditto otool file shasum strip vtool; do
        command -v "$t" >/dev/null || die "$t not found (swift: xcode-select --install; uv: https://docs.astral.sh/uv/)"
    done
}

# ---- the Swift executable ------------------------------------------------------------------------------------------

build_swift() {
    step "swift build -c release (product Framescribe)"
    local flags=(--package-path "$APP_DIR" -c release --arch arm64)
    swift build "${flags[@]}" --product Framescribe
    SWIFT_BIN="$(swift build "${flags[@]}" --show-bin-path)/Framescribe"
    [[ -x "$SWIFT_BIN" ]] || die "no executable at $SWIFT_BIN"
}

# ---- the embedded Python runtime ------------------------------------------------------------------------------------

runtime_stamp() {
    # everything the runtime is made from; a change rebuilds it
    {
        printf 'macOS %s\n' "$MIN_MACOS"
        cat "$REPO/uv.lock" "$REPO/pyproject.toml" "$REPO/README.md" "$REPO/LICENSE" \
            "$APP_DIR/Support/framescribe_app_launcher.py" "${BASH_SOURCE[0]}"
        find "$REPO/src" -type f -name '*.py' | LC_ALL=C sort | while read -r f; do printf '%s\n' "${f#"$REPO"/}"; cat "$f"; done
    } | shasum -a 256 | cut -d' ' -f1
}

build_runtime() {
    local stamp runtime="$WORK/runtime"
    stamp="$(runtime_stamp)"
    if [[ -x "$runtime/bin/python$PY_SERIES" && -f "$WORK/runtime.stamp" && "$(cat "$WORK/runtime.stamp")" == "$stamp" ]]; then
        step "embedded runtime: up to date ($WORK/runtime)"
        return
    fi

    step "standalone CPython $PY_SERIES (uv python install, into $WORK/python)"
    uv python install "$PY_SERIES" --install-dir "$WORK/python" --no-bin
    local base
    base="$(find "$WORK/python" -maxdepth 1 -type d -name "cpython-$PY_SERIES.*-macos-aarch64-none" | LC_ALL=C sort | tail -1)"
    [[ -n "$base" && -x "$base/bin/python$PY_SERIES" ]] || die "no CPython $PY_SERIES under $WORK/python"
    note "$(basename "$base")"

    step "copy it to $runtime"
    rm -rf "$runtime" "$WORK/runtime.stamp"
    cp -R "$base" "$runtime"
    local py="$runtime/bin/python$PY_SERIES"
    # bin/python3.12 is linked statically; the shared library is for programs that embed Python, and uv set its
    # install name to the path above
    rm -f "$runtime/lib/libpython$PY_SERIES.dylib"
    # uv writes its install path into the build-time configuration; give it back the standalone build's prefix
    sed -i '' "s|$base|/install|g" "$runtime/lib/python$PY_SERIES/_sysconfigdata__darwin_darwin.py"

    step "locked dependencies for macOS $MIN_MACOS (uv export --locked --no-dev, then uv pip install --require-hashes)"
    uv export --project "$REPO" --locked --no-dev --no-emit-project --no-header --format requirements-txt \
        --output-file "$WORK/requirements.txt" >/dev/null
    note "$(grep -c '^[a-z]' "$WORK/requirements.txt") pinned packages"
    # without a target platform uv picks the wheels for the macOS this build runs on (mlx: macosx_26_0);
    # --python-platform with MACOSX_DEPLOYMENT_TARGET picks the newest wheel tagged for MIN_MACOS or older, and
    # --only-binary keeps a package without such a wheel from being built from source here
    MACOSX_DEPLOYMENT_TARGET="$MIN_MACOS" uv pip install --python "$py" --break-system-packages --require-hashes \
        --no-deps --only-binary :all: --python-platform aarch64-apple-darwin -r "$WORK/requirements.txt" \
        || die "no wheels for macOS $MIN_MACOS of every locked package (see above); try a higher --min-macos"

    step "framescribe itself (uv build --wheel; installed non-editable)"
    rm -rf "$WORK/dist"
    uv build --wheel --out-dir "$WORK/dist" "$REPO"
    local wheel
    wheel="$(find "$WORK/dist" -name 'framescribe-*.whl' | head -1)"
    [[ -n "$wheel" ]] || die "no wheel built"
    uv pip install --python "$py" --break-system-packages --no-deps "$wheel"

    local site="$runtime/lib/python$PY_SERIES/site-packages"
    cp "$APP_DIR/Support/framescribe_app_launcher.py" "$site/"
    # the install records the wheel's path on this machine; the bundle must not carry it
    find "$site" -path '*framescribe-*.dist-info/direct_url.json' -delete

    strip_runtime "$runtime"

    step "byte-compile (unchecked-hash .pyc: never rewritten, so the signed bundle stays unchanged)"
    # -s/-p: the recorded source paths are relative to the runtime, not this machine's (importing fixes them up)
    # -B: the modules compileall itself imports are not written first with timestamps and absolute paths
    "$py" -I -B -m compileall -q -f -j 0 --invalidation-mode unchecked-hash -s "$runtime" -p runtime \
        "$runtime/lib/python$PY_SERIES" >/dev/null \
        || note "some files did not compile (they are not importable on this Python either)"

    step "check: the runtime imports framescribe"
    env -i HOME="$HOME" PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 "$py" -I -B -c \
        'import framescribe.cli, framescribe.pipeline, framescribe_app_launcher, Vision, mlx.core, cv2, numpy, parakeet_mlx, librosa; print("    imports ok")'
    printf '%s\n' "$stamp" > "$WORK/runtime.stamp"
}

strip_runtime() {
    local rt="$1" lib="$1/lib/python$PY_SERIES"
    step "strip what the tool never uses"
    # interpreter extras: headers, static library, man pages, other executables, Tcl/Tk
    rm -rf "$rt/include" "$rt/share" "$rt/lib/pkgconfig" "$lib"/config-"$PY_SERIES"-darwin
    find "$rt/bin" -mindepth 1 ! -name "python$PY_SERIES" -exec rm -rf {} +
    rm -rf "$rt"/lib/tcl* "$rt"/lib/tk* "$rt"/lib/itcl* "$rt"/lib/thread* "$rt"/lib/libtcl* "$rt"/lib/libtk*
    rm -f "$lib"/lib-dynload/_tkinter*.so
    # standard library parts for development and GUIs
    rm -rf "$lib/test" "$lib/idlelib" "$lib/tkinter" "$lib/turtledemo" "$lib/turtle.py" "$lib/ensurepip" \
        "$lib/lib2to3/tests" "$lib/pydoc_data"
    # installers and test suites of the packages; static libraries and headers they ship
    rm -rf "$lib"/site-packages/pip "$lib"/site-packages/pip-*.dist-info "$lib"/site-packages/PyObjCTest
    find "$lib/site-packages" -type d \( -name tests -o -name testing_data \) -path '*/site-packages/*/*' -prune \
        -exec rm -rf {} +
    # (*.pyi stubs stay: librosa's lazy loader reads its __init__.pyi at import time)
    find "$rt" -type f -name '*.a' -delete
    find "$lib/site-packages" -type d -name include -path '*/site-packages/*/include' -prune -exec rm -rf {} +
    find "$rt" -type d -name __pycache__ -prune -exec rm -rf {} +
}

# ---- the bundle -----------------------------------------------------------------------------------------------------

make_icon() {
    local icns="$WORK/AppIcon.icns"
    if [[ -f "$icns" && "$icns" -nt "$APP_DIR/Support/make_icon.swift" ]]; then return; fi
    step "app icon (drawn by Support/make_icon.swift)"
    local set="$WORK/AppIcon.iconset" png="$WORK/icon_1024.png"
    rm -rf "$set"
    mkdir -p "$set"
    swift "$APP_DIR/Support/make_icon.swift" "$png" >/dev/null
    local s
    for s in 16 32 128 256 512; do
        sips -z "$s" "$s" "$png" --out "$set/icon_${s}x${s}.png" >/dev/null
        sips -z $((s * 2)) $((s * 2)) "$png" --out "$set/icon_${s}x${s}@2x.png" >/dev/null
    done
    iconutil -c icns "$set" -o "$icns"
}

write_info_plist() {
    cat > "$1" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key><string>en</string>
    <key>CFBundleExecutable</key><string>Framescribe</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>CFBundleIdentifier</key><string>io.github.jwiderberg.framescribe</string>
    <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
    <key>CFBundleName</key><string>Framescribe</string>
    <key>CFBundleDisplayName</key><string>Framescribe</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>$VERSION</string>
    <key>CFBundleVersion</key><string>$VERSION</string>
    <key>LSMinimumSystemVersion</key><string>$MIN_MACOS</string>
    <key>LSArchitecturePriority</key><array><string>arm64</string></array>
    <key>LSApplicationCategoryType</key><string>public.app-category.productivity</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>NSPrincipalClass</key><string>NSApplication</string>
    <key>NSHumanReadableCopyright</key><string>MIT License</string>
    <key>NSDocumentsFolderUsageDescription</key><string>Framescribe saves its reports in Documents/framescribe unless you choose another folder in Settings, and reads the videos you add.</string>
    <key>NSDesktopFolderUsageDescription</key><string>Framescribe reads the videos you add and writes reports to the folder you choose.</string>
    <key>NSDownloadsFolderUsageDescription</key><string>Framescribe reads the videos you add and writes reports to the folder you choose.</string>
    <key>CFBundleDocumentTypes</key>
    <array>
        <dict>
            <key>CFBundleTypeName</key><string>Video</string>
            <key>CFBundleTypeRole</key><string>Viewer</string>
            <key>LSHandlerRank</key><string>Alternate</string>
            <key>LSItemContentTypes</key>
            <array>
                <string>public.movie</string>
                <string>public.mpeg-4</string>
                <string>com.apple.quicktime-movie</string>
                <string>com.apple.m4v-video</string>
                <string>org.matroska.mkv</string>
                <string>org.webmproject.webm</string>
            </array>
        </dict>
    </array>
    <key>UTImportedTypeDeclarations</key>
    <array>
        <dict>
            <key>UTTypeIdentifier</key><string>org.matroska.mkv</string>
            <key>UTTypeDescription</key><string>Matroska video</string>
            <key>UTTypeConformsTo</key><array><string>public.movie</string></array>
            <key>UTTypeTagSpecification</key>
            <dict><key>public.filename-extension</key><array><string>mkv</string></array>
                  <key>public.mime-type</key><array><string>video/x-matroska</string></array></dict>
        </dict>
        <dict>
            <key>UTTypeIdentifier</key><string>org.webmproject.webm</string>
            <key>UTTypeDescription</key><string>WebM video</string>
            <key>UTTypeConformsTo</key><array><string>public.movie</string></array>
            <key>UTTypeTagSpecification</key>
            <dict><key>public.filename-extension</key><array><string>webm</string></array>
                  <key>public.mime-type</key><array><string>video/webm</string></array></dict>
        </dict>
    </array>
</dict>
</plist>
EOF
    plutil -lint "$1" >/dev/null
}

assemble_bundle() {
    make_icon
    step "assemble $APP"
    rm -rf "$APP"
    mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
    cp "$SWIFT_BIN" "$APP/Contents/MacOS/Framescribe"
    strip -S -x "$APP/Contents/MacOS/Framescribe"  # debug symbols name this checkout's paths
    write_info_plist "$APP/Contents/Info.plist"
    printf 'APPL????' > "$APP/Contents/PkgInfo"
    cp "$WORK/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
    # a clone where the file system can (APFS), a copy otherwise
    if ! cp -Rc "$WORK/runtime" "$APP/Contents/Resources/runtime" 2>/dev/null; then
        rm -rf "$APP/Contents/Resources/runtime"
        cp -R "$WORK/runtime" "$APP/Contents/Resources/runtime"
    fi
    # nothing in the bundle may point at this checkout (libraries by build path, recorded paths, source paths)
    if otool -L "$APP/Contents/Resources/runtime/bin/python$PY_SERIES" | tail -n +2 | grep -qF "$REPO"; then
        die "the Python executable links to a library by its build path"
    fi
    if grep -rlF "$REPO" "$APP/Contents" >/dev/null 2>&1; then
        grep -rlF "$REPO" "$APP/Contents" | head -5 >&2
        die "the bundle mentions the path of this checkout"
    fi
}

# ---- the deployment target -----------------------------------------------------------------------------------------

version_number() {  # 15.0 -> 150000, 10.9.5 -> 100905
    local IFS=. a b c
    read -r a b c <<< "$1"
    echo $(( ${a:-0} * 10000 + ${b:-0} * 100 + ${c:-0} ))
}

macho_files() {  # every Mach-O file of the bundle, one per line, found by content (app/bundle_check.sh)
    check_bundle_enumerable "$APP" || die "the assembled bundle fails the structure check: $BUNDLE_PROBLEM"
    macho_by_magic "$APP"
}

check_min_macos() {
    step "check: no Mach-O file needs a macOS newer than $MIN_MACOS (vtool -show-build, arm64)"
    local list="$WORK/macho-all.txt" target worst=0 worst_file="" worst_version="" bad=0 n=0 f v
    target="$(version_number "$MIN_MACOS")"
    macho_files > "$list"
    while IFS= read -r f; do
        n=$((n + 1))
        # LC_BUILD_VERSION gives "minos X"; older binaries have LC_VERSION_MIN_MACOSX with "version X"
        v="$(vtool -arch arm64 -show-build "$f" 2>/dev/null | awk '
            /cmd LC_BUILD_VERSION/ {b=1; m=0} /cmd LC_VERSION_MIN_MACOSX/ {m=1; b=0}
            b && $1 == "minos" {print $2; exit} m && $1 == "version" {print $2; exit}')"
        [[ -n "$v" ]] || die "no macOS version in $f"
        if (( $(version_number "$v") > worst )); then worst="$(version_number "$v")"; worst_file="$f"; worst_version="$v"; fi
        if (( $(version_number "$v") > target )); then
            printf '    needs macOS %s: %s\n' "$v" "${f#"$APP"/}" >&2
            bad=$((bad + 1))
        fi
    done < "$list"
    note "$n Mach-O files; the highest minimum is macOS $worst_version (${worst_file#"$APP"/})"
    [[ $bad -eq 0 ]] || die "$bad file(s) need a macOS newer than $MIN_MACOS"
}

# ---- signing --------------------------------------------------------------------------------------------------------

sign_bundle() {
    # one code path for every kind of signing: app/sign_app.sh (the release workflow calls it on its own)
    local args=()
    if [[ "$identity" != "-" ]]; then args+=(--identity "$identity"); elif [[ $HARDENED -eq 1 ]]; then args+=(--hardened); fi
    if [[ -n "$keychain" ]]; then args+=(--keychain "$keychain"); fi
    step "sign (app/sign_app.sh${args[*]+ ${args[*]}})"
    "$APP_DIR/sign_app.sh" ${args[@]+"${args[@]}"} "$APP" | sed 's/^/    /'
}

check_signed_runtime() {
    # the same imports as the check before signing, now under the hardened runtime, the entitlements and (with a
    # Developer ID) library validation: every extension module the tool uses is loaded, and numba compiles and runs a
    # function, which needs executable memory. A missing entitlement ends the process with SIGKILL.
    step "check: the signed runtime imports framescribe and runs numba-compiled code"
    local tmp
    tmp="$(mktemp -d "${TMPDIR:-/tmp}/framescribe-check.XXXXXX")"
    if ! env -i HOME="$HOME" TMPDIR="$tmp" PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 NUMBA_CACHE_DIR="$tmp" \
        "$APP/Contents/Resources/runtime/bin/python$PY_SERIES" -I -B -c '
import framescribe.cli, framescribe.pipeline, framescribe_app_launcher, Vision, Quartz, mlx.core, cv2, numpy, scipy
import parakeet_mlx, librosa, numba
@numba.njit
def total(n):
    t = 0
    for i in range(n):
        t += i
    return t
assert total(1000) == 499500
print("    imports ok, numba ok")'; then
        rm -rf "$tmp"
        die "the signed runtime does not start (status above; a SIGKILL means a missing entitlement)"
    fi
    rm -rf "$tmp"
    codesign --verify --deep --strict "$APP" || die "running the signed runtime changed the bundle"
}

preflight() {
    check_signed_runtime
    local mode=()
    if [[ "$identity" == "-" ]]; then mode=(--allow-adhoc); fi
    step "preflight: what notarization checks (app/preflight_app.sh ${mode[*]:-})"
    "$APP_DIR/preflight_app.sh" ${mode[@]+"${mode[@]}"} "$APP" | sed 's/^/    /' \
        || die "the signed bundle fails the preflight (see above)"
}

notarize() {
    local zip="$WORK/notarize-upload.zip" out="$WORK/notarize.json" status
    step "notarize with profile $1"
    rm -f "$zip"
    ditto -c -k --keepParent "$APP" "$zip"
    xcrun notarytool submit "$zip" --keychain-profile "$1" --wait --output-format json > "$out" || true
    status="$(plutil -extract status raw -o - "$out" 2>/dev/null || true)"
    if [[ "$status" != "Accepted" ]]; then
        cat "$out" >&2
        local id
        id="$(plutil -extract id raw -o - "$out" 2>/dev/null || true)"
        if [[ -n "$id" ]]; then xcrun notarytool log "$id" --keychain-profile "$1" >&2 || true; fi
        die "notarization status: ${status:-unknown}"
    fi
    rm -f "$zip"
    xcrun stapler staple "$APP"
    xcrun stapler validate "$APP"
    spctl --assess --type execute --verbose=2 "$APP"
}

report() {
    local rt="$APP/Contents/Resources/runtime" site top all stdlib
    site="$rt/lib/python$PY_SERIES/site-packages"
    step "done: $APP"
    note "size: $(du -sh "$APP" | cut -f1); for macOS $MIN_MACOS and later (LSMinimumSystemVersion)"
    all="$(du -sm "$rt/lib/python$PY_SERIES" | cut -f1)"
    stdlib=$((all - $(du -sm "$site" | cut -f1)))
    note "runtime: $(du -sm "$rt" | cut -f1) MB (standard library ${stdlib} MB); largest packages (MB):"
    top="$(du -sm "$site"/* | sort -rn | head -8)"  # a variable, not a pipe into head: pipefail
    while read -r mb path; do note "  $(printf '%5s' "$mb")  ${path#"$site"/}"; done <<< "$top"
    note "open it with: open \"$APP\""
}

main "$@"
