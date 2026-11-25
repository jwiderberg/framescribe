# Releasing the Mac app

A release is a zip of `Framescribe.app`, signed with a Developer ID, notarized by Apple and published on the releases page by `.github/workflows/release.yml`. The workflow runs when a tag `v<version>` is pushed. It needs five secrets in a GitHub environment called `release`, set once with `scripts/release-secrets.sh`. What the workflow does, job by job, is in [DESIGN.md](DESIGN.md#release).

Needed once:

- Membership in the Apple Developer Program. Only the Account Holder can create a Developer ID Application certificate.
- Admin rights on the GitHub repository and `gh` logged in (`gh auth login`), to create the environment and set the secrets.

## 1. Repository settings

Switch these on before the first release. All of them are in the repository's Settings.

- **Environment `release`.** Environments ▸ New environment ▸ `release`. Tick Required reviewers and add yourself. Under Deployment branches and tags choose Selected branches and tags ▸ Add deployment branch or tag rule ▸ Ref type Tag ▸ `v*`. Leave its secrets empty, the script fills them in step 4. Only the workflow's `sign` job uses this environment, and every run of it waits for your approval.
- **Tag ruleset.** Rules ▸ Rulesets ▸ New ruleset ▸ New tag ruleset, target tags matching `v*`, enforcement Active, restrict creations, restrict updates and restrict deletions, bypass list Repository admin. Then only admins can make or move a release tag.
- **Branch ruleset.** New branch ruleset for `main` (Include default branch), Active, block force pushes and restrict deletions.
- **Actions.** Actions ▸ General ▸ Actions permissions: Allow <owner>, and select non-<owner>, actions and reusable workflows, with Allow actions created by GitHub ticked and `astral-sh/setup-uv@*` added. Tick Require actions to be pinned to a full-length commit SHA. Under Fork pull request workflows from outside collaborators choose Require approval for all external contributors. Workflow permissions: Read repository contents and packages permissions.
- **Security.** Advanced Security (or Code security): turn on Dependabot alerts, Dependabot security updates and Private vulnerability reporting.

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

Check everything without changing anything (this calls no GitHub API):

```
scripts/release-secrets.sh --p12 ~/Desktop/DeveloperID.p12 --p8 ~/Downloads/AuthKey_ABC123DEFG.p8 \
    --key-id ABC123DEFG --issuer 12345678-aaaa-bbbb-cccc-1234567890ab --dry-run
```

Then the same command without `--dry-run`. It asks for the .p12 password without echo and checks that the file opens with it, holds a private key and a Developer ID Application certificate issued by Apple that has not expired. It stops if the environment `release` does not exist. It then sets `DEVELOPER_ID_P12_BASE64`, `DEVELOPER_ID_P12_PASSWORD`, `NOTARY_KEY_P8_BASE64`, `NOTARY_KEY_ID` and `NOTARY_ISSUER_ID` as secrets of that environment, passing each value to `gh secret set --env release` on standard input. It prints no value.

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

A dry run builds, signs, notarizes and staples exactly like a release, then keeps the zip as a workflow artifact for 7 days. It publishes and attests nothing.

The `sign` job only runs on a ref the environment admits, and the environment admits tags `v*`. So a dry run is started on a tag: Actions tab ▸ Release ▸ Run workflow, pick the tag under Use workflow from, leave "Dry run" ticked. Or `gh workflow run release.yml --ref v0.1.0 -f dry_run=true`, then `gh run watch` and `gh run download` to get the zip. Approve the `sign` job when GitHub asks.

Pushing a `v*` tag starts a real release. For a dry run before the first release, add a second deployment rule to the environment for the branch `main` (Ref type Branch), run the workflow from `main` with "Dry run" ticked, and remove that rule again afterwards.

The workflow does not run the signed app. Unzip the dry-run zip on a Mac, open the app and process one video. Only that shows the embedded Python starts with library validation on (see DESIGN.md, Release).

## 6. Cut a release

1. Set the version in `pyproject.toml`, run `uv lock` (the build uses the lockfile with `--locked`, which fails when the lock does not match), commit both.
2. Tag that commit and push the tag:

   ```
   git tag v0.1.0 && git push origin v0.1.0
   ```

3. Approve the `sign` job when GitHub asks (Actions tab, or the e-mail it sends).

The tag must be `v` plus the version in `pyproject.toml`, otherwise the build stops at its first step. A manual run on a tag with "Dry run" unticked also publishes.

## 7. Check the result

- The run is green and the release holds `Framescribe-<version>.zip` and `Framescribe-<version>.zip.sha256`. The notes name the commit and the Team ID.
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

When a step fails, its log says why. The usual causes: the tag does not match `pyproject.toml` (Version, in the `build` job), a missing secret or a wrong password (Import Developer ID certificate), a file without the hardened runtime or with another Team ID (Preflight), a rejection by Apple (Notarize prints `notarytool log` with the reason per file). To redo a release, delete the release and the tag (`gh release delete v0.1.0 --cleanup-tag`, then `git tag -d v0.1.0` locally), fix, tag again.

## Rotating and revoking

- **Certificate.** A Developer ID certificate is valid for five years. With a new one, export it as in step 2 and run the script again: it overwrites the secrets. Revoking a certificate (developer.apple.com ▸ Certificates) stops apps signed with it from opening, published releases included. Do that only if the key leaked, then publish a new release signed with a new certificate.
- **API key.** Revoke it in App Store Connect ▸ Users and Access ▸ Integrations, generate a new one and run the script again. Revoking it affects nothing already notarized.
- **Secrets.** `gh secret delete NAME --env release`, or Settings ▸ Environments ▸ release. GitHub never shows a secret's value again after it is set.

## What the workflow can and cannot see

- Three jobs. `build` downloads and runs all third-party code (uv, the standalone Python, the wheels, the import checks) and has no secrets and a read-only token. `sign` is the only job in the `release` environment and the only one that sees the secrets. It downloads the finished bundle, runs only the repository's own signing, checking and packaging scripts from the tag, and installs, caches and executes nothing else. `publish` has no secrets. It gets a token that can create the release (`contents: write`) and sign the provenance attestation (`id-token`, `attestations`), and nothing else.
- In `sign` the secrets are environment variables of two steps: importing the certificate and notarizing. GitHub masks their values in logs. The certificate lives in a throwaway keychain with a random password while the app is signed. The keychain is deleted right after signing and again at the end of the job, whatever happened. The .p12 and .p8 are written to the runner's temporary folder with owner-only permissions and removed in the same way. GitHub's hosted runners are discarded after each job.
- Apple sees the uploaded zip and the Team ID. It never sees the certificate's private key. The workflow never sees an Apple ID or its password.
- The environment's required reviewer and its tag rule decide which runs reach the secrets. Anyone with write access can still push a branch that changes the workflow, but its `sign` job waits for approval and runs only on a `v*` tag, which the tag ruleset leaves to admins. Approve only runs you started.
- Pull requests do not run this workflow, and GitHub passes no secrets to workflows started from forks.
