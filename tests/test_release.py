"""The release pipeline, offline: the workflow's security properties (read from the YAML), every shell block it runs
(bash -n, and the version check executed), the secrets script against throwaway certificates and a stub `gh`, and the
structure check, signing, packaging and preflight scripts against a tiny bundle made of copies of a system binary.
Nothing here calls GitHub or Apple."""

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
CHECK = ROOT / "app" / "bundle_check.sh"
BUNDLE_ID = "io.github.jwiderberg.framescribe"
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
    assert on["push"] == {"tags": ["v*", "!v*-dry*"]}  # a pushed dry-run tag starts nothing
    dry = on["workflow_dispatch"]["inputs"]["dry_run"]
    assert dry["type"] == "boolean" and dry["default"] is True  # a manual run is a dry run unless asked otherwise


def test_the_token_is_read_only_at_the_top_and_each_job_asks_only_for_what_it_needs(wf):
    assert wf["permissions"] == {"contents": "read"}
    assert set(wf["jobs"]) == {"build", "sign", "verify", "publish"}
    assert wf["jobs"]["build"]["permissions"] == {"contents": "read"}
    assert wf["jobs"]["sign"]["permissions"] == {"contents": "read"}
    assert wf["jobs"]["verify"]["permissions"] == {"contents": "read"}
    assert wf["jobs"]["publish"]["permissions"] == {
        "contents": "write",
        "id-token": "write",
        "attestations": "write",
    }


@pytest.mark.parametrize("job", ["build", "sign", "verify"])
def test_build_and_sign_run_on_apple_silicon_macos_15_or_later(wf, job):
    runner = wf["jobs"][job]["runs-on"]
    m = re.fullmatch(r"macos-(\d+)", runner)
    assert m and int(m.group(1)) >= 15, runner  # macos-NN images are arm64; -large/-intel ones are not


def test_only_the_sign_job_has_an_environment_and_it_is_release(wf):
    assert {name: job.get("environment") for name, job in wf["jobs"].items()} == {
        "build": None, "sign": "release", "verify": None, "publish": None}
    assert wf["jobs"]["sign"]["needs"] == "build"
    assert wf["jobs"]["verify"]["needs"] == ["build", "sign"]
    assert wf["jobs"]["publish"]["needs"] == ["build", "sign", "verify"]  # nothing is published unless verify passed


# ---- secrets -------------------------------------------------------------------------------------------------------


def secret_refs(node) -> set[str]:
    return set(re.findall(r"secrets\.([A-Z0-9_]+)", as_text(node)))


def test_the_secrets_are_the_five_the_script_sets_and_only_the_sign_job_sees_them(wf):
    assert secret_refs(wf["jobs"]["sign"]) == SECRET_NAMES
    assert secret_refs(wf["jobs"]["build"]) == set()
    assert secret_refs(wf["jobs"]["verify"]) == set()
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
    for leftover in ("developer-id.p12", "developer-id-import.p12", "AuthKey.p8"):
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
    assert scripts == {"app/bundle_check.sh", "app/sign_app.sh", "app/preflight_app.sh", "app/package_app.sh"}
    # the signing script itself runs nothing from the bundle
    body = SIGN.read_text()
    assert "python3.12 -" not in body and "/bin/python" not in body.replace("bin/python3.*", "")
    names = [s.get("name") for s in sign["steps"]]
    assert names.index("Unpack the unsigned bundle") < names.index("Check the bundle structure") \
        < names.index("Import Developer ID certificate")


# ---- actions and caches ---------------------------------------------------------------------------------------------


def test_every_action_is_pinned_to_a_commit_sha_with_its_version_in_a_comment():
    uses = [line.strip() for line in WORKFLOW.read_text().splitlines() if re.match(r"\s*(- )?uses:", line)]
    assert uses
    for line in uses:
        assert re.fullmatch(r"(- )?uses: [\w.-]+/[\w./-]+@[0-9a-f]{40} # v\d+(\.\d+)*", line), line


def test_nothing_is_cached_between_runs_and_uv_is_pinned_with_its_sha256(wf):
    for job in wf["jobs"].values():
        for s in job["steps"]:
            assert not s.get("uses", "").startswith("actions/cache"), s
            assert not s.get("uses", "").startswith("astral-sh/"), s  # uv comes from its release, checked by SHA-256
    installs = [(j, s) for j, job in wf["jobs"].items() for s in job["steps"] if s.get("name") == "Install uv"]
    assert [j for j, _ in installs] == ["build"]
    uv = installs[0][1]
    assert re.fullmatch(r"\d+\.\d+\.\d+", uv["env"]["UV_VERSION"])
    assert re.fullmatch(r"[0-9a-f]{64}", uv["env"]["UV_SHA256"])
    run = uv["run"]
    assert 'shasum -a 256 -c -' in run and '"$UV_SHA256  $RUNNER_TEMP/uv.tar.gz"' in run
    assert run.index("shasum -a 256 -c") < run.index("tar -xzf") < run.index("GITHUB_PATH")  # checked before use
    assert "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-aarch64-apple-darwin.tar.gz" in run
    names = [s.get("name") for s in steps(wf, "build")]
    assert names.index("Install uv") < names.index("Build")
    # the pinned uv must be the one that builds the wheel with its own backend (uv_build>=X.Y,<X.Y+1)
    requires = tomllib.loads((ROOT / "pyproject.toml").read_text())["build-system"]["requires"]
    m = re.fullmatch(r"uv_build>=(\d+)\.(\d+),<(\d+)\.(\d+)", requires[0])
    assert m and uv["env"]["UV_VERSION"].startswith(f"{m.group(1)}.{m.group(2)}.")


def test_every_action_is_one_of_githubs_own(wf):
    """The Actions allowlist then needs only "Allow actions created by GitHub"."""
    uses = {s["uses"].split("@")[0] for job in wf["jobs"].values() for s in job["steps"] if "uses" in s}
    assert uses == {"actions/checkout", "actions/upload-artifact", "actions/download-artifact",
                    "actions/attest-build-provenance"}


def test_the_build_uses_the_lockfile_with_hashes():
    script = BUILD.read_text()
    assert "uv export --project \"$REPO\" --locked" in script
    assert "--require-hashes" in script and "--only-binary :all:" in script


@pytest.mark.parametrize("job", ["build", "sign", "verify"])
def test_checkout_is_at_the_triggering_commit_without_persisted_credentials(wf, job):
    checkout = steps(wf, job)[0]
    assert checkout["uses"].startswith("actions/checkout@")
    # the commit the run started for: a tag moved while the run waits for approval does not change what is signed
    assert checkout["with"] == {"ref": "${{ github.sha }}", "persist-credentials": False}


def test_the_publish_job_checks_out_install_sh_alone_and_runs_nothing_from_it(wf):
    """The installer's checksum comes from the commit the run started for, in the publish job: not from the build
    job, whose Build step runs dependency code that could change the workspace before a checksum step there."""
    publish = steps(wf, "publish")
    checkouts = [s for s in publish if s.get("uses", "").startswith("actions/checkout@")]
    assert checkouts == [publish[0]]
    assert publish[0]["with"] == {"ref": "${{ github.sha }}", "persist-credentials": False, "path": "tree",
                                  "sparse-checkout": "install.sh", "sparse-checkout-cone-mode": False}
    checksum = step(wf, "publish", "Installer checksum")["run"]
    assert 'sha256sum tree/install.sh' in checksum
    for s in publish[1:]:  # no step runs or sources a file of the checkout
        assert "tree/" not in s.get("run", "").replace("sha256sum tree/install.sh", ""), s.get("name")
    assert "install_sha256" not in wf["jobs"]["build"].get("outputs", {})
    assert not any(s.get("name") == "Installer checksum" for s in steps(wf, "build"))
    assert "install.sh" not in as_text(steps(wf, "build"))


# ---- dry run and publish -------------------------------------------------------------------------------------------


