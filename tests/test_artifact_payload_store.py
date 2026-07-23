"""Unit tests for the dedicated Artifact payload persistence layer (C4)."""

import json

import pytest

from src.interface.artifact import Artifact
from src.orchestration.artifact_payload_store import (
    ArtifactPayloadCorruption,
    ArtifactPayloadStore,
)
from src.orchestration.store import ArtifactStore


def _store_with_one(payload):
    store = ArtifactStore()
    store.put(Artifact(logical_name="p", payload=payload).with_checksum())
    return store


def test_save_returns_desensitized_index_without_payload(tmp_path):
    store = _store_with_one({"name": "王强", "salary": 42000})
    ps = ArtifactPayloadStore("task-1", base_dir=tmp_path)
    index = ps.save_store_state(store.dump_state())
    # Index carries refs + checksum but NOT the raw payload.
    for versions in index.values():
        for meta in versions.values():
            assert "payload" not in meta
            assert meta["checksum"]


def test_roundtrip_load_index_matches_original(tmp_path):
    store = _store_with_one({"name": "王强"})
    ps = ArtifactPayloadStore("task-1", base_dir=tmp_path)
    index = ps.save_store_state(store.dump_state())

    restored = ArtifactStore()
    restored.load_state(ps.load_index(index))  # must not raise
    assert restored.dump_state().keys() == store.dump_state().keys()


def test_missing_payload_file_fails_closed(tmp_path):
    store = _store_with_one({"name": "王强"})
    ps = ArtifactPayloadStore("task-1", base_dir=tmp_path)
    index = ps.save_store_state(store.dump_state())
    ps.clear()  # payloads gone, index still references them
    with pytest.raises(ArtifactPayloadCorruption):
        ps.load_index(index)


def test_tampered_payload_fails_closed(tmp_path):
    store = _store_with_one({"name": "王强"})
    ps = ArtifactPayloadStore("task-1", base_dir=tmp_path)
    index = ps.save_store_state(store.dump_state())
    # Tamper with the on-disk payload file so its checksum no longer matches.
    files = list((tmp_path / "task-1").glob("*_v*.json"))
    assert files
    data = json.loads(files[0].read_text(encoding="utf-8"))
    data["payload"] = {"name": "TAMPERED"}
    files[0].write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ArtifactPayloadCorruption):
        ps.load_index(index)
