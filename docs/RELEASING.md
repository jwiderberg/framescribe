# Releasing the Mac app

A release is a zip of `Framescribe.app`, signed with a Developer ID, notarized by Apple, run once on a fresh machine and published on the releases page by `.github/workflows/release.yml`. The workflow runs when a tag `v<version>` is pushed. It needs five secrets in a GitHub environment called `release`, set once with `scripts/release-secrets.sh`. What the workflow does, job by job, is in [DESIGN.md](DESIGN.md#release).

Needed once:

- Membership in the Apple Developer Program. Only the Account Holder can create a Developer ID Application certificate.
- Admin rights on the GitHub repository and `gh` logged in (`gh auth login`), to set the secrets and to read the settings below.

## 1. Repository settings

The order matters. The settings and the `release` environment have to be in place before the workflow file is ever pushed, and the secrets go into the environment only. A repository-level secret is readable by every workflow on every branch, including one a collaborator pushes. An environment secret reaches only a job that names the environment, and only once the environment's rules admit it.

This repository has all of the following already. They are the required state: check them before a release, and after any change to the repository's settings. Each item says how to check it with read-only `gh` calls.

- **Environment `release`.** At least one required reviewer, and under Deployment branches and tags a single rule, Ref type Tag, `v*`, and no branch rule. Its secrets are the five the script sets, nothing else. Only the workflow's `sign` job uses this environment, and every run of it waits for an approval. `scripts/release-secrets.sh` checks the reviewer and the tag rule before it sets anything, and `--dry-run` reports them.

  ```
  gh api repos/jwiderberg/framescribe/environments/release \
      --jq '{reviewers: [.protection_rules[] | select(.type == "required_reviewers") | .reviewers[].reviewer.login], policy: .deployment_branch_policy}'
  gh api repos/jwiderberg/framescribe/environments/release/deployment-branch-policies --jq '.branch_policies[] | {type, name}'
  gh secret list --repo jwiderberg/framescribe --env release
  ```

  Expected: one or more reviewers, `custom_branch_policies: true` and `protected_branches: false`, one policy `{"type": "tag", "name": "v*"}`, the five secret names.

- **Tag ruleset.** Target tags matching `v*`, enforcement Active, restrict creations, updates and deletions, bypass list Repository admin. Only admins can make, move or delete a release tag or a dry-run tag.
- **Branch ruleset.** `main` (the default branch), Active, block force pushes, restrict deletions.

  ```
  gh api repos/jwiderberg/framescribe/rulesets --jq '.[] | {name, target, enforcement}'
  gh api repos/jwiderberg/framescribe/rulesets/RULESET_ID     # for the rules and the bypass list of one of them
  ```

- **Actions.** Allow only actions created by GitHub (every action the workflow uses is GitHub's own: checkout, upload-artifact, download-artifact, attest-build-provenance), with Require actions to be pinned to a full-length commit SHA. An entry for `astral-sh/setup-uv` is no longer needed: the workflow downloads uv's release itself and checks its SHA-256. Fork pull request workflows: Require approval for all outside contributors. Workflow permissions: Read repository contents and packages permissions.

  ```
  gh api repos/jwiderberg/framescribe/actions/permissions
  gh api repos/jwiderberg/framescribe/actions/permissions/selected-actions
  gh api repos/jwiderberg/framescribe/actions/permissions/workflow
  ```

- **Security.** Dependabot alerts and security updates, and private vulnerability reporting (the path [SECURITY.md](../SECURITY.md) points to).

  ```
  gh api repos/jwiderberg/framescribe/private-vulnerability-reporting
  gh api repos/jwiderberg/framescribe/vulnerability-alerts --silent && echo "Dependabot alerts on"
  ```

## 2. Export the Developer ID certificate as a .p12

1. Open Keychain Access, select the login keychain and the My Certificates tab.
2. Find `Developer ID Application: <name> (<Team ID>)`. Its private key shows under it when the row is expanded. Without the key the export is useless. If there is no such certificate, create one in Xcode ▸ Settings ▸ Accounts ▸ Manage Certificates ▸ + ▸ Developer ID Application (Account Holder only). It then appears here.
3. Right-click the certificate (not the key), Export, file format Personal Information Exchange (.p12), save it, for example as `~/Desktop/DeveloperID.p12`.
4. Choose a password for the file. The script asks for it, and it becomes a secret too.

## 3. Create an App Store Connect API key

1. In [App Store Connect](https://appstoreconnect.apple.com), go to Users and Access ▸ Integrations ▸ App Store Connect API ▸ Team Keys.
2. Generate a key (+). Any name. Access: Developer. That role is enough to notarize, and it can do less than Admin or App Manager if it ever leaks.
3. Download `AuthKey_<Key ID>.p8`. Apple offers the download once.
4. Note two values from that page: the Key ID in the key's row (10 characters) and the Issuer ID shown above the table (a UUID).

## 4. Load the secrets

Check everything without setting anything (this only reads from GitHub):

```
scripts/release-secrets.sh --p12 ~/Desktop/DeveloperID.p12 --p8 ~/Downloads/AuthKey_ABC123DEFG.p8 \
    --key-id ABC123DEFG --issuer 12345678-aaaa-bbbb-cccc-1234567890ab --dry-run
```

Then the same command without `--dry-run`. It checks that the .p8 parses as an EC private key, that the environment `release` exists with a required reviewer and deployments from tags only (it stops with what to fix otherwise), asks for the .p12 password without echo and checks that the file opens with it, holds a private key and a Developer ID Application certificate issued by Apple that has not expired. It then sets `DEVELOPER_ID_P12_BASE64`, `DEVELOPER_ID_P12_PASSWORD`, `NOTARY_KEY_P8_BASE64`, `NOTARY_KEY_ID` and `NOTARY_ISSUER_ID` as secrets of that environment, passing each value to `gh secret set --env release` on standard input. It prints no value.

If any of the five was ever set at repository level, delete those copies so that only the environment holds them:

```
gh secret list --repo jwiderberg/framescribe
for s in DEVELOPER_ID_P12_BASE64 DEVELOPER_ID_P12_PASSWORD NOTARY_KEY_P8_BASE64 NOTARY_KEY_ID NOTARY_ISSUER_ID; do
    gh secret delete "$s" --repo jwiderberg/framescribe
done
gh secret list --repo jwiderberg/framescribe --env release    # the five, nothing at repository level
```

Or in the UI: Settings ▸ Secrets and variables ▸ Actions ▸ Repository secrets, delete each of the five.

Afterwards delete both files (`rm -P`), or keep them offline. The certificate and key stay in the login keychain, so a new .p12 can be exported any time. The .p8 cannot be downloaded again.

## 5. Dry run

A dry run builds, signs, notarizes, staples and verifies exactly like a release, then keeps the zip as a workflow artifact. It publishes and attests nothing.

The zip of a dry run is a workflow artifact of a public repository: anyone can download it until it expires, and it expires after one day (`retention-days: 1`). It is signed with the Developer ID and notarized, so treat it as published for that day.

How a dry run starts, and why:

- A manual run (`workflow_dispatch`) uses the workflow file of the ref it is started on, but GitHub only offers it once the workflow file is on the default branch. So `release.yml` has to be on `main` first.
- The `sign` job only runs on a ref the environment admits, and the environment admits tags `v*` only. So a dry run is started on a tag, never on a branch. The Version step stops any run that is not on a tag.
- A real release tag is `v` plus the version in `pyproject.toml`. A dry run uses a dry-run tag instead: `v<version>-dry<N>`, for example `v0.1.0-dry1`. Pushing a dry-run tag starts nothing (the push trigger excludes `v*-dry*`). A manual run on it with "Dry run" ticked signs, notarizes and verifies. A manual run on it with "Dry run" unticked is refused by the Version step, and the publish job never runs for a `-dry` tag either way.

The sequence:

1. Merge the branch with the workflow into `main` and push `main`.
2. Tag the commit to test with a dry-run tag and push the tag. Nothing starts.

   ```
   git tag v0.1.0-dry1 && git push origin v0.1.0-dry1
   ```

3. Start the run on that tag: Actions tab ▸ Release ▸ Run workflow, pick the tag `v0.1.0-dry1` under Use workflow from, leave "Dry run" ticked. Or:

   ```
   gh workflow run release.yml --repo jwiderberg/framescribe --ref v0.1.0-dry1 -f dry_run=true
   ```

4. Approve the `sign` job when GitHub asks. Each approval request shows who started the run and the commit it runs on. Check both before approving: the actor must be you, and the commit must be the one the tag was made on (`git rev-parse v0.1.0-dry1`). Reject anything else.
5. Watch it (`gh run watch`) and download the zip within the day (`gh run download RUN_ID --repo jwiderberg/framescribe`). The `verify` job has already run the signed app on a fresh machine: Gatekeeper's assessment, the staple, the signature, the Team ID, the entitlements, `--version`, `doctor` and the imports that need the executable-memory entitlement, under library validation with the real Team ID.
6. On a Mac, unzip it, open the app and process one video. Only that shows the whole pipeline running from the signed bundle.
7. Delete the dry-run tag (an admin bypasses the tag ruleset): `git push origin :refs/tags/v0.1.0-dry1 && git tag -d v0.1.0-dry1`. A second dry run of the same version uses `v0.1.0-dry2`.

## 6. Cut a release

1. Set the version in `pyproject.toml`, in `install.sh` (`VERSION`) and in the README's installer URLs, run `uv lock` and regenerate `constraints.txt` (`uv export --locked --no-dev --no-emit-project --no-hashes --no-header --format requirements-txt > constraints.txt`). The tests fail when any of these disagree. Commit, and merge to `main`.
2. Tag that commit and push the tag:

   ```
   git tag v0.1.0 && git push origin v0.1.0
   ```

3. Approve the `sign` job when GitHub asks (Actions tab, or the e-mail it sends), after checking the actor and the commit as in the dry run.

The tag must be `v` plus the version in `pyproject.toml`, otherwise the build stops at its first step. A manual run on the release tag with "Dry run" unticked also publishes. With "Dry run" ticked it is a dry run.

## 7. Check the result

- The run is green and the release holds `Framescribe-<version>.zip` and `Framescribe-<version>.zip.sha256`. The notes name the commit, the Team ID and the SHA-256 of `install.sh` at the tag.
- On a Mac, after downloading both files:

  ```
  shasum -a 256 -c Framescribe-<version>.zip.sha256
  gh attestation verify Framescribe-<version>.zip --repo jwiderberg/framescribe
  ditto -x -k Framescribe-<version>.zip /Applications
  spctl --assess --type execute -vv /Applications/Framescribe.app    # accepted, source=Notarized Developer ID
  xcrun stapler validate /Applications/Framescribe.app
  codesign -dvv /Applications/Framescribe.app 2>&1 | grep TeamIdentifier
  ```

- Open the app and run one video. The first launch of a download shows Gatekeeper's "downloaded from the internet" prompt once. Any other warning means something is wrong.
- The command line installer of the release: `curl -fsSLO https://raw.githubusercontent.com/jwiderberg/framescribe/v<version>/install.sh`, then `shasum -a 256 install.sh` must print the value in the notes.

When a step fails, its log says why. The usual causes: the tag does not match `pyproject.toml` (Version, in the `build` job), a malformed bundle (Check the bundle structure), a missing secret or a wrong password (Import Developer ID certificate), a file without the hardened runtime, the Developer ID chain or the one Team ID (Preflight), a rejection by Apple (Notarize prints `notarytool log` with the reason per file), a signed app that does not start (verify). To redo a release, delete the release and the tag (`gh release delete v0.1.0 --cleanup-tag`, then `git tag -d v0.1.0` locally), fix, tag again.

## Rotating and revoking

- **Certificate.** A Developer ID certificate is valid for five years. With a new one, export it as in step 2 and run the script again: it overwrites the secrets. Revoking a certificate (developer.apple.com ▸ Certificates) stops apps signed with it from opening, published releases included. Do that only if the key leaked, then publish a new release signed with a new certificate.
- **API key.** Revoke it in App Store Connect ▸ Users and Access ▸ Integrations, generate a new one and run the script again. Revoking it affects nothing already notarized.
- **Secrets.** `gh secret delete NAME --env release`, or Settings ▸ Environments ▸ release. GitHub never shows a secret's value again after it is set.

## What the workflow can and cannot see

- Four jobs. `build` downloads and runs all third-party code (uv, the standalone Python, the wheels, the import checks) and has no secrets and a read-only token. `sign` is the only job in the `release` environment and the only one that sees the secrets. It downloads the finished bundle, checks its structure before any secret is touched, runs only the repository's own checking, signing and packaging scripts from the commit, and installs, caches and executes nothing else. `verify` has no secrets and a read-only token: it runs the signed app on a fresh machine. `publish` has no secrets. It gets a token that can create the release (`contents: write`) and sign the provenance attestation (`id-token`, `attestations`), and nothing else, and runs only after `verify` passed.
- Every job checks out the commit the run was started for (`github.sha`), so a tag moved while a run waits for approval changes nothing in that run.
- In `sign` the secrets are environment variables of two steps: importing the certificate and notarizing. GitHub masks their values in logs. The .p12's password never appears on a command line: `openssl` reads it from the environment and packs the certificate again under a random password made for the one `security import`, the same way the throwaway keychain has a random password. The Key ID and Issuer ID are given to `notarytool` as arguments: they are identifiers, not secrets. The certificate lives in the throwaway keychain while the app is signed. The keychain is deleted right after signing and again at the end of the job, whatever happened. The .p12 files and the .p8 are written to the runner's temporary folder with owner-only permissions and removed in the same way. GitHub's hosted runners are discarded after each job.
- Apple sees the uploaded zip and the Team ID. It never sees the certificate's private key. The workflow never sees an Apple ID or its password.
- The environment's required reviewer and its tag rule decide which runs reach the secrets. Anyone with write access can still push a branch that changes the workflow, but its `sign` job waits for approval and runs only on a `v*` tag, which the tag ruleset leaves to admins. Approve only runs you started, on the commit you tagged.
- Pull requests do not run this workflow, and GitHub passes no secrets to workflows started from forks.