def evaluate(expression: str, **context) -> bool:
    """Evaluates the small subset of GitHub's expression syntax the publish condition uses."""
    # the literals true and false outside quotes; 'true' in quotes is a string (an output compared as text)
    parts = re.split(r"('[^']*')", expression)
    for i in range(0, len(parts), 2):
        part = parts[i].replace("&&", " and ").replace("||", " or ").replace("!", " not ")
        part = re.sub(r"\bfalse\b", "False", re.sub(r"\btrue\b", "True", part))
        parts[i] = part.replace(" not =", "!=")
    py = "".join(parts)

    def ns(value):
        return SimpleNamespace(**{k: ns(v) for k, v in value.items()}) if isinstance(value, dict) else value

    names = {k: ns(v) for k, v in context.items()}
    names["contains"] = lambda haystack, needle: needle.lower() in (haystack or "").lower()
    return bool(eval(py, {"__builtins__": {}}, names))  # noqa: S307 (a fixed expression from the repo)


@pytest.mark.parametrize(
    ("event", "ref_type", "ref_name", "dry_run", "publish_output", "publishes"),
    [
        ("push", "tag", "v0.1.0", None, "true", True),
        ("workflow_dispatch", "tag", "v0.1.0", False, "true", True),
        ("workflow_dispatch", "tag", "v0.1.0", True, "false", False),
        ("workflow_dispatch", "tag", "v0.1.0-dry1", True, "false", False),
        ("workflow_dispatch", "tag", "v0.1.0-dry1", False, "true", False),  # even if the version step said so
        ("workflow_dispatch", "branch", "main", True, "false", False),
        ("workflow_dispatch", "branch", "main", False, "true", False),
        ("push", "branch", "main", None, "true", False),
        ("push", "tag", "v0.1.0", None, "false", False),  # the version step refused: publish stays off
    ],
)
def test_only_a_release_tag_that_is_not_a_dry_run_reaches_the_publish_job(wf, event, ref_type, ref_name, dry_run,
                                                                          publish_output, publishes):
    condition = wf["jobs"]["publish"]["if"]
    got = evaluate(condition, github={"event_name": event, "ref_type": ref_type, "ref_name": ref_name},
                   inputs={"dry_run": dry_run}, needs={"build": {"outputs": {"publish": publish_output}}})
    assert got is publishes


