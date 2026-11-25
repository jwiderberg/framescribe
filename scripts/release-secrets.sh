#!/usr/bin/env bash
# Puts the signing and notarization secrets that .github/workflows/release.yml needs into the GitHub environment
# "release" of the repository, with `gh secret set --env`. The environment must exist already (docs/RELEASING.md:
# create it first, with a required reviewer and deployments limited to tags v*). docs/RELEASING.md also says how to get
# the two files.
#
#   scripts/release-secrets.sh --p12 DeveloperID.p12 --p8 AuthKey_KEYID.p8 --key-id KEYID --issuer ISSUER-UUID
#
#   --p12 FILE      the "Developer ID Application" certificate with its private key, exported from Keychain Access
#   --p8 FILE       the App Store Connect API key (AuthKey_<KEYID>.p8)
#   --key-id ID     its Key ID (10 letters and digits)
#   --issuer UUID   the Issuer ID shown above the keys in App Store Connect
#   --repo O/NAME   the repository (default: the one gh resolves for this clone, or origin's in a dry run)
#   --env NAME      the environment that holds the secrets (default: release, the one the workflow's sign job uses)
#   --dry-run       check everything and say what would be set, set nothing and do not call gh at all
#
# It asks for the .p12 password without echo, checks that the .p12 opens with it and holds a Developer ID
# Application certificate (issued by Apple's Developer ID CA, not expired) and a private key, and that the .p8 looks
# like a private key, and that the environment exists. Then it sets five environment secrets: DEVELOPER_ID_P12_BASE64,
# DEVELOPER_ID_P12_PASSWORD, NOTARY_KEY_P8_BASE64, NOTARY_KEY_ID, NOTARY_ISSUER_ID. Every value goes to gh on standard
# input, never as an argument, and nothing secret is printed.
set -euo pipefail

usage() { sed -n '2,/^set -euo pipefail$/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }
fail() { printf 'release-secrets.sh: %s\n' "$*" >&2; exit 1; }
need_arg() { [[ $# -ge 2 && -n "${2:-}" ]] || { printf 'release-secrets.sh: %s needs a value\n' "$1" >&2; exit 2; }; }

main() {
    local p12="" p8="" key_id="" issuer="" repo="" env="release" dry_run=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --p12) need_arg "$@"; p12="$2"; shift ;;
            --p8) need_arg "$@"; p8="$2"; shift ;;
            --key-id) need_arg "$@"; key_id="$2"; shift ;;
            --issuer) need_arg "$@"; issuer="$2"; shift ;;
            --repo) need_arg "$@"; repo="$2"; shift ;;
            --env) need_arg "$@"; env="$2"; shift ;;
            --dry-run) dry_run=1 ;;
            -h|--help) usage; exit 0 ;;
            *) printf 'release-secrets.sh: unknown option %s (see --help)\n' "$1" >&2; exit 2 ;;
        esac
        shift
    done

    [[ -n "$p12" && -n "$p8" && -n "$key_id" && -n "$issuer" ]] || fail "--p12, --p8, --key-id and --issuer are all required (see --help)"
    [[ -f "$p12" ]] || fail "no such file: $p12"
    [[ -f "$p8" ]] || fail "no such file: $p8"
    [[ "$key_id" =~ ^[A-Z0-9]{10}$ ]] || fail "--key-id must be the 10-character Key ID shown next to the key"
    [[ "$issuer" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]] \
        || fail "--issuer must be the Issuer ID (a UUID, shown above the list of keys)"
    grep -q -- '-----BEGIN PRIVATE KEY-----' "$p8" && grep -q -- '-----END PRIVATE KEY-----' "$p8" \
        || fail "$p8 does not look like an App Store Connect API key (no PRIVATE KEY block)"
    command -v openssl >/dev/null || fail "openssl not found"

    if [[ $dry_run -eq 1 ]]; then
        [[ -n "$repo" ]] || repo="$(origin_repo)"
        [[ -n "$repo" ]] || repo="(the repository gh would resolve)"
    else
        command -v gh >/dev/null || fail "gh (GitHub CLI) is not installed"
        gh auth status >/dev/null 2>&1 || fail "gh is not logged in (gh auth login)"
        if [[ -z "$repo" ]]; then
            repo="$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || true)"
            [[ -n "$repo" ]] || fail "could not tell the repository; pass --repo owner/name"
        fi
        # gh secret set --env would fail too, but with a less helpful message
        gh api "repos/$repo/environments/$env" --silent >/dev/null 2>&1 \
            || fail "the environment \"$env\" does not exist in $repo (or gh may not see it). Create it first: repository Settings > Environments > New environment \"$env\", see docs/RELEASING.md"
    fi

    # read without echo, never printed; it opens the .p12 below and becomes a secret itself
    local password=""
    if [[ -t 0 ]]; then printf 'Password of %s: ' "$p12" >&2; fi
    IFS= read -r -s password || true
    if [[ -t 0 ]]; then printf '\n' >&2; fi
    [[ -n "$password" ]] || fail "empty password"

    check_p12 "$p12" "$password"
    printf 'Certificate: %s\n' "$CERT_NAME"
    printf 'Valid until: %s\n' "$CERT_END"
    printf 'Repository:  %s\n' "$repo"
    printf 'Environment: %s\n' "$env"
    printf 'Secrets:\n'
    base64 < "$p12" | tr -d '\n' | set_secret DEVELOPER_ID_P12_BASE64 "$repo" "$env" "$dry_run"
    printf '%s' "$password" | set_secret DEVELOPER_ID_P12_PASSWORD "$repo" "$env" "$dry_run"
    base64 < "$p8" | tr -d '\n' | set_secret NOTARY_KEY_P8_BASE64 "$repo" "$env" "$dry_run"
    printf '%s' "$key_id" | set_secret NOTARY_KEY_ID "$repo" "$env" "$dry_run"
    printf '%s' "$issuer" | set_secret NOTARY_ISSUER_ID "$repo" "$env" "$dry_run"
    password=""

    printf '\n'
    if [[ $dry_run -eq 1 ]]; then
        printf 'Dry run: nothing was set and gh was not called.\n'
    else
        printf 'Done. GitHub holds the values now. Delete the exported files, or move them somewhere offline:\n'
        printf '  rm -P %q %q\n' "$p12" "$p8"
        printf 'The certificate and its key stay in your login keychain. The .p8 cannot be downloaded again: if it is\n'
        printf 'lost, revoke the key in App Store Connect and make a new one.\n'
        printf 'If the five secrets were ever set at repository level, delete those copies:\n'
        printf '  gh secret list --repo %s\n' "$repo"
        printf '  for s in DEVELOPER_ID_P12_BASE64 DEVELOPER_ID_P12_PASSWORD NOTARY_KEY_P8_BASE64 NOTARY_KEY_ID NOTARY_ISSUER_ID; do gh secret delete "$s" --repo %s; done\n' "$repo"
        printf 'Test the pipeline without publishing (on a ref the environment admits, see docs/RELEASING.md):\n'
        printf '  gh workflow run release.yml --repo %s --ref vVERSION -f dry_run=true\n' "$repo"
    fi
}

