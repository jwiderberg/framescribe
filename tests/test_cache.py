import hashlib
import os

import pytest

from framescribe import cache
from framescribe.models import AsrArtifact, TranscriptArtifact

BASE = dict(fingerprint="fp", stage="asr.parakeet", version=1, config={"chunk_seconds": 120.0}, dep_keys={"ingest": "k1"},
            dep_digests={"ingest": "d1"})


def test_key_is_stable_and_order_independent():
    a = cache.stage_key(**BASE, extra="")
    b = cache.stage_key(**{**BASE, "dep_keys": {"ingest": "k1"}, "config": {"chunk_seconds": 120.0}})
    assert a == b and len(a) == 64
    two = cache.stage_key(**{**BASE, "dep_keys": {"a": "1", "b": "2"}})
    assert two == cache.stage_key(**{**BASE, "dep_keys": {"b": "2", "a": "1"}})


@pytest.mark.parametrize("change", [
    {"config": {"chunk_seconds": 60.0}},   # used config field
    {"version": 2},                         # stage VERSION
    {"dep_keys": {"ingest": "k2"}},         # upstream key
    {"dep_digests": {"ingest": "d2"}},      # upstream artifact content
    {"fingerprint": "other"},               # source
    {"stage": "asr.whisperkit"},
    {"extra": "x"},
])
def test_key_changes_with_each_input(change):
    assert cache.stage_key(**BASE) != cache.stage_key(**{**BASE, **change})


def test_fingerprint_changes_with_content_size_and_mtime(tmp_path):
    f = tmp_path / "v.bin"
    f.write_bytes(b"a" * 100)
    fp = cache.source_fingerprint(f)
    assert cache.source_fingerprint(f) == fp
    os.utime(f, ns=(1, 1))
    assert cache.source_fingerprint(f) != fp
    f.write_bytes(b"b" * 100)
    os.utime(f, ns=(1, 1))
    changed = cache.source_fingerprint(f)
    f.write_bytes(b"b" * 101)
    os.utime(f, ns=(1, 1))
    assert len({fp, changed, cache.source_fingerprint(f)}) == 3


def test_fingerprint_hashes_only_first_and_last_4mb(tmp_path):
    f = tmp_path / "big.bin"
    size = 10 * 1024 * 1024
    f.write_bytes(bytes(size))
    os.utime(f, ns=(1, 1))
    fp = cache.source_fingerprint(f)
    with f.open("r+b") as fh:  # change a byte in the middle, same size and mtime
        fh.seek(size // 2)
        fh.write(b"x")
    os.utime(f, ns=(1, 1))
    assert cache.source_fingerprint(f) == fp
    with f.open("r+b") as fh:  # change a byte in the last 4 MB
        fh.seek(size - 10)
        fh.write(b"x")
    os.utime(f, ns=(1, 1))
    assert cache.source_fingerprint(f) != fp


def test_store_hit_miss_and_corruption(tmp_path):
    store = cache.ArtifactStore(tmp_path)
    art = TranscriptArtifact(primary="p", segments=[], sources=["p"])
    assert store.load("t.json", "k", TranscriptArtifact) is None
    store.save("t.json", "k", art)
    assert store.load("t.json", "k", TranscriptArtifact) == art
    assert store.load("t.json", "other-key", TranscriptArtifact) is None
    (tmp_path / "t.json").write_text("{not json")
    assert store.load("t.json", "k", TranscriptArtifact) is None
    assert store.load("t.json", "k", AsrArtifact) is None


def test_side_file_must_keep_its_recorded_size(tmp_path):
    store = cache.ArtifactStore(tmp_path)
    art = TranscriptArtifact(primary="p", segments=[], sources=["p"])
    (tmp_path / "audio.wav").write_bytes(b"x" * 100)
    store.save("t.json", "k", art, ["audio.wav"])
    assert store.load("t.json", "k", TranscriptArtifact) == art
    (tmp_path / "audio.wav").write_bytes(b"x" * 40)  # truncated
    assert store.load("t.json", "k", TranscriptArtifact) is None
    (tmp_path / "audio.wav").write_bytes(b"x" * 100)
    assert store.load("t.json", "k", TranscriptArtifact) == art
    (tmp_path / "audio.wav").unlink()
    assert store.load("t.json", "k", TranscriptArtifact) is None


def test_digest_follows_the_artifact_and_side_file_contents(tmp_path):
    store = cache.ArtifactStore(tmp_path)
    (tmp_path / "audio.wav").write_bytes(b"x" * 100)
    store.save("t.json", "k", TranscriptArtifact(primary="alpha", segments=[], sources=["p"]), ["audio.wav"])
    first = store.digest("t.json")
    assert store.digest("t.json") == first
    store.save("t.json", "k", TranscriptArtifact(primary="beta", segments=[], sources=["p"]), ["audio.wav"])
    second = store.digest("t.json")
    assert second != first  # same key, different result
    (tmp_path / "audio.wav").write_bytes(b"y" * 100)  # same size, different content
    assert store.digest("t.json") != second


def _asr(text, wall):
    return AsrArtifact(backend="b", model="m", language="en", words=[], segments=[], audio_seconds=10.0,
                       wall_seconds=wall, warnings=[text])


def test_digest_ignores_wall_clock_timings_but_not_other_fields(tmp_path):
    store = cache.ArtifactStore(tmp_path)
    store.save("a.json", "k", _asr("x", 1.25))
    first = store.digest("a.json")
    store.save("a.json", "k", _asr("x", 9.75))
    assert store.digest("a.json") == first
    store.save("a.json", "k", _asr("y", 1.25))
    assert store.digest("a.json") != first


def test_digest_of_an_artifact_without_timings_is_the_hash_of_its_bytes(tmp_path):
    store = cache.ArtifactStore(tmp_path)
    store.save("t.json", "k", TranscriptArtifact(primary="p", segments=[], sources=["p"]))
    h = hashlib.sha256(b"t.json\0" + hashlib.sha256((tmp_path / "t.json").read_bytes()).digest())
    assert store.digest("t.json") == h.hexdigest()


def test_invalidate_makes_the_old_artifact_a_miss_until_saved_again(tmp_path):
    store = cache.ArtifactStore(tmp_path)
    art = TranscriptArtifact(primary="p", segments=[], sources=["p"])
    store.save("t.json", "k", art)
    store.invalidate("t.json")
    assert store.load("t.json", "k", TranscriptArtifact) is None
    store.save("t.json", "k", art)
    assert store.load("t.json", "k", TranscriptArtifact) == art


def test_write_if_changed_leaves_an_unchanged_file_alone(tmp_path):
    from framescribe.cache import write_if_changed

    f = tmp_path / "a.md"
    assert write_if_changed(f, "one") and f.read_text() == "one"
    before = f.stat().st_mtime_ns
    assert not write_if_changed(f, "one") and f.stat().st_mtime_ns == before
    assert write_if_changed(f, "two") and f.read_text() == "two"