def test_only_the_publish_job_can_create_or_attest_a_release(wf):
    for job in ("build", "sign", "verify"):
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
                   "macOS 15", "Apple Silicon", "ffmpeg", "2.5 GB", "shasum -a 256 install.sh", "$INSTALL_SHA256",
                   "raw.githubusercontent.com/$GITHUB_REPOSITORY/$GITHUB_REF_NAME/install.sh"):
        assert needed in notes, needed
    assert step(wf, "publish", "Release notes")["env"]["INSTALL_SHA256"] == "${{ steps.installer.outputs.sha256 }}"
    names = [s.get("name") for s in steps(wf, "publish")]
    assert names.index("Installer checksum") < names.index("Release notes")
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
    full = {"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(out), "DRY_RUN": "false", "EVENT": "push", **env}
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


def test_the_version_step_refuses_a_branch_even_for_a_dry_run(wf, tmp_path):
    """The release environment admits tags only, so a run from a branch could never sign: it stops at once."""
    for dry in ("true", "false"):
        rc, out = version_step(wf, tmp_path, GITHUB_REF_TYPE="branch", GITHUB_REF_NAME="main", DRY_RUN=dry,
                               EVENT="workflow_dispatch")
        assert rc == 1 and "needs a tag" in out and "version=" not in out


def pyproject_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


@pytest.mark.parametrize(
    ("event", "suffix", "dry_run", "outcome"),
    [
        ("push", "", "false", "publish"),                   # a real tag pushed: a release
        ("workflow_dispatch", "", "false", "publish"),      # run by hand on the tag with dry_run off: a release
        ("workflow_dispatch", "", "true", "dry"),           # run by hand on the tag with dry_run on
        ("workflow_dispatch", "-dry1", "true", "dry"),      # the dry run: signs, notarizes, verifies, publishes nothing
        ("workflow_dispatch", "-dry12", "true", "dry"),
        ("workflow_dispatch", "-dry1", "false", "refused"),  # a dry-run tag never publishes
        ("push", "-dry1", "false", "refused"),              # (the trigger ignores it; were it to start, it stops)
        ("workflow_dispatch", "-dry", "true", "refused"),   # the number is required
        ("workflow_dispatch", "-dry1x", "true", "refused"),
        ("workflow_dispatch", "-rc1", "true", "refused"),
    ],
)
def test_the_version_step_decides_release_dry_run_or_refusal(wf, tmp_path, event, suffix, dry_run, outcome):
    version = pyproject_version()
    rc, out = version_step(wf, tmp_path, GITHUB_REF_TYPE="tag", GITHUB_REF_NAME=f"v{version}{suffix}",
                           EVENT=event, DRY_RUN=dry_run)
    if outcome == "refused":
        assert rc == 1 and "version=" not in out, out
    else:
        assert rc == 0, out
        assert f"version={version}\n" in out and f"artifact=Framescribe-{version}.zip\n" in out
        assert f"publish={'true' if outcome == 'publish' else 'false'}\n" in out


def tag_filter_matches(patterns: list[str], tag: str) -> bool:
    """GitHub's filter for `on.push.tags`: `*` matches anything but `/`, a `!` pattern excludes, the last pattern that
    matches decides."""
    result = False
    for pattern in patterns:
        negate = pattern.startswith("!")
        glob = pattern[1:] if negate else pattern
        regex = "".join("[^/]*" if c == "*" else re.escape(c) for c in glob)
        if re.fullmatch(regex, tag):
            result = not negate
    return result


@pytest.mark.parametrize(("tag", "starts"), [
    ("v0.1.0", True), ("v1.2.3", True), ("v0.1.0-dry1", False), ("v0.1.0-dry12", False), ("v0.1.0-dry", False)])
def test_pushing_a_dry_run_tag_starts_nothing(wf, tag, starts):
    assert tag_filter_matches(triggers(wf)["push"]["tags"], tag) is starts


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
    # the old check only looked for these two lines
    (d / "fake.p8").write_text("-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n")
    sh("openssl", "genpkey", "-algorithm", "RSA", "-out", "rsa.p8")
    return SimpleNamespace(
        dir=d, password=pw, devid=p12("devid.p12", "leaf.key", "leaf.pem"), other=p12("other.p12", "self.key", "self.pem"),
        selfsigned_devid=p12("fake.p12", "fake.key", "fake.pem"), p8=d / "AuthKey.p8", bad_p8=d / "bad.p8",
        fake_p8=d / "fake.p8", rsa_p8=d / "rsa.p8",
    )


@pytest.fixture
def gh_stub(tmp_path) -> SimpleNamespace:
    """A `gh` that records its calls and standard input instead of talking to GitHub. `gh api` on the environment
    answers what the script's --jq expressions would print for a well protected one (STUB_ENV and STUB_POLICIES
    change that); the environment "missing" does not exist."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "gh.log"
    stub = bindir / "gh"
    stub.write_text(f'#!/bin/bash\nprintf "ARGS %s\\n" "$*" >> "{log}"\nif [[ "$1 $2" == "secret set" ]]; then '
                    f'printf "STDIN %s\\n" "$(cat)" >> "{log}"; fi\n'
                    f'if [[ "$1" == api && "$2" == */environments/missing* ]]; then exit 1; fi\n'
                    f'if [[ "$1" == api && "$2" == */deployment-branch-policies ]]; then echo "${{STUB_POLICIES-tag:v*}}"; exit 0; fi\n'
                    f'if [[ "$1" == api && "$2" == */environments/* ]]; then '
                    f'echo "${{STUB_ENV-reviewers=1 custom=true protected=false admins=false}}"; exit 0; fi\nexit 0\n')
    stub.chmod(0o755)
    return SimpleNamespace(path=f"{bindir}:{os.environ['PATH']}", log=log)


def secrets(certs, gh_stub, p12: Path, *extra: str, password: str | None = None, p8: Path | None = None,
            key_id: str = "ABCDEF1234", env: dict | None = None) -> subprocess.CompletedProcess:
    args = ["bash", str(SECRETS_SCRIPT), "--p12", str(p12), "--p8", str(p8 or certs.p8), "--key-id", key_id,
            "--issuer", "12345678-aaaa-bbbb-cccc-1234567890ab", *extra]
    return subprocess.run(args, input=f"{password or certs.password}\n", capture_output=True, text=True,
                          env={**os.environ, "PATH": gh_stub.path, **(env or {})})


def test_the_secrets_script_is_valid_bash():
    assert subprocess.run(["bash", "-n", str(SECRETS_SCRIPT)]).returncode == 0


def test_a_dry_run_checks_everything_names_the_five_secrets_and_only_reads_from_gh(certs, gh_stub):
    r = secrets(certs, gh_stub, certs.devid, "--dry-run", "--repo", "example/repo")
    assert r.returncode == 0, r.stderr
    assert "Certificate: Developer ID Application: Example Person (ABCDE12345)" in r.stdout
    assert set(re.findall(r"would set (\w+) in environment release", r.stdout)) == SECRET_NAMES
    assert "nothing was set" in r.stdout
    assert "Environment release: 1 required reviewer(s), deployment policies: tag:v*" in r.stdout
    calls = gh_stub.log.read_text().splitlines()
    # read-only: auth status and two GET requests on the environment; nothing is set
    assert [c.split()[1] for c in calls] == ["auth", "api", "api"]
    assert not any("secret" in c or "-X" in c or "--method" in c for c in calls)
    assert certs.password not in r.stdout + r.stderr


def test_a_real_run_hands_every_value_to_gh_on_stdin_never_as_an_argument(certs, gh_stub):
    r = secrets(certs, gh_stub, certs.devid, "--repo", "example/repo")
    assert r.returncode == 0, r.stderr
    log = gh_stub.log.read_text()
    assert "ARGS api repos/example/repo/environments/release --jq" in log
    assert "ARGS api repos/example/repo/environments/release/deployment-branch-policies --jq" in log
    assert log.index("deployment-branch-policies") < log.index("secret set")  # checked before anything is set
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


@pytest.mark.parametrize(("stub_env", "stub_policies", "message"), [
    ("reviewers=0 custom=true protected=false", "tag:v*", "has no required reviewer"),
    ("reviewers=1 custom=false protected=false", "", "does not limit deployments to selected tags"),
    ("reviewers=1 custom=false protected=true", "", "does not limit deployments to selected tags"),
    ("reviewers=1 custom=true protected=false", "", "does not limit deployments to selected tags"),
    ("reviewers=1 custom=true protected=false", "tag:v* branch:main", "admits deployments from a branch (branch:main)"),
])
@pytest.mark.parametrize("dry_run", [True, False])
def test_an_unprotected_environment_stops_before_any_secret_is_set(certs, gh_stub, stub_env, stub_policies, message,
                                                                   dry_run):
    r = secrets(certs, gh_stub, certs.devid, "--repo", "example/repo", *(["--dry-run"] if dry_run else []),
                env={"STUB_ENV": stub_env, "STUB_POLICIES": stub_policies})
    assert r.returncode == 1 and message in r.stderr and "Required reviewers" in r.stderr, r.stderr
    assert "secret set" not in gh_stub.log.read_text()
    assert "would set" not in r.stdout


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("admins", ["admins=true", ""], ids=["can_admins_bypass true", "field missing"])
def test_an_environment_administrators_can_bypass_is_refused_unless_allowed(certs, gh_stub, admins, dry_run):
    env = {"STUB_ENV": f"reviewers=1 custom=true protected=false {admins}".strip()}
    mode = ["--dry-run"] if dry_run else []
    r = secrets(certs, gh_stub, certs.devid, "--repo", "example/repo", *mode, env=env)
    assert r.returncode == 1 and "administrators can bypass the protection rules" in r.stderr, r.stderr
    assert "untick Allow administrators to bypass configured protection rules" in r.stderr
    assert "--allow-admin-bypass" in r.stderr
    assert "secret set" not in gh_stub.log.read_text() and "would set" not in r.stdout
    r = secrets(certs, gh_stub, certs.devid, "--repo", "example/repo", *mode, "--allow-admin-bypass", env=env)
    assert r.returncode == 0, r.stderr
    assert "WARNING: repository administrators can bypass" in r.stderr
    assert ("would set" in r.stdout) if dry_run else ("secret set" in gh_stub.log.read_text())


def test_the_jq_expression_reads_can_admins_bypass_and_treats_a_missing_field_as_true():
    if not shutil.which("jq"):
        pytest.skip("jq not found")
    expr = re.search(r"--jq '(\"reviewers=[^']*)'", SECRETS_SCRIPT.read_text()).group(1)
    for doc, want in (('{"can_admins_bypass": false}', "admins=false"), ('{"can_admins_bypass": true}', "admins=true"),
                      ("{}", "admins=true")):
        out = subprocess.run(["jq", "-r", expr], input=doc, capture_output=True, text=True, check=True).stdout
        assert out.strip().endswith(want), (doc, out)


def test_a_certificate_that_is_not_developer_id_application_is_refused(certs, gh_stub):
    r = secrets(certs, gh_stub, certs.other, "--dry-run")
    assert r.returncode == 1 and "is not a Developer ID Application certificate" in r.stderr


def test_a_developer_id_name_on_a_certificate_apple_did_not_issue_is_refused(certs, gh_stub):
    r = secrets(certs, gh_stub, certs.selfsigned_devid, "--dry-run")
    assert r.returncode == 1 and "not issued by Apple's Developer ID Certification Authority" in r.stderr


def test_a_wrong_password_a_bad_p8_and_a_bad_key_id_are_refused(certs, gh_stub):
    assert "could not open" in secrets(certs, gh_stub, certs.devid, "--dry-run", password="wrong").stderr
    assert "App Store Connect API key" in secrets(certs, gh_stub, certs.devid, "--dry-run", p8=certs.bad_p8).stderr
    # parsed as a key now, not just looked at: two PEM lines around junk, and an RSA key, are refused
    assert "does not parse" in secrets(certs, gh_stub, certs.devid, "--dry-run", p8=certs.fake_p8).stderr
    assert "not an EC private key" in secrets(certs, gh_stub, certs.devid, "--dry-run", p8=certs.rsa_p8).stderr
    assert "--key-id" in secrets(certs, gh_stub, certs.devid, "--dry-run", key_id="abc").stderr


# ---- app/package_app.sh and app/preflight_app.sh -------------------------------------------------------------------


def write_plist(app: Path, **overrides: str) -> None:
    plist = {"CFBundleExecutable": "Framescribe", "CFBundleName": "Framescribe", "CFBundleShortVersionString": "1.2.3",
             "CFBundleIdentifier": BUNDLE_ID, "CFBundlePackageType": "APPL", **overrides}
    body = "".join(f"<key>{k}</key><string>{v}</string>" for k, v in plist.items())
    (app / "Contents" / "Info.plist").write_text(
        '<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>' + body + "</dict></plist>")


def make_unsigned_bundle(root: Path) -> Path:
    """A tiny Framescribe.app with the layout of the real one: copies of /bin/echo as the app's executable and as
    runtime/bin/python3.12, a data file, and the standard library folder."""
    app = root / "Framescribe.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    runtime = app / "Contents" / "Resources" / "runtime"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "lib" / "python3.12").mkdir(parents=True)
    write_plist(app)
    shutil.copy("/bin/echo", app / "Contents" / "MacOS" / "Framescribe")
    shutil.copy("/bin/echo", runtime / "bin" / "python3.12")
    (app / "Contents" / "Resources" / "data.txt").write_text("data\n")
    (runtime / "lib" / "python3.12" / "os.py").write_text("# not really\n")
    return app


def sign(app: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SIGN), *args, str(app)], capture_output=True, text=True)


def make_bundle(root: Path, *, runtime: bool = True) -> Path:
    """The tiny Framescribe.app signed by app/sign_app.sh (with --hardened: the hardened runtime and the test
    entitlements, as build_app.sh --hardened signs)."""
    app = make_unsigned_bundle(root)
    r = sign(app, *(["--hardened"] if runtime else []))
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
    assert sorted(p.name for p in out.iterdir()) == ["Framescribe-1.2.3.zip", "Framescribe-1.2.3.zip.sha256"]
    assert subprocess.run(["shasum", "-a", "256", "-c", "Framescribe-1.2.3.zip.sha256"], cwd=out,
                          capture_output=True).returncode == 0
    unpacked = tmp_path / "unpacked"
    subprocess.run(["ditto", "-x", "-k", str(out / "Framescribe-1.2.3.zip"), str(unpacked)], check=True)
    assert [p.name for p in unpacked.iterdir()] == ["Framescribe.app"]
    assert subprocess.run(["codesign", "--verify", "--deep", "--strict", str(unpacked / "Framescribe.app")]).returncode == 0
    assert subprocess.run(["bash", str(CHECK), "--unpacked", str(unpacked)], capture_output=True).returncode == 0


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
    shutil.copy(CHECK, repo / "app" / "bundle_check.sh")
    shutil.copy(ROOT / "app" / "Package.swift", repo / "app" / "Package.swift")  # app/.build is next to it
    shutil.copy(ROOT / "pyproject.toml", repo / "pyproject.toml")
    out = tmp_path / "out"
    for made in ("work/runtime", "Framescribe.app/Contents"):
        (out / made).mkdir(parents=True)
    (out / "work" / ".framescribe-build").write_text("")  # an earlier build made this work folder
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
    assert [p.name for p in (out / "work").iterdir()] == [".framescribe-build"]  # but for the build's marker
    assert not (repo / "app" / ".build").exists()


@macos_only
def test_a_file_without_entitlements_has_none_even_when_plutil_complains_on_standard_output(tmp_path):
    """macOS 15's plutil writes "<stdin>: Property List error" to standard output for empty input. A signed file with
    no entitlements must still read as having none, and a plist that cannot be parsed must not read as none."""
    exe = tmp_path / "tool"
    shutil.copy("/usr/bin/true", exe)
    subprocess.run(["codesign", "--force", "-s", "-", str(exe)], check=True, capture_output=True)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "plutil").write_text(
        '#!/bin/bash\nin="$(cat)"\n'
        'if [[ -z "$in" ]]; then echo "<stdin>: Property List error: empty"; exit 1; fi\n'
        '[[ -n "${BREAK_PLUTIL:-}" ]] && { echo "<stdin>: Property List error: broken"; exit 1; }\n'
        'printf "%s" "$in" | /usr/bin/plutil "$@"\n')
    (stubs / "plutil").chmod(0o755)
    script = f'source "{CHECK}"; entitlement_names "$1"'
    env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}"}
    r = subprocess.run(["bash", "-c", script, "x", str(exe)], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and r.stdout == "", r.stdout
    ent = tmp_path / "e.plist"
    ent.write_text('<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>'
                   "<key>com.apple.security.cs.allow-jit</key><true/></dict></plist>")
    subprocess.run(["codesign", "--force", "-s", "-", "--entitlements", str(ent), str(exe)], check=True,
                   capture_output=True)
    r = subprocess.run(["bash", "-c", script, "x", str(exe)], capture_output=True, text=True, env=env)
    assert r.stdout.split() == ["com.apple.security.cs.allow-jit"], r.stdout
    r = subprocess.run(["bash", "-c", script, "x", str(exe)], capture_output=True, text=True,
                       env={**env, "BREAK_PLUTIL": "1"})
    assert r.stdout.split() == ["unreadable-entitlements"], r.stdout


@macos_only
def test_a_fresh_checkout_with_no_output_folder_builds(tmp_path):
    """The release runner starts from a checkout with no app/build. The script must make it and reach the Swift
    build (a stub that fails here), not stop while looking into a folder that is not there yet."""
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    shutil.copy(BUILD, repo / "app" / "build_app.sh")
    shutil.copy(CHECK, repo / "app" / "bundle_check.sh")
    shutil.copy(ROOT / "app" / "Package.swift", repo / "app" / "Package.swift")
    shutil.copy(ROOT / "pyproject.toml", repo / "pyproject.toml")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "swift").write_text("#!/bin/bash\nexit 1\n")
    (stubs / "swift").chmod(0o755)
    r = subprocess.run(["bash", str(repo / "app" / "build_app.sh")], capture_output=True, text=True,
                       env={**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}"})
    assert "No such file or directory" not in r.stderr, r.stderr
    assert "==> framescribe " in r.stdout  # got past the output-folder checks
    assert (repo / "app" / "build" / "work" / ".framescribe-build").exists()


@macos_only
def test_clean_leaves_a_work_folder_the_build_did_not_make(tmp_path):
    """--out naming a folder whose work/ is someone else's: neither --clean nor the build touches it."""
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    shutil.copy(BUILD, repo / "app" / "build_app.sh")
    shutil.copy(CHECK, repo / "app" / "bundle_check.sh")
    shutil.copy(ROOT / "pyproject.toml", repo / "pyproject.toml")
    out = tmp_path / "out"
    (out / "work").mkdir(parents=True)
    (out / "work" / "thesis.tex").write_text("not the build's")
    for clean in (["--clean"], []):
        r = subprocess.run(["bash", str(repo / "app" / "build_app.sh"), "--out", str(out), *clean], capture_output=True,
                           text=True)
        assert r.returncode == 1 and "was not made by build_app.sh" in r.stderr, r.stderr
        assert (out / "work" / "thesis.tex").read_text() == "not the build's"
        assert not (out / "work" / ".framescribe-build").exists()


# ---- executable bits -----------------------------------------------------------------------------------------------

DIRECT_CALL = re.compile(r'(?:^|[\s;&|(`"])(?:"?\$(?:GITHUB_WORKSPACE|HERE|APP_DIR|REPO)"?/|\./)?((?:app|scripts)/[\w.-]+\.sh|install\.sh|[\w-]+\.sh)(?=["\s;|&)]|$)')


def git_modes() -> dict[str, str]:
    r = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-s", "--", "*.sh"], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout:
        pytest.skip("not a git checkout")
    return {line.split("\t", 1)[1]: line.split()[0] for line in r.stdout.splitlines()}


def test_every_shell_script_is_executable_in_git_and_on_disk():
    """The workflow and the scripts run app/*.sh, scripts/*.sh and install.sh directly (not through `bash`): a file
    without its executable bit in git fails there with exit 126 (Permission denied), as app/bundle_check.sh did."""
    modes = git_modes()
    expected = {"install.sh", *(p.relative_to(ROOT).as_posix() for d in ("app", "scripts") for p in (ROOT / d).glob("*.sh"))}
    assert expected <= set(modes), expected - set(modes)
    for path in sorted(expected):
        assert modes[path] == "100755", f"{path} is {modes[path]} in git (git update-index --chmod=+x {path})"
        assert os.access(ROOT / path, os.X_OK), path


def test_every_script_the_workflow_or_a_script_runs_directly_is_100755():
    modes = git_modes()
    called: set[str] = set()
    sources = [s.get("run", "") for _, s in all_steps(yaml.safe_load(WORKFLOW.read_text()))]
    sources += [p.read_text() for p in (*(ROOT / "app").glob("*.sh"), *(ROOT / "scripts").glob("*.sh"))]
    for text in sources:
        for line in text.splitlines():
            if line.lstrip().startswith(("#", "echo", "source ", ". ")) or "::error::" in line or "curl" in line:
                continue
            for m in DIRECT_CALL.finditer(line):
                name = m.group(1)
                path = name if "/" in name or name == "install.sh" else f"app/{name}"
                if path in modes:
                    called.add(path)
    # what the measured failure was about, and what build_app.sh runs
    assert {"app/bundle_check.sh", "app/sign_app.sh", "app/preflight_app.sh", "app/package_app.sh",
            "app/build_app.sh"} <= called, called
    assert {p: modes[p] for p in called if modes[p] != "100755"} == {}


@macos_only
@pytest.mark.parametrize("clean", [["--clean"], []], ids=["--clean", "build"])
def test_an_app_in_an_output_folder_the_build_did_not_make_is_never_removed(tmp_path, clean):
    """`--clean --out /Applications` used to run rm -rf on /Applications/Framescribe.app. An output folder without
    the build's marker in work/ keeps its Framescribe.app and zips, with --clean or without, and so does app/.build
    when it is not next to Package.swift."""
    repo = tmp_path / "repo"
    (repo / "app" / ".build").mkdir(parents=True)
    (repo / "app" / ".build" / "keep").write_text("not without Package.swift")
    shutil.copy(BUILD, repo / "app" / "build_app.sh")
    shutil.copy(CHECK, repo / "app" / "bundle_check.sh")
    shutil.copy(ROOT / "pyproject.toml", repo / "pyproject.toml")
    applications = tmp_path / "Applications"
    installed = applications / "Framescribe.app" / "Contents"
    installed.mkdir(parents=True)
    (installed / "Info.plist").write_text("installed")
    (applications / "Other.app").mkdir()
    r = subprocess.run(["bash", str(repo / "app" / "build_app.sh"), "--out", str(applications), *clean],
                       capture_output=True, text=True)
    assert r.returncode == 1 and "was not made by build_app.sh here" in r.stderr, r.stderr
    assert "refusing to remove or replace it" in r.stderr and "==> clean:" not in r.stdout
    assert (installed / "Info.plist").read_text() == "installed"
    assert sorted(p.name for p in applications.iterdir()) == ["Framescribe.app", "Other.app"]  # no work/ either
    # a zip of that name alone is someone else's too
    shutil.rmtree(applications / "Framescribe.app")
    (applications / "Framescribe-9.9.9.zip").write_text("theirs")
    r = subprocess.run(["bash", str(repo / "app" / "build_app.sh"), "--out", str(applications), *clean],
                       capture_output=True, text=True)
    assert r.returncode == 1 and "Framescribe-9.9.9.zip was not made by build_app.sh here" in r.stderr, r.stderr
    assert (applications / "Framescribe-9.9.9.zip").read_text() == "theirs"
    # nothing of anyone's in the output folder, but app/.build has no Package.swift next to it: kept
    (applications / "Framescribe-9.9.9.zip").unlink()
    if clean:
        r = subprocess.run(["bash", str(repo / "app" / "build_app.sh"), "--out", str(applications), "--clean"],
                           capture_output=True, text=True)
        assert r.returncode == 1 and "is not the Swift build folder of this checkout" in r.stderr, r.stderr
        assert (repo / "app" / ".build" / "keep").exists()


# ---- workflow hygiene ----------------------------------------------------------------------------------------------


def all_steps(wf: dict) -> list[tuple[str, dict]]:
    return [(job, s) for job, j in wf["jobs"].items() for s in j["steps"]]


def test_no_run_block_holds_an_expression(wf):
    """Values reach a script through env: only; ${{ }} inside a run: block is pasted into the shell as code."""
    for job, s in all_steps(wf):
        assert "${{" not in s.get("run", ""), (job, s.get("name"))


def test_no_whole_secrets_context_and_no_continue_on_error(wf):
    text = WORKFLOW.read_text()
    assert not re.search(r"toJSON\s*\(\s*secrets", text, re.IGNORECASE)
    assert not re.search(r"secrets\s*\[", text)  # no dynamic secret lookup
    assert "continue-on-error" not in text
    for job in wf["jobs"].values():
        assert "continue-on-error" not in job


def test_no_job_or_workflow_level_env_carries_a_secret(wf):
    assert "secrets." not in as_text(wf.get("env", {}))
    for name, job in wf["jobs"].items():
        assert "secrets." not in as_text(job.get("env", {})), name


def test_no_step_dumps_the_environment(wf):
    dump = re.compile(r"(^|[;&|(]\s*|\bthen\s+|\bdo\s+)(printenv|env|set|export -p|declare -p|compgen -v)\s*($|[;|>&)])",
                      re.MULTILINE)
    for job, s in all_steps(wf):
        run = s.get("run", "")
        assert "printenv" not in run, (job, s.get("name"))
        assert not dump.search(run), (job, s.get("name"))


def test_the_sign_job_never_traces_its_shell(wf):
    for s in steps(wf, "sign"):
        run = s.get("run", "")
        assert not re.search(r"set\s+-[a-wyz]*x|set -o xtrace|bash\s+-[a-z]*x|BASH_XTRACEFD|shopt -s -o xtrace", run)
        assert not re.search(r"set\s+-[a-wyz]*v", run), s.get("name")  # verbose echoes every line, secrets included
        assert "ACTIONS_STEP_DEBUG" not in as_text(s)
    for script in (SIGN, PREFLIGHT, PACKAGE, CHECK):
        assert not re.search(r"^\s*set\s+-[a-z]*x", script.read_text(), re.MULTILINE), script.name


def command_words(script: str) -> list[str]:
    """The first word of every simple command in a step's shell text (a rough split on newlines, pipes, ;, && and ||,
    $( and `then`/`do`/`if`)."""
    words = []
    for line in script.replace("\\\n", " ").splitlines():  # continuation lines are one command
        line = re.sub(r"(^|\s)#.*$", "", line)
        # a quoted string is one word: keep its first characters (to see a quoted command path), drop separators in it
        line = re.sub(r'"[^"]*"|\'[^\']*\'', lambda m: re.sub(r"[|;&()!<>\s]", "_", m.group(0)), line)
        for part in re.split(r"\|\||&&|[|;]|\$\(|<\(|\bthen\b|\bdo\b|\bif\b|\belse\b|\bwhile\b|\bfor\b|!", line):
            part = part.strip().lstrip("({ ").strip()
            while re.match(r"^[A-Za-z_][A-Za-z0-9_]*=\S*\s", part):  # VAR=value command
                part = re.sub(r"^[A-Za-z_][A-Za-z0-9_]*=\S*\s+", "", part)
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=\S*", part):  # an assignment alone runs nothing
                continue
            if part:
                words.append(part.split()[0])
    return words


def test_the_sign_job_runs_nothing_from_inside_the_bundle(wf):
    for s in steps(wf, "sign"):
        for word in command_words(s.get("run", "")):
            assert "$APP" not in word and "RUNNER_TEMP/app" not in word, (s.get("name"), word)
            assert not word.startswith(('"$RUNNER_TEMP', "$RUNNER_TEMP")), (s.get("name"), word)
    for script in (SIGN, PACKAGE, CHECK):
        for word in command_words(script.read_text()):
            assert "$APP" not in word and "$app" not in word and "$py" not in word, (script.name, word)


def test_the_bundle_is_checked_before_any_secret_and_curl_runs_without_one(wf):
    names = [s.get("name") for s in steps(wf, "sign")]
    first_secret = min(i for i, s in enumerate(steps(wf, "sign")) if "secrets." in as_text(s))
    assert names.index("Check the bundle structure") < first_secret
    check = step(wf, "sign", "Check the bundle structure")["run"]
    assert 'app/bundle_check.sh --unpacked "$RUNNER_TEMP/app"' in check
    assert check.index("bundle_check.sh") < check.index("plutil")  # Info.plist is read only once it passed
    for s in steps(wf, "sign"):
        if "curl " in s.get("run", ""):
            assert "secrets." not in as_text(s.get("env", {})), s.get("name")
    assert "curl " not in step(wf, "sign", "Import Developer ID certificate")["run"]


def test_the_p12_password_never_reaches_a_command_line(wf):
    run = step(wf, "sign", "Import Developer ID certificate")["run"]
    lines = [ln for ln in run.splitlines() if "DEVELOPER_ID_P12_PASSWORD" in ln]
    for ln in lines:
        stripped = ln.strip()
        # only the emptiness check, the error text and openssl's -passin env:NAME (the name, not the value)
        assert ("-passin env:DEVELOPER_ID_P12_PASSWORD" in ln and "$DEVELOPER_ID_P12_PASSWORD" not in ln) \
            or stripped.startswith("if [[ -z") or stripped.startswith('echo "::error::'), ln
    assert "-passout env:IMPORT_PASSWORD" in run
    assert 'security import "$IMPORT_P12" -k "$KEYCHAIN" -P "$IMPORT_PASSWORD"' in run
    assert 'IMPORT_PASSWORD="$(openssl rand -hex 24)"' in run
    assert 'rm -f "$IMPORT_P12"' in run and 'rm -f "$P12"' in run
    assert "rewrap -legacy" in run  # OpenSSL 3 reading an older Keychain Access export, after the plain attempt
    assert run.index("if ! rewrap 2>/dev/null") < run.index("rewrap -legacy")


def test_the_notary_identifiers_are_commented_as_identifiers(wf):
    run = step(wf, "sign", "Notarize")["run"]
    assert "identifiers, not secrets" in run


def test_the_dry_run_zip_expires_after_one_day(wf):
    for job, s in all_steps(wf):
        if s.get("uses", "").startswith("actions/upload-artifact@"):
            assert s["with"]["retention-days"] == 1, (job, s.get("name"))


def test_the_verify_job_runs_the_signed_app_with_no_secret(wf):
    verify = wf["jobs"]["verify"]
    assert "environment" not in verify and secret_refs(verify) == set()
    assert verify["permissions"] == {"contents": "read"}
    assert "secrets" not in as_text(verify)
    text = "\n".join(s.get("run", "") for s in verify["steps"])
    for needed in ("shasum -a 256 -c", "com.apple.quarantine", "spctl --assess --type execute -vv",
                   "source=Notarized Developer ID", "stapler validate", "codesign --verify --deep --strict",
                   "devid_team", "entitlement_names app/Support/runtime.entitlements", "-m framescribe --version",
                   "-m framescribe doctor --json", '{"ffmpeg", "ffprobe", "speech model"}',
                   "import librosa, numba, llvmlite", "import mlx.core", "@numba.njit", 'bundle_check.sh" --unpacked'):
        assert needed in text, needed
    uses = [s["uses"].split("@")[0] for s in verify["steps"] if "uses" in s]
    assert uses == ["actions/checkout", "actions/download-artifact"]


def test_every_new_step_of_the_verify_job_is_valid_python_where_it_is_python(wf):
    run = step(wf, "verify", "Run the signed runtime")["run"]
    for block in re.findall(r"run_py -c '\n(.*?)'", run, re.DOTALL):
        compile(block, "<verify>", "exec")


# ---- app/bundle_check.sh, and what sign_app.sh, package_app.sh and preflight_app.sh do with it -------------------


def bundle_check(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(CHECK), *args], capture_output=True, text=True)