origin_repo() {  # owner/name of origin when it is on github.com, from the local git configuration only
    local url
    url="$(git -C "$(dirname "${BASH_SOURCE[0]}")" remote get-url origin 2>/dev/null || true)"
    sed -nE 's#^(https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+)$#\2#p' <<< "$url" | sed 's/\.git$//'
}

p12_cert() {  # the certificate of the .p12 (PEM) on stdout; the password goes to openssl on its standard input
    local file="$1" password="$2"
    printf '%s\n' "$password" | openssl pkcs12 "${@:3}" -in "$file" -nokeys -clcerts -passin stdin 2>/dev/null \
        | openssl x509 2>/dev/null || true
}

p12_has_key() {
    local file="$1" password="$2" n
    n="$(printf '%s\n' "$password" | openssl pkcs12 "${@:3}" -in "$file" -nocerts -nodes -passin stdin 2>/dev/null \
        | grep -c -- '-----BEGIN .*PRIVATE KEY-----' || true)"
    [[ "${n:-0}" -gt 0 ]]
}

check_p12() {
    local file="$1" password="$2" pem legacy=()
    pem="$(p12_cert "$file" "$password")"
    if [[ -z "$pem" ]]; then
        # OpenSSL 3 needs -legacy for the older encryption Keychain Access uses; LibreSSL (macOS) does not know it
        legacy=(-legacy)
        pem="$(p12_cert "$file" "$password" -legacy)"
    fi
    [[ -n "$pem" ]] || fail "could not open $file with that password (or it holds no certificate)"
    p12_has_key "$file" "$password" ${legacy[@]+"${legacy[@]}"} \
        || fail "$file holds no private key: export the certificate together with its key (My Certificates in Keychain Access)"

    local subject issuer
    subject="$(openssl x509 -noout -subject -nameopt multiline <<< "$pem" | sed -n 's/^ *commonName *= *//p')"
    issuer="$(openssl x509 -noout -issuer -nameopt multiline <<< "$pem" | sed -n 's/^ *commonName *= *//p')"
    [[ "$subject" == "Developer ID Application: "* ]] \
        || fail "the certificate in $file is not a Developer ID Application certificate: ${subject:-no name}"
    [[ "$issuer" == "Developer ID Certification Authority" ]] \
        || fail "the certificate in $file was not issued by Apple's Developer ID Certification Authority: ${issuer:-no issuer}"
    openssl x509 -noout -checkend 0 <<< "$pem" >/dev/null || fail "the certificate in $file has expired"
    CERT_NAME="$subject"
    CERT_END="$(openssl x509 -noout -enddate <<< "$pem" | sed 's/^notAfter=//')"
}

set_secret() {  # NAME REPO ENVIRONMENT DRY_RUN, the value on standard input
    if [[ "$4" -eq 1 ]]; then
        cat > /dev/null
        printf '  would set %s in environment %s\n' "$1" "$3"
    else
        # no --body: gh reads the value from standard input
        gh secret set "$1" --env "$3" --repo "$2" > /dev/null
        printf '  set %s in environment %s\n' "$1" "$3"
    fi
}

main "$@"
