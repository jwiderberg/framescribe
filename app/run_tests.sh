#!/usr/bin/env bash
# Runs the Swift tests (Swift Testing). With only the Command Line Tools installed, `swift test` does not find the
# Testing framework by itself; this adds its location. With Xcode selected, it is plain `swift test`.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
dev="$(xcode-select -p 2>/dev/null || true)"
frameworks="$dev/Library/Developer/Frameworks"
if [[ "$dev" == */CommandLineTools && -d "$frameworks/Testing.framework" ]]; then
    exec swift test \
        -Xswiftc -F -Xswiftc "$frameworks" \
        -Xlinker -F -Xlinker "$frameworks" \
        -Xlinker -rpath -Xlinker "$frameworks" \
        -Xlinker -rpath -Xlinker "$dev/Library/Developer/usr/lib" \
        "$@"
fi
exec swift test "$@"