def sentinel(tmp_path: Path) -> tuple[Path, str]:
    victim = tmp_path / "outside" / "victim"
    victim.parent.mkdir()
    shutil.copy("/bin/echo", victim)
    return victim, victim.read_bytes().hex()


@macos_only
def test_a_mach_o_file_named_like_python_bytecode_is_found_and_signed(tmp_path):
    app = make_unsigned_bundle(tmp_path)
    stray = app / "Contents" / "Resources" / "runtime" / "lib" / "python3.12" / "stray.pyc"
    shutil.copy("/bin/echo", stray)
    subprocess.run(["codesign", "--remove-signature", str(stray)], check=True)
    r = sign(app, "--hardened")
    assert r.returncode == 0, r.stderr
    assert "signed 3 Mach-O files, every one the bundle holds" in r.stdout  # the app, Python and stray.pyc
    info = subprocess.run(["codesign", "-dv", str(stray)], capture_output=True, text=True).stderr
    assert "flags=0x10002(adhoc,runtime)" in info


@macos_only
def test_preflight_finds_an_unsigned_mach_o_file_whatever_its_name(tmp_path):
    app = make_bundle(tmp_path)
    for name in ("stray.pyc", "stray.pyi", "stray.py", "stray.txt", "stray.json"):
        f = app / "Contents" / "Resources" / "runtime" / "lib" / "python3.12" / name
        shutil.copy("/bin/echo", f)
        subprocess.run(["codesign", "--remove-signature", str(f)], check=True)
    r = preflight(app, "--allow-adhoc")
    assert r.returncode == 1
    # the five strays, and the app's executable, whose bundle seal no longer matches
    assert "FAIL  signature: 6 of 7 Mach-O files are unsigned or do not verify" in r.stdout
    for name in ("stray.pyc", "stray.pyi", "stray.py", "stray.json"):  # (the sixth is behind "... and 1 more")
        assert f"python3.12/{name}\n" in r.stdout
    assert "PASS  Mach-O files: 7, found by content" in r.stdout


