"""session_id ↔ conversation_id 매핑 검증 (§6)."""

from __future__ import annotations

from agy_bridge.sessions import SessionStore


def test_record_use_creates_and_accumulates(bridge_config):
    store = SessionStore(bridge_config())
    store.record_use(
        "sess-a",
        conversation_id="conv-1",
        mode="review",
        usage={"total_tokens": 100},
    )
    store.record_use(
        "sess-a",
        conversation_id="conv-1",
        mode="verify",
        usage={"total_tokens": 50},
    )

    meta = store.resolve("sess-a")
    assert meta["conversation_id"] == "conv-1"
    assert meta["turns"] == 2
    assert meta["total_tokens"] == 150
    assert meta["last_mode"] == "verify"


def test_resolve_unknown_returns_none(bridge_config):
    store = SessionStore(bridge_config())
    assert store.resolve("nope") is None


def test_persistence_across_instances(bridge_config):
    config = bridge_config()
    SessionStore(config).record_use(
        "sess-b", conversation_id="conv-2", mode="derive", usage={}
    )
    fresh = SessionStore(config)
    assert fresh.resolve("sess-b")["conversation_id"] == "conv-2"


def test_close_removes_mapping(bridge_config):
    config = bridge_config()
    store = SessionStore(config)
    store.record_use("sess-c", conversation_id="conv-3", mode="review", usage={})
    assert store.close("sess-c") is True
    assert store.resolve("sess-c") is None
    assert store.close("sess-c") is False


def test_corrupt_file_is_quarantined(bridge_config):
    config = bridge_config()
    store = SessionStore(config)
    (config.state_dir / "sessions.json").write_text("{{{ not json")
    assert store.list_sessions() == {}
    assert (config.state_dir / "sessions.json.corrupt").exists()
