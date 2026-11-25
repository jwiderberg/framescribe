"""The release pipeline, offline: the workflow's security properties (read from the YAML), every shell block it runs
(bash -n, and the version check executed), the secrets script against throwaway certificates, and the packaging and
preflight scripts against a tiny bundle made of copies of a system binary. Nothing here calls GitHub or Apple."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
SECRETS_SCRIPT = ROOT / "scripts" / "release-secrets.sh"
PREFLIGHT = ROOT / "app" / "preflight_app.sh"
PACKAGE = ROOT / "app" / "package_app.sh"
BUILD = ROOT / "app" / "build_app.sh"
SIGN = ROOT / "app" / "sign_app.sh"
SECRET_NAMES = {
    "DEVELOPER_ID_P12_BASE64",
    "DEVELOPER_ID_P12_PASSWORD",
    "NOTARY_KEY_P8_BASE64",
    "NOTARY_KEY_ID",
    "NOTARY_ISSUER_ID",
}
macos_only = pytest.mark.skipif(sys.platform != "darwin", reason="needs macOS tools (codesign, ditto, vtool)")


@pytest.fixture(scope="module")
def wf() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def triggers(wf: dict) -> dict:
    return wf.get("on", wf.get(True))  # YAML 1.1 reads the key `on` as true


def steps(wf: dict, job: str) -> list[dict]:
    return wf["jobs"][job]["steps"]


def step(wf: dict, job: str, name: str) -> dict:
    return next(s for s in steps(wf, job) if s.get("name") == name)


def as_text(node) -> str:
    return yaml.safe_dump(node)


# ---- triggers and permissions --------------------------------------------------------------------------------------


def test_triggers_are_a_version_tag_and_a_manual_run_with_a_dry_run_input(wf):
    on = triggers(wf)
    assert set(on) == {"push", "workflow_dispatch"}  # no pull_request, pull_request_target or workflow_run
    assert on["push"] == {"tags": ["v*"]}
    dry = on["workflow_dispatch"]["inputs"]["dry_run"]
    assert dry["type"] == "boolean" and dry["default"] is True  # a manual run is a dry run unless asked otherwise


def test_the_token_is_read_only_at_the_top_and_each_job_asks_only_for_what_it_needs(wf):
    assert wf["permissions"] == {"contents": "read"}
    assert set(wf["jobs"]) == {"build", "sign", "publish"}
    assert wf["jobs"]["build"]["permissions"] == {"contents": "read"}
    assert wf["jobs"]["sign"]["permissions"] == {"contents": "read"}
    assert wf["jobs"]["publish"]["permissions"] == {
        "contents": "write",
        "id-token": "write",
        "attestations": "write",
    }


@pytest.mark.parametrize("job", ["build", "sign"])
def test_build_and_sign_run_on_apple_silicon_macos_15_or_later(wf, job):
    runner = wf["jobs"][job]["runs-on"]
    m = re.fullmatch(r"macos-(\d+)", runner)
    assert m and int(m.group(1)) >= 15, runner  # macos-NN images are arm64; -large/-intel ones are not


def test_only_the_sign_job_has_an_environment_and_it_is_release(wf):
    assert {name: job.get("environment") for name, job in wf["jobs"].items()} == {
        "build": None, "sign": "release", "publish": None}
    assert wf["jobs"]["sign"]["needs"] == "build"
    assert wf["jobs"]["publish"]["needs"] == ["build", "sign"]


# ---- secrets -------------------------------------------------------------------------------------------------------


def secret_refs(node) -> set[str]:
    return set(re.findall(r"secrets\.([A-Z0-9_]+)", as_text(node)))


def test_the_secrets_are_the_five_the_script_sets_and_only_the_sign_job_sees_them(wf):
    assert secret_refs(wf["jobs"]["sign"]) == SECRET_NAMES
    assert secret_refs(wf["jobs"]["build"]) == set()
    assert secret_refs(wf["jobs"]["publish"]) == set()
    assert not re.search(r"secrets\.[A-Z]", as_text({k: v for k, v in wf.items() if k != "jobs"}))
    script = SECRETS_SCRIPT.read_text()
    assert set(re.findall(r"set_secret ([A-Z0-9_]+)", script)) == SECRET_NAMES


def test_secrets_reach_steps_only_through_env_never_inside_a_script_or_an_action_input(wf):
    for job in wf["jobs"].values():
        for s in job["steps"]:
            assert not re.search(r"secrets\.[A-Z]", s.get("run", "")), s.get("name")
            assert not re.search(r"secrets\.[A-Z]", as_text(s.get("with", {}))), s.get("name")
            for key, value in (s.get("env") or {}).items():
                if "secrets." in str(value):
                    assert re.fullmatch(r"\$\{\{ secrets\.([A-Z0-9_]+) \}\}", value) and key in SECRET_NAMES


def test_no_step_traces_its_shell_or_prints_a_secret_variable(wf):
    for job in wf["jobs"].values():
        for s in job["steps"]:
            run = s.get("run", "")
            assert not re.search(r"set -[a-z]*x|set -o xtrace|bash -x", run), s.get("name")
            for name in SECRET_NAMES:
                for line in run.splitlines():
                    if re.search(rf"\b(echo|printf|cat)\b.*\${{?{name}\b", line):
                        # the only use allowed: piped straight into a decoder
                        assert re.search(r"\|\s*base64 --decode >", line), (s.get("name"), line)


def test_secret_files_are_written_with_tight_permissions_and_removed(wf):
    for name in ("Import Developer ID certificate", "Notarize"):
        run = step(wf, "sign", name)["run"]
        assert "umask 077" in run
    notarize = step(wf, "sign", "Notarize")["run"]
    assert notarize.count('rm -f "$KEY"') >= 2  # on success and before the failure exit
    assert 'rm -f "$P12"' in step(wf, "sign", "Import Developer ID certificate")["run"]


def test_the_keychain_is_deleted_after_signing_and_again_by_an_always_fallback(wf):
    names = [s.get("name") for s in steps(wf, "sign")]
    assert names.index("Remove the signing keychain") == names.index("Sign") + 1
    fallback = steps(wf, "sign")[-1]
    assert fallback["if"] == "always()"
    assert "security delete-keychain" in fallback["run"]
    for leftover in ("developer-id.p12", "AuthKey.p8"):
        assert leftover in fallback["run"]


def test_the_build_job_builds_and_hands_over_an_unsigned_bundle(wf):
    assert step(wf, "build", "Build")["run"].strip() == "app/build_app.sh"
    assert "env" not in step(wf, "build", "Build")
    assert "ditto -c -k --keepParent" in step(wf, "build", "Pack the unsigned bundle")["run"]  # keeps file modes
    upload = step(wf, "build", "Upload the unsigned bundle")
    download = step(wf, "sign", "Download the unsigned bundle")
    assert upload["with"]["name"] == download["with"]["name"] == "Framescribe-unsigned"
    unpack = step(wf, "sign", "Unpack the unsigned bundle")["run"]
    assert "shasum -a 256 -c" in unpack and "ditto -x -k" in unpack


def test_the_sign_job_runs_no_dependency_code_and_no_build(wf):
    sign = wf["jobs"]["sign"]
    text = as_text(sign)
    for forbidden in ("build_app.sh", "setup-uv", "actions/cache", "uv ", "pip ", "python3.12 -", "swift ",
                      "brew ", "npm ", "cache"):
        assert forbidden not in text, forbidden
    uses = [s["uses"].split("@")[0] for s in sign["steps"] if "uses" in s]
    assert uses == ["actions/checkout", "actions/download-artifact", "actions/upload-artifact"]
    runs = "\n".join(s.get("run", "") for s in sign["steps"])
    scripts = set(re.findall(r"app/[\w./-]+\.sh", runs))
    assert scripts == {"app/sign_app.sh", "app/preflight_app.sh", "app/package_app.sh"}
    # the signing script itself runs nothing from the bundle
    body = SIGN.read_text()
    assert "python3.12 -" not in body and "/bin/python" not in body.replace("bin/python3.*", "")
    names = [s.get("name") for s in sign["steps"]]
    assert names.index("Unpack the unsigned bundle") < names.index("Import Developer ID certificate")


# ---- actions and caches ---------------------------------------------------------------------------------------------


def test_every_action_is_pinned_to_a_commit_sha_with_its_version_in_a_comment():
    uses = [line.strip() for line in WORKFLOW.read_text().splitlines() if re.match(r"\s*(- )?uses:", line)]
    assert uses
    for line in uses:
        assert re.fullmatch(r"(- )?uses: [\w.-]+/[\w./-]+@[0-9a-f]{40} # v\d+(\.\d+)*", line), line


def test_nothing_is_cached_between_runs_and_uv_is_pinned(wf):
    for job in wf["jobs"].values():
        for s in job["steps"]:
            assert not s.get("uses", "").startswith("actions/cache"), s
    setups = [(j, s) for j, job in wf["jobs"].items() for s in job["steps"]
              if s.get("uses", "").startswith("astral-sh/setup-uv@")]
    assert [j for j, _ in setups] == ["build"]
    uv = setups[0][1]
    assert uv["with"]["enable-cache"] is False
    assert re.fullmatch(r"\d+\.\d+\.\d+", uv["with"]["version"])
    # the pinned uv must be the one that builds the wheel with its own backend (uv_build>=X.Y,<X.Y+1)
    requires = tomllib.loads((ROOT / "pyproject.toml").read_text())["build-system"]["requires"]
    m = re.fullmatch(r"uv_build>=(\d+)\.(\d+),<(\d+)\.(\d+)", requires[0])
    assert m and uv["with"]["version"].startswith(f"{m.group(1)}.{m.group(2)}.")


def test_the_build_uses_the_lockfile_with_hashes():
    script = BUILD.read_text()
    assert "uv export --project \"$REPO\" --locked" in script
    assert "--require-hashes" in script and "--only-binary :all:" in script


@pytest.mark.parametrize("job", ["build", "sign"])
def test_checkout_is_at_the_triggering_ref_without_persisted_credentials(wf, job):
    checkout = steps(wf, job)[0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"] == {"ref": "${{ github.ref }}", "persist-credentials": False}
    assert not any(s.get("uses", "").startswith("actions/checkout@") for s in steps(wf, "publish"))


# ---- dry run and publish -------------------------------------------------------------------------------------------


def evaluate(expression: str, **context) -> bool:
    """Evaluates the small subset of GitHub's expression syntax the publish condition uses."""
    py = expression.replace("&&", " and ").replace("||", " or ").replace("!", " not ")
    py = re.sub(r"\bfalse\b", "False", re.sub(r"\btrue\b", "True", py))
    py = py.replace(" not =", "!=")
    names = {k: SimpleNamespace(**v) for k, v in context.items()}
    return bool(eval(py, {"__builtins__": {}}, names))  # noqa: S307 (a fixed expression from the repo)