@macos_only
def test_a_path_with_a_newline_is_refused_and_nothing_outside_is_touched(tmp_path):
    victim, before = sentinel(tmp_path)
    app = make_unsigned_bundle(tmp_path / "b")
    # the measured attack: a directory "x\n" holding a path that, split at the newline, names the file outside
    evil = app / "Contents" / "Resources" / "runtime" / "lib" / ("x\n")
    target = evil / str(victim.parent).lstrip("/")
    target.mkdir(parents=True)
    shutil.copy("/bin/echo", target / "victim")
    for result in (sign(app, "--hardened"), bundle_check(str(app)),
                   subprocess.run(["bash", str(PACKAGE), "--out", str(tmp_path / "dist"), str(app)],
                                  capture_output=True, text=True)):
        assert result.returncode == 1 and "control character" in result.stderr, result.stderr
    r = preflight(app, "--allow-adhoc")
    assert r.returncode == 1 and "FAIL  bundle structure:" in r.stdout and "control character" in r.stdout
    assert victim.read_bytes().hex() == before
    assert not (tmp_path / "dist").exists() or not any((tmp_path / "dist").iterdir())


@macos_only
@pytest.mark.parametrize("where", ["Contents/Resources/runtime", "Contents/Resources/runtime/bin", "Contents",
                                   "Contents/Resources/runtime/bin/python3.12", "Contents/Resources/runtime/lib/python3.12"])
def test_a_symbolic_link_at_a_fixed_place_is_refused_and_nothing_outside_is_signed(tmp_path, where):
    """The measured attack: runtime/ a link to a folder outside, whose bin/python3.12 sign_app.sh then signed."""
    app = make_unsigned_bundle(tmp_path / "b")
    outside = tmp_path / "outside" / Path(where).name
    outside.parent.mkdir()
    shutil.move(str(app / where), str(outside))
    (app / where).symlink_to(outside)
    before = {f: f.read_bytes() for f in (tmp_path / "outside").rglob("*") if f.is_file()}
    assert before
    r = sign(app, "--hardened")
    assert r.returncode == 1 and ("is a symbolic link" in r.stderr or "is not a regular file" in r.stderr), r.stderr
    assert {f: f.read_bytes() for f in (tmp_path / "outside").rglob("*") if f.is_file()} == before
    assert bundle_check(str(app)).returncode == 1
    assert preflight(app, "--allow-adhoc").returncode == 1


@macos_only
def test_a_symbolic_link_out_of_the_bundle_is_refused_by_sign_and_package(tmp_path):
    victim, before = sentinel(tmp_path)
    app = make_unsigned_bundle(tmp_path / "b")
    (app / "Contents" / "Resources" / "outside").symlink_to(victim)
    r = sign(app, "--hardened")
    assert r.returncode == 1 and "points outside the bundle" in r.stderr
    r = subprocess.run(["bash", str(PACKAGE), "--out", str(tmp_path / "dist"), str(app)], capture_output=True, text=True)
    assert r.returncode == 1 and "points outside the bundle" in r.stderr
    assert victim.read_bytes().hex() == before