@pytest.mark.parametrize(
    ("event", "ref_type", "dry_run", "publishes"),
    [
        ("push", "tag", None, True),
        ("workflow_dispatch", "tag", False, True),
        ("workflow_dispatch", "tag", True, False),
        ("workflow_dispatch", "branch", True, False),
        ("workflow_dispatch", "branch", False, False),
        ("push", "branch", None, False),
    ],
)
def test_only_a_tag_that_is_not_a_dry_run_reaches_the_publish_job(wf, event, ref_type, dry_run, publishes):
    condition = wf["jobs"]["publish"]["if"]
    got = evaluate(condition, github={"event_name": event, "ref_type": ref_type}, inputs={"dry_run": dry_run})
    assert got is publishes


def test_only_the_publish_job_can_create_or_attest_a_release(wf):
    for job in ("build", "sign"):
        text = as_text(wf["jobs"][job])
        assert "gh release" not in text and "attest" not in text and "github.token" not in text, job
    publish = steps(wf, "publish")
    names = [s.get("name") for s in publish]
    # the download is checked against the build's checksum before it is attested and released
    assert names.index("Check the download against its SHA-256") < names.index("Attest build provenance")
    assert names.index("Attest build provenance") < names.index("Publish release")
    assert "sha256sum -c" in step(wf, "publish", "Check the download against its SHA-256")["run"]
    assert "--verify-tag" in step(wf, "publish", "Publish release")["run"]