@macos_only
@pytest.mark.parametrize(("key", "value", "message"), [
    ("CFBundleName", "../../escaped", "CFBundleName is not of the form"),
    ("CFBundleName", "a/b", "CFBundleName is not of the form"),
    ("CFBundleShortVersionString", "1.0/../../x", "CFBundleShortVersionString is not of the form"),
    ("CFBundleExecutable", "../../../bin/sh", "CFBundleExecutable is not of the form"),
    ("CFBundleIdentifier", "org.example.other", "expected io.github.jwiderberg.framescribe"),
])
def test_info_plist_values_must_be_plain_names_and_the_identifier_ours(tmp_path, key, value, message):
    app = make_bundle(tmp_path / "b")
    write_plist(app, **{key: value})
    out = tmp_path / "x" / "y" / "dist"
    r = subprocess.run(["bash", str(PACKAGE), "--out", str(out), str(app)], capture_output=True, text=True)
    assert r.returncode == 1 and message in r.stderr, r.stderr
    assert not any(p.suffix == ".zip" for p in tmp_path.rglob("*"))  # nothing written, in --out or anywhere above it
    assert sign(app, "--hardened").returncode == 1
    assert bundle_check(str(app)).returncode == 1


@macos_only
def test_a_hard_link_is_refused(tmp_path):
    victim, before = sentinel(tmp_path)
    app = make_unsigned_bundle(tmp_path / "b")
    os.link(victim, app / "Contents" / "Resources" / "linked")
    r = sign(app, "--hardened")
    assert r.returncode == 1 and "more than one hard link" in r.stderr
    assert victim.read_bytes().hex() == before


@macos_only
def test_the_unpacked_artifact_must_be_exactly_one_framescribe_app(tmp_path):
    good = tmp_path / "good"
    make_unsigned_bundle(good)
    assert bundle_check("--unpacked", str(good)).returncode == 0
    (good / "extra.txt").write_text("x")
    r = bundle_check("--unpacked", str(good))
    assert r.returncode == 1 and "exactly one entry" in r.stderr
    other = tmp_path / "other"
    app = make_unsigned_bundle(other)
    app.rename(other / "Other.app")
    assert bundle_check("--unpacked", str(other)).returncode == 1
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "Framescribe.app").symlink_to(tmp_path / "good" / "Framescribe.app")
    assert bundle_check("--unpacked", str(linked)).returncode == 1