def test_the_release_notes_say_how_to_verify_and_what_is_needed(wf):
    notes = step(wf, "publish", "Release notes")["run"]
    for needed in ("shasum -a 256 -c", "gh attestation verify", "spctl --assess", "TeamIdentifier=$TEAM",
                   "macOS 15", "Apple Silicon", "ffmpeg", "2.5 GB"):
        assert needed in notes, needed
    assert wf["jobs"]["sign"]["outputs"]["team"] == "${{ steps.package.outputs.team }}"
    assert step(wf, "publish", "Release notes")["env"]["TEAM"] == "${{ needs.sign.outputs.team }}"
    assert "TeamIdentifier" in step(wf, "sign", "Package")["run"]


def test_the_sign_job_signs_runs_preflight_notarizes_staples_and_assesses(wf):
    names = [s.get("name") for s in steps(wf, "sign")]
    order = ["Sign", "Preflight", "Notarize", "Staple and assess", "Package", "Upload artifact"]
    assert [n for n in names if n in order] == order
    assert step(wf, "sign", "Sign")["run"].startswith("app/sign_app.sh --identity")
    assert "notarytool submit" in step(wf, "sign", "Notarize")["run"]
    assert "--wait" in step(wf, "sign", "Notarize")["run"]
    assert "notarytool log" in step(wf, "sign", "Notarize")["run"]
    staple = step(wf, "sign", "Staple and assess")["run"]
    assert "stapler staple" in staple and "stapler validate" in staple and "spctl --assess" in staple
    assert "package_app.sh" in step(wf, "sign", "Package")["run"]  # the stapled app is zipped again