def devid_team(info: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", f'source "{CHECK}"; devid_team'], input=info, capture_output=True, text=True)


GOOD_CHAIN = """Executable=/x
Identifier=io.github.jwiderberg.framescribe
Authority=Developer ID Application: Example Person (ABCDE12345)
Authority=Developer ID Certification Authority
Authority=Apple Root CA
Timestamp=1 Jan 2026 at 10:00:00
TeamIdentifier=ABCDE12345
"""


@pytest.mark.parametrize(("info", "team"), [
    (GOOD_CHAIN, "ABCDE12345"),
    (GOOD_CHAIN.replace("TeamIdentifier=ABCDE12345", "TeamIdentifier=ZZZZZ99999"), None),  # leaf and team differ
    (GOOD_CHAIN.replace("Authority=Apple Root CA\n", ""), None),  # chain cut short
    (GOOD_CHAIN.replace("Developer ID Certification Authority", "Apple Worldwide Developer Relations"), None),
    (GOOD_CHAIN.replace("Developer ID Application:", "Apple Development:"), None),  # a development certificate
    (GOOD_CHAIN.replace("Authority=Apple Root CA", "Authority=Apple Root CA\nAuthority=Extra"), None),
    ("Signature=adhoc\nTeamIdentifier=not set\n", None),
])
def test_the_team_id_comes_from_the_developer_id_authority_chain(info, team):
    r = devid_team(info)
    assert (r.stdout.strip() or None) == team and (r.returncode == 0) is (team is not None)


@macos_only
def test_the_two_mach_o_enumerations_agree_and_the_structure_passes_on_a_good_bundle(tmp_path):
    app = make_bundle(tmp_path)
    r = bundle_check(str(app))
    assert r.returncode == 0 and "(2 Mach-O files)" in r.stdout, r.stderr


@macos_only
def test_preflight_refuses_entitlements_on_any_mach_o_file_but_the_python(tmp_path):
    """v12's extra-ent-exe case: a helper executable signed with disable-library-validation passed the old preflight."""
    app = make_bundle(tmp_path)
    ent = tmp_path / "e.plist"
    ent.write_text('<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>com.apple.security.cs.'
                   'disable-library-validation</key><true/></dict></plist>')
    tool = app / "Contents" / "Resources" / "tool2"
    shutil.copy("/bin/echo", tool)
    subprocess.run(["codesign", "--force", "--sign", "-", "--options", "runtime", "--entitlements", str(ent), str(tool)],
                   check=True, capture_output=True)
    subprocess.run(["codesign", "--force", "--sign", "-", "--options", "runtime", "--timestamp=none", str(app)],
                   check=True, capture_output=True)
    r = preflight(app, "--allow-adhoc")
    assert r.returncode == 1 and "FAIL  entitlements: 1 Mach-O files other than runtime/bin/python3.12 carry some" in r.stdout


# ---- control characters in names, unreadable entries, and a RESULT line on every failing exit ----------------------


@macos_only
@pytest.mark.parametrize("name", ["c1-\u0085-next.py", "c1-\u009b-csi.py", "del-\x7f.py", "c0-\x01.py", "tab-\t.py"],
                         ids=["C1 NEL (C2 85)", "C1 CSI (C2 9B)", "DEL", "C0 SOH", "tab"])
def test_a_control_character_in_a_file_name_is_refused(tmp_path, name):
    app = make_unsigned_bundle(tmp_path)
    (app / "Contents" / "Resources" / "runtime" / "lib" / "python3.12" / name).write_text("# planted\n")
    r = bundle_check(str(app))
    assert r.returncode == 1 and "control character" in r.stderr, r.stderr
    s = sign(app, "--hardened")
    assert s.returncode == 1 and "control character" in s.stderr, s.stderr
    p = preflight(app, "--allow-adhoc")
    assert p.returncode == 1 and "FAIL  bundle structure:" in p.stdout and "control character" in p.stdout
    assert p.stdout.rstrip().splitlines()[-1] == "RESULT: refused before any other check"


@pytest.fixture
def restore_modes():
    """Folders and files made unreadable by a test get their permissions back, so that tmp_path can be removed."""
    changed: list[Path] = []
    yield changed
    for p in changed:
        p.chmod(0o755)


@macos_only
@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
@pytest.mark.parametrize("what", ["folder", "file"])
def test_an_unreadable_folder_or_file_is_a_stated_refusal(tmp_path, restore_modes, what):
    app = make_bundle(tmp_path)
    site = app / "Contents" / "Resources" / "runtime" / "lib" / "python3.12"
    if what == "folder":
        locked = site / "lockdir"
        locked.mkdir()
        (locked / "hidden.so").write_bytes(Path("/bin/echo").read_bytes())
        message = "a folder cannot be listed or entered (permissions): Contents/Resources/runtime/lib/python3.12/lockdir"
    else:
        locked = site / "os.py"
        message = "a file cannot be read (permissions): Contents/Resources/runtime/lib/python3.12/os.py"
    locked.chmod(0)
    restore_modes.append(locked)
    r = bundle_check(str(app))
    assert r.returncode == 1 and message in r.stderr and "Permission denied" not in r.stderr, r.stderr
    s = sign(app, "--hardened")
    assert s.returncode == 1 and message in s.stderr, s.stderr
    p = preflight(app, "--allow-adhoc")
    assert p.returncode == 1 and f"FAIL  bundle structure: {message}" in p.stdout
    assert p.stdout.rstrip().splitlines()[-1] == "RESULT: refused before any other check"


UNREADABLE = "cannot be read: the folder cannot be listed or entered (permissions)"


def last_line(r: subprocess.CompletedProcess) -> str:
    return (r.stdout.rstrip().splitlines() or [""])[-1]


@macos_only
@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
@pytest.mark.parametrize("mode", [0o000, 0o300, 0o100], ids=["no access", "enter but not list", "enter only"])
def test_an_unreadable_bundle_is_a_stated_refusal_in_every_script(tmp_path, restore_modes, mode):
    app = make_unsigned_bundle(tmp_path / "b")
    app.chmod(mode)
    restore_modes.append(app)
    r = bundle_check(str(app))
    assert r.returncode == 1 and f"bundle_check.sh: refused: {UNREADABLE}: {app}" in r.stderr, r.stderr
    assert "Permission denied" not in r.stderr and last_line(r) == f"RESULT: refused: {UNREADABLE}: {app}"
    s = sign(app, "--hardened")
    assert s.returncode == 1 and f"sign_app.sh: refused, nothing was signed: {UNREADABLE}" in s.stderr, s.stderr
    k = subprocess.run(["bash", str(PACKAGE), "--out", str(tmp_path / "dist"), str(app)], capture_output=True, text=True)
    assert k.returncode == 1 and f"package_app.sh: refused, nothing was written: {UNREADABLE}" in k.stderr, k.stderr
    p = preflight(app, "--allow-adhoc")
    assert p.returncode == 2 and "not an app bundle" not in p.stdout + p.stderr, p.stdout + p.stderr
    assert last_line(p) == f"RESULT: refused: {UNREADABLE}: {app}"


@macos_only
@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
@pytest.mark.parametrize("mode", [0o000, 0o300, 0o100], ids=["no access", "enter but not list", "enter only"])
def test_an_unpacked_folder_that_cannot_be_listed_is_refused_not_counted_empty(tmp_path, restore_modes, mode):
    folder = tmp_path / "unpacked"
    make_unsigned_bundle(folder)
    folder.chmod(mode)
    restore_modes.append(folder)
    r = bundle_check("--unpacked", str(folder))
    assert r.returncode == 1 and UNREADABLE in r.stderr and "found 0" not in r.stderr, r.stderr
    assert "Permission denied" not in r.stderr and last_line(r) == f"RESULT: refused: {UNREADABLE}: {folder}"


@macos_only
def test_every_bundle_check_ends_with_a_result_line_and_every_refusal_states_its_reason(tmp_path):
    good = make_unsigned_bundle(tmp_path / "good")
    r = bundle_check(str(good))
    assert r.returncode == 0 and last_line(r) == "RESULT: passed", r.stdout + r.stderr
    assert last_line(bundle_check("--unpacked", str(tmp_path / "good"))) == "RESULT: passed"
    help_run = bundle_check("--help")
    assert help_run.returncode == 0 and "RESULT:" not in help_run.stdout.splitlines()[-1]
    other = tmp_path / "other" / "Other.app"
    other.mkdir(parents=True)
    crowded = tmp_path / "crowded"
    make_unsigned_bundle(crowded)
    (crowded / "extra.txt").write_text("x")
    named = make_unsigned_bundle(tmp_path / "named")
    (named / "Contents" / "Resources" / "tab\there").write_text("x")
    cases = {
        "no argument": ((), 2, "usage: bundle_check.sh APP | --unpacked DIR"),
        "two arguments": ((str(good), str(good)), 2, "usage: bundle_check.sh APP"),
        "--unpacked without a folder": (("--unpacked",), 2, "usage: bundle_check.sh --unpacked DIR"),
        "missing": ((str(tmp_path / "missing" / "Framescribe.app"),), 1, "does not exist"),
        "a file": ((str(tmp_path / "good" / "Framescribe.app" / "Contents" / "Info.plist"),), 1, "not a directory"),
        "another name": ((str(other),), 1, "the bundle must be called Framescribe.app"),
        "a second entry": (("--unpacked", str(crowded)), 1, "must hold exactly one entry, Framescribe.app (found 2)"),
        "a control character": ((str(named),), 1, "a path holds a newline or another control character"),
    }
    for name, (args, code, reason) in cases.items():
        r = bundle_check(*args)
        assert r.returncode == code, (name, r.returncode, r.stderr)
        assert reason in r.stderr, (name, r.stderr)
        result = last_line(r)
        assert result.startswith("RESULT: refused: ") and reason in result, (name, result)


@macos_only
def test_an_unexpected_error_in_bundle_check_still_ends_with_a_result(tmp_path):
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "find").write_text("#!/bin/bash\nexit 3\n")
    (stubs / "find").chmod(0o755)
    app = make_unsigned_bundle(tmp_path / "b")
    r = subprocess.run(["bash", str(CHECK), str(app)], capture_output=True, text=True,
                       env={**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}"})
    assert r.returncode != 0 and last_line(r).startswith("RESULT: refused: "), r.stdout + r.stderr
    assert len(last_line(r)) > len("RESULT: refused: ")


@macos_only
def test_a_file_that_is_only_a_mach_o_magic_number_fails_the_preflight_with_a_result(tmp_path):
    """Four bytes, a Mach-O magic and nothing else: vtool cannot read it. The preflight used to stop there (set -e)
    with exit 1 and no RESULT line."""
    app = make_bundle(tmp_path)
    (app / "Contents" / "Resources" / "runtime" / "lib" / "python3.12" / "short.so").write_bytes(b"\xcf\xfa\xed\xfe")
    p = preflight(app, "--allow-adhoc")
    assert p.returncode == 1, p.stdout + p.stderr
    assert "FAIL  SDK: 1 Mach-O files have no readable SDK version or one older than macOS 10.9" in p.stdout
    assert "short.so (no SDK version: vtool cannot read one)" in p.stdout
    assert re.search(r"^RESULT: \d+ check\(s\) failed$", p.stdout.rstrip().splitlines()[-1])


@macos_only
def test_an_unexpected_error_in_the_preflight_still_ends_with_its_reason_and_a_result(tmp_path):
    app = make_bundle(tmp_path)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "sort").write_text("#!/bin/bash\necho 'sort: stub failure' >&2\nexit 3\n")
    (stubs / "sort").chmod(0o755)
    p = subprocess.run(["bash", str(PREFLIGHT), "--allow-adhoc", str(app)], capture_output=True, text=True,
                       env={**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}"})
    assert p.returncode != 0
    assert re.search(r"^FAIL  the preflight stopped at an unexpected error \(exit \d+, preflight_app.sh line \d+\)",
                     p.stdout, re.M), p.stdout
    assert p.stdout.rstrip().splitlines()[-1] == "RESULT: refused, the preflight did not finish"


def test_a_usage_error_of_the_preflight_ends_with_a_result():
    p = subprocess.run(["bash", str(PREFLIGHT)], capture_output=True, text=True)
    assert p.returncode == 2 and "usage: preflight_app.sh" in p.stderr
    assert p.stdout.rstrip().splitlines()[-1] == "RESULT: refused: usage: preflight_app.sh [--allow-adhoc] APP"
    p = subprocess.run(["bash", str(PREFLIGHT), "/nonexistent/Framescribe.app"], capture_output=True, text=True)
    assert p.returncode == 2 and p.stdout.rstrip().splitlines()[-1].startswith("RESULT: refused: not an app bundle")


def test_releasing_md_says_to_turn_admin_bypass_off_and_what_decides_trust():
    doc = (ROOT / "docs" / "RELEASING.md").read_text()
    assert "Allow administrators to bypass configured protection rules is turned off" in doc
    assert "`can_admins_bypass: false`" in doc and "can_admins_bypass}" in doc
    assert "sanity check" in doc and "`codesign --verify --strict`" in doc and "Notarized Developer ID" in doc
    assert "computed in the `publish` job" in doc
    for script in (PREFLIGHT, CHECK):  # the comment there says so too
        assert re.search(r"sanity\s+(?:#\s+)?check", script.read_text()), script