# ---- the shell of every step ---------------------------------------------------------------------------------------


def run_blocks(wf: dict) -> list[tuple[str, str]]:
    return [(f"{job}: {s.get('name', s.get('uses'))}", s["run"])
            for job, j in wf["jobs"].items() for s in j["steps"] if "run" in s]


def test_every_run_block_is_valid_bash(wf, tmp_path):
    blocks = run_blocks(wf)
    assert len(blocks) >= 12
    for name, script in blocks:
        f = tmp_path / "step.sh"
        f.write_text(re.sub(r"\$\{\{[^}]*\}\}", "EXPR", script))
        result = subprocess.run(["bash", "-n", str(f)], capture_output=True, text=True)
        assert result.returncode == 0, f"{name}: {result.stderr}"


def version_step(wf, tmp_path, **env) -> tuple[int, str]:
    script = tmp_path / "version.sh"
    script.write_text(step(wf, "build", "Version")["run"])
    out = tmp_path / "github_output"
    out.write_text("")
    full = {"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(out), "DRY_RUN": "false", **env}
    r = subprocess.run(["bash", str(script)], cwd=ROOT, env=full, capture_output=True, text=True)
    return r.returncode, out.read_text() + r.stdout


def test_the_version_step_accepts_the_matching_tag_and_names_the_zip(wf, tmp_path):
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    rc, out = version_step(wf, tmp_path, GITHUB_REF_TYPE="tag", GITHUB_REF_NAME=f"v{version}")
    assert rc == 0
    assert f"version={version}\n" in out and f"artifact=Framescribe-{version}.zip\n" in out


def test_the_version_step_refuses_a_tag_that_does_not_match_pyproject(wf, tmp_path):
    rc, out = version_step(wf, tmp_path, GITHUB_REF_TYPE="tag", GITHUB_REF_NAME="v99.0.0")
    assert rc == 1 and "does not match" in out and "version=" not in out


def test_the_version_step_allows_a_branch_only_for_a_dry_run(wf, tmp_path):
    assert version_step(wf, tmp_path, GITHUB_REF_TYPE="branch", GITHUB_REF_NAME="main", DRY_RUN="true")[0] == 0
    rc, out = version_step(wf, tmp_path, GITHUB_REF_TYPE="branch", GITHUB_REF_NAME="main", DRY_RUN="false")
    assert rc == 1 and "needs a tag" in out


# ---- scripts/release-secrets.sh ------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def certs(tmp_path_factory) -> SimpleNamespace:
    """Throwaway PKCS#12 files: one under a made-up CA named like Apple's, one self-signed, both with made-up names.
    Nothing touches a keychain."""
    if not shutil.which("openssl"):
        pytest.skip("openssl not found")
    d = tmp_path_factory.mktemp("certs")
    pw = "throwaway-password"

    def sh(*args: str) -> None:
        subprocess.run(args, cwd=d, check=True, capture_output=True)

    def p12(name: str, key: str, cert: str) -> Path:
        sh("openssl", "pkcs12", "-export", "-inkey", key, "-in", cert, "-out", name, "-passout", f"pass:{pw}")
        return d / name

    sh("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "2",
       "-subj", "/CN=Developer ID Certification Authority/O=Example Test CA")
    sh("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", "leaf.key", "-out", "leaf.csr",
       "-subj", "/CN=Developer ID Application: Example Person (ABCDE12345)/O=Example Person")
    sh("openssl", "x509", "-req", "-in", "leaf.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
       "-out", "leaf.pem", "-days", "2")
    sh("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "self.key", "-out", "self.pem",
       "-days", "2", "-subj", "/CN=Example Self Signed Test")
    sh("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "fake.key", "-out", "fake.pem",
       "-days", "2", "-subj", "/CN=Developer ID Application: Example Person (ABCDE12345)")
    sh("openssl", "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", "AuthKey.p8")
    (d / "bad.p8").write_text("not a key\n")
    return SimpleNamespace(
        dir=d, password=pw, devid=p12("devid.p12", "leaf.key", "leaf.pem"), other=p12("other.p12", "self.key", "self.pem"),
        selfsigned_devid=p12("fake.p12", "fake.key", "fake.pem"), p8=d / "AuthKey.p8", bad_p8=d / "bad.p8",
    )


@pytest.fixture
def gh_stub(tmp_path) -> SimpleNamespace:
    """A `gh` that records its calls and standard input instead of talking to GitHub."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "gh.log"
    stub = bindir / "gh"
    stub.write_text(f'#!/bin/bash\nprintf "ARGS %s\\n" "$*" >> "{log}"\nif [[ "$1 $2" == "secret set" ]]; then '
                    f'printf "STDIN %s\\n" "$(cat)" >> "{log}"; fi\n'
                    f'if [[ "$1" == api && "$2" == */environments/missing ]]; then exit 1; fi\nexit 0\n')
    stub.chmod(0o755)
    return SimpleNamespace(path=f"{bindir}:{os.environ['PATH']}", log=log)


def secrets(certs, gh_stub, p12: Path, *extra: str, password: str | None = None, p8: Path | None = None,
            key_id: str = "ABCDEF1234") -> subprocess.CompletedProcess:
    args = ["bash", str(SECRETS_SCRIPT), "--p12", str(p12), "--p8", str(p8 or certs.p8), "--key-id", key_id,
            "--issuer", "12345678-aaaa-bbbb-cccc-1234567890ab", *extra]
    return subprocess.run(args, input=f"{password or certs.password}\n", capture_output=True, text=True,
                          env={**os.environ, "PATH": gh_stub.path})


def test_the_secrets_script_is_valid_bash():
    assert subprocess.run(["bash", "-n", str(SECRETS_SCRIPT)]).returncode == 0


def test_a_dry_run_checks_everything_names_the_five_secrets_and_never_calls_gh(certs, gh_stub):
    r = secrets(certs, gh_stub, certs.devid, "--dry-run", "--repo", "example/repo")
    assert r.returncode == 0, r.stderr
    assert "Certificate: Developer ID Application: Example Person (ABCDE12345)" in r.stdout
    assert set(re.findall(r"would set (\w+) in environment release", r.stdout)) == SECRET_NAMES
    assert "nothing was set" in r.stdout
    assert not gh_stub.log.exists()
    assert certs.password not in r.stdout + r.stderr


def test_a_real_run_hands_every_value_to_gh_on_stdin_never_as_an_argument(certs, gh_stub):
    r = secrets(certs, gh_stub, certs.devid, "--repo", "example/repo")
    assert r.returncode == 0, r.stderr
    log = gh_stub.log.read_text()
    assert "ARGS api repos/example/repo/environments/release --silent" in log
    calls = [line for line in log.splitlines() if line.startswith("ARGS secret set")]
    assert {c.split()[3] for c in calls} == SECRET_NAMES
    # environment secrets, and no --body, no value on the command line
    assert all(c.split()[4:] == ["--env", "release", "--repo", "example/repo"] for c in calls)
    assert f"STDIN {certs.password}" in log and "STDIN ABCDEF1234" in log
    assert certs.password not in r.stdout + r.stderr
    assert "rm -P" in r.stdout  # the reminder about the exported files


def test_a_missing_environment_stops_before_any_secret_is_set(certs, gh_stub):
    r = secrets(certs, gh_stub, certs.devid, "--repo", "example/repo", "--env", "missing")
    assert r.returncode == 1 and 'the environment "missing" does not exist' in r.stderr
    assert "secret set" not in gh_stub.log.read_text()


def test_a_certificate_that_is_not_developer_id_application_is_refused(certs, gh_stub):
    r = secrets(certs, gh_stub, certs.other, "--dry-run")
    assert r.returncode == 1 and "is not a Developer ID Application certificate" in r.stderr


def test_a_developer_id_name_on_a_certificate_apple_did_not_issue_is_refused(certs, gh_stub):
    r = secrets(certs, gh_stub, certs.selfsigned_devid, "--dry-run")
    assert r.returncode == 1 and "not issued by Apple's Developer ID Certification Authority" in r.stderr


def test_a_wrong_password_a_bad_p8_and_a_bad_key_id_are_refused(certs, gh_stub):
    assert "could not open" in secrets(certs, gh_stub, certs.devid, "--dry-run", password="wrong").stderr
    assert "App Store Connect API key" in secrets(certs, gh_stub, certs.devid, "--dry-run", p8=certs.bad_p8).stderr
    assert "--key-id" in secrets(certs, gh_stub, certs.devid, "--dry-run", key_id="abc").stderr


# ---- app/package_app.sh and app/preflight_app.sh -------------------------------------------------------------------


def make_bundle(root: Path, *, runtime: bool = True) -> Path:
    """Fake.app: copies of /bin/echo as the app's executable and as runtime/bin/python3.12, signed by app/sign_app.sh
    (with --hardened: the hardened runtime and the test entitlements, as build_app.sh --hardened signs)."""
    app = root / "Fake.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    bindir = app / "Contents" / "Resources" / "runtime" / "bin"
    bindir.mkdir(parents=True)
    plist = {"CFBundleExecutable": "Fake", "CFBundleName": "Fake", "CFBundleShortVersionString": "1.2.3",
             "CFBundleIdentifier": "org.example.fake", "CFBundlePackageType": "APPL"}
    body = "".join(f"<key>{k}</key><string>{v}</string>" for k, v in plist.items())
    (app / "Contents" / "Info.plist").write_text(
        '<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>' + body + "</dict></plist>")
    shutil.copy("/bin/echo", app / "Contents" / "MacOS" / "Fake")
    shutil.copy("/bin/echo", bindir / "python3.12")
    (app / "Contents" / "Resources" / "data.txt").write_text("data\n")
    r = subprocess.run(["bash", str(SIGN), *(["--hardened"] if runtime else []), str(app)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return app


def preflight(app: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(PREFLIGHT), *args, str(app)], capture_output=True, text=True)


@macos_only
def test_preflight_passes_a_hardened_adhoc_bundle_in_test_mode(tmp_path):
    r = preflight(make_bundle(tmp_path), "--allow-adhoc")
    assert r.returncode == 0, r.stdout
    assert "FAIL" not in r.stdout and "RESULT: every check passed" in r.stdout
    assert "PASS  hardened runtime: all 2 Mach-O files" in r.stdout


@macos_only
def test_preflight_as_the_release_gate_refuses_an_adhoc_bundle(tmp_path):
    r = preflight(make_bundle(tmp_path))
    assert r.returncode == 1
    assert "FAIL  Developer ID: 2 Mach-O files are signed ad hoc" in r.stdout
    assert "FAIL  secure timestamp" in r.stdout
    assert "FAIL  Team ID" in r.stdout
    assert "FAIL  entitlements of runtime/bin/python3.12" in r.stdout  # the test entitlements are not the release's


@macos_only
def test_preflight_refuses_code_without_the_hardened_runtime(tmp_path):
    r = preflight(make_bundle(tmp_path, runtime=False), "--allow-adhoc")
    assert r.returncode == 1
    assert "FAIL  hardened runtime: 2 Mach-O files lack the runtime flag" in r.stdout
    assert "FAIL  entitlements of runtime/bin/python3.12: none" in r.stdout  # plain ad hoc signs without them
    assert "RESULT: 2 check(s) failed" in r.stdout


@macos_only
def test_preflight_finds_what_notarization_rejects(tmp_path):
    app = make_bundle(tmp_path)
    res = app / "Contents" / "Resources"
    (res / "outside").symlink_to("/etc/hosts")
    (res / "libthing.a").write_bytes(b"!<arch>\n")
    (app / "Contents" / "MacOS" / "notes.txt").write_text("not code\n")
    subprocess.run(["xattr", "-w", "com.apple.quarantine", "0081;00000000;Test;", str(res / "data.txt")], check=True)
    r = preflight(app, "--allow-adhoc")
    assert r.returncode == 1
    # the app's executable verifies as part of its bundle, whose sealed contents changed
    for line in ("FAIL  signature: 1 of 2 Mach-O files are unsigned or do not verify",
                 "FAIL  symbolic links: 1 point outside the bundle",
                 "FAIL  static libraries: 1 found",
                 "FAIL  Contents/MacOS: 1 entries are not Mach-O files",
                 "FAIL  extended attributes: 1 files carry quarantine",
                 "FAIL  codesign --verify --deep --strict on the bundle"):
        assert line in r.stdout, line


@macos_only
def test_package_makes_a_ditto_zip_with_the_app_at_its_top_and_a_checksum_file(tmp_path):
    app = make_bundle(tmp_path / "src")
    out = tmp_path / "dist"
    r = subprocess.run(["bash", str(PACKAGE), "--out", str(out), str(app)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in out.iterdir()) == ["Fake-1.2.3.zip", "Fake-1.2.3.zip.sha256"]
    assert subprocess.run(["shasum", "-a", "256", "-c", "Fake-1.2.3.zip.sha256"], cwd=out,
                          capture_output=True).returncode == 0
    unpacked = tmp_path / "unpacked"
    subprocess.run(["ditto", "-x", "-k", str(out / "Fake-1.2.3.zip"), str(unpacked)], check=True)
    assert [p.name for p in unpacked.iterdir()] == ["Fake.app"]
    assert subprocess.run(["codesign", "--verify", "--deep", "--strict", str(unpacked / "Fake.app")]).returncode == 0


# ---- app/build_app.sh options (parsed before anything is built) ----------------------------------------------------


def build_app(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(BUILD), *args], capture_output=True, text=True)


def test_build_help_lists_the_release_options():
    out = build_app("--help").stdout
    for option in ("--hardened", "--sign", "--keychain", "--notarize", "--zip", "--out", "--min-macos"):
        assert option in out, option


def test_build_refuses_notarizing_or_a_keychain_without_a_developer_id():
    assert "--notarize needs --sign" in build_app("--notarize", "profile").stderr
    assert "--keychain needs --sign" in build_app("--keychain", str(BUILD)).stderr
    assert "--sign needs an identity" in build_app("--sign", "").stderr


@macos_only
def test_sign_app_gives_the_python_executable_the_entitlements_and_the_app_none(tmp_path):
    app = make_bundle(tmp_path)
    py = app / "Contents" / "Resources" / "runtime" / "bin" / "python3.12"
    ent = subprocess.run(["codesign", "-d", "--entitlements", "-", "--xml", str(py)], capture_output=True, text=True)
    assert "allow-unsigned-executable-memory" in ent.stdout and "disable-library-validation" in ent.stdout
    assert "get-task-allow" not in ent.stdout
    main = subprocess.run(["codesign", "-d", "--entitlements", "-", "--xml", str(app)], capture_output=True, text=True)
    assert main.stdout == ""
    info = subprocess.run(["codesign", "-dv", str(py)], capture_output=True, text=True).stderr
    assert "flags=0x10002(adhoc,runtime)" in info


def test_sign_app_refuses_a_keychain_without_an_identity(tmp_path):
    r = subprocess.run(["bash", str(SIGN), "--keychain", str(BUILD), str(tmp_path)], capture_output=True, text=True)
    assert r.returncode == 1


@macos_only
def test_clean_removes_only_what_the_build_made(tmp_path):
    """--clean with --out pointing at a folder that holds other files: the script's own output goes, the rest stays.
    A copy of the script runs from a scratch app/ folder (so its app/.build is a scratch one too) and stops at the
    Swift build, after cleaning, because `swift` is a stub that fails."""
    repo = tmp_path / "repo"
    (repo / "app" / ".build").mkdir(parents=True)
    shutil.copy(BUILD, repo / "app" / "build_app.sh")
    shutil.copy(ROOT / "pyproject.toml", repo / "pyproject.toml")
    out = tmp_path / "out"
    for made in ("work/runtime", "Framescribe.app/Contents"):
        (out / made).mkdir(parents=True)
    (out / "Framescribe-0.1.0.zip").write_text("zip")
    (out / "Framescribe-0.1.0.zip.sha256").write_text("sum")
    (out / "keep.txt").write_text("not the script's")
    (out / "keep").mkdir()
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "swift").write_text("#!/bin/bash\nexit 1\n")
    (stubs / "swift").chmod(0o755)
    r = subprocess.run(["bash", str(repo / "app" / "build_app.sh"), "--out", str(out), "--clean"], capture_output=True,
                       text=True, env={**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}"})
    assert r.returncode != 0 and "==> clean:" in r.stdout  # cleaned, then stopped at the stubbed Swift build
    assert sorted(p.name for p in out.iterdir()) == ["keep", "keep.txt", "work"]  # work/ is made again, empty
    assert list((out / "work").iterdir()) == []
    assert not (repo / "app" / ".build").exists()
