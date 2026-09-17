"""Tests for POST /events/batch: atomic, ordered, idempotent batch writes."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import app.db as db_module
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient backed by an isolated SQLite file."""
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "batch-test.db"))
    db_module.init_db()
    return TestClient(app)


def _ev(event_id, participant_id="alice", points=10, correction_of=None):
    payload = {
        "event_id": event_id,
        "participant_id": participant_id,
        "points": points,
    }
    if correction_of is not None:
        payload["correction_of"] = correction_of
    return payload


def _batch(client, events, expected=201):
    r = client.post("/events/batch", json={"events": events})
    assert r.status_code == expected, r.text
    return r.json()


def _revision(client):
    return client.get("/leaderboard").json()["revision"]


def test_batch_writes_contiguous_revisions_in_order(client):
    body = _batch(client, [_ev("b1", "a", 1), _ev("b2", "b", 2), _ev("b3", "c", 3)])
    assert [e["event_id"] for e in body] == ["b1", "b2", "b3"]
    assert [e["revision"] for e in body] == [1, 2, 3]
    assert _revision(client) == 3


def test_batch_appends_after_existing_revision(client):
    _batch(client, [_ev("b1", "a", 1)])
    body = _batch(client, [_ev("b2", "b", 2), _ev("b3", "c", 3)])
    assert [e["revision"] for e in body] == [2, 3]
    assert _revision(client) == 3


def test_batch_response_follows_request_order_not_revision_sort(client):
    # revisions are allocated by array position; verify via GET ordering too
    body = _batch(client, [_ev("z1", "z", 1), _ev("a1", "a", 2), _ev("m1", "m", 3)])
    assert [e["event_id"] for e in body] == ["z1", "a1", "m1"]
    stored = client.get("/events").json()
    assert [e["event_id"] for e in stored] == ["z1", "a1", "m1"]


def test_empty_batch_422(client):
    r = client.post("/events/batch", json={"events": []})
    assert r.status_code == 422
    assert _revision(client) == 0


def test_batch_over_100_422(client):
    r = client.post(
        "/events/batch", json={"events": [_ev(f"x{i}") for i in range(101)]}
    )
    assert r.status_code == 422
    assert _revision(client) == 0


def test_batch_of_exactly_100_succeeds(client):
    body = _batch(client, [_ev(f"x{i}", points=i) for i in range(100)])
    assert len(body) == 100
    assert [e["revision"] for e in body] == list(range(1, 101))


def test_duplicate_event_id_within_batch_422(client):
    r = client.post(
        "/events/batch",
        json={"events": [_ev("dup", points=1), _ev("other"), _ev("dup", points=2)]},
    )
    assert r.status_code == 422
    assert _revision(client) == 0
    # nothing from the rejected batch is visible
    assert client.get("/events/dup").status_code == 404
    assert client.get("/events/other").status_code == 404


def test_self_referencing_correction_422(client):
    r = client.post("/events/batch", json={"events": [_ev("s1", correction_of="s1")]})
    assert r.status_code == 422
    assert _revision(client) == 0


def test_correction_reference_to_earlier_batch_event(client):
    body = _batch(
        client,
        [
            _ev("c1", "alice", 10),
            _ev("c2", "alice", 12, correction_of="c1"),
        ],
    )
    assert [e["revision"] for e in body] == [1, 2]
    lb = client.get("/leaderboard").json()
    alice = next(e for e in lb["entries"] if e["participant_id"] == "alice")
    assert alice["total_points"] == 12  # only chain tip counts


def test_correction_chain_entirely_within_batch(client):
    body = _batch(
        client,
        [
            _ev("c1", "alice", 10),
            _ev("c2", "alice", 12, correction_of="c1"),
            _ev("c3", "alice", 15, correction_of="c2"),
        ],
    )
    assert [e["revision"] for e in body] == [1, 2, 3]
    lb = client.get("/leaderboard").json()
    alice = next(e for e in lb["entries"] if e["participant_id"] == "alice")
    assert [e["event_id"] for e in alice["effective_events"]] == ["c3"]
    assert alice["total_points"] == 15


def test_correction_reference_to_stored_event(client):
    _batch(client, [_ev("p1", "alice", 10)])
    body = _batch(client, [_ev("p2", "bob", 10, correction_of="p1")])
    assert body[0]["revision"] == 2
    lb = client.get("/leaderboard").json()
    assert {e["participant_id"]: e["total_points"] for e in lb["entries"]} == {
        "bob": 10
    }


def test_correction_reference_to_later_batch_event_422(client):
    r = client.post(
        "/events/batch",
        json={
            "events": [
                _ev("late", "alice", 5, correction_of="future"),
                _ev("future", "alice", 5),
            ]
        },
    )
    assert r.status_code == 422
    assert _revision(client) == 0
    assert client.get("/events/late").status_code == 404
    assert client.get("/events/future").status_code == 404


def test_correction_reference_unknown_event_422(client):
    r = client.post(
        "/events/batch",
        json={"events": [_ev("u1", correction_of="ghost")]},
    )
    assert r.status_code == 422
    assert _revision(client) == 0


def test_invalid_reference_rolls_back_whole_batch(client):
    # first event valid, last references a later entry: nothing may land
    r = client.post(
        "/events/batch",
        json={
            "events": [
                _ev("ok1", "a", 1),
                _ev("ok2", "b", 2),
                _ev("bad", "c", 3, correction_of="ok9"),
                _ev("ok9", "d", 4),
            ]
        },
    )
    assert r.status_code == 422
    assert _revision(client) == 0
    assert client.get("/events").json() == []


def test_unknown_reference_does_not_consume_revision(client):
    bad = client.post(
        "/events/batch", json={"events": [_ev("x1", correction_of="nope")]}
    )
    assert bad.status_code == 422
    body = _batch(client, [_ev("y1")])
    assert body[0]["revision"] == 1  # revision sequence did not advance for the failure


def test_full_replay_returns_200_original_results_in_order(client):
    payload = [_ev("r1", "alice", 10), _ev("r2", "bob", 7, correction_of=None)]
    first = _batch(client, payload)
    replay = client.post("/events/batch", json={"events": payload})
    assert replay.status_code == 200
    assert replay.json() == first
    assert _revision(client) == 2  # no new revisions
    # key order within payload entries must not matter
    reordered = [
        {"points": 10, "participant_id": "alice", "event_id": "r1"},
        {"event_id": "r2", "points": 7, "participant_id": "bob"},
    ]
    r2 = client.post("/events/batch", json={"events": reordered})
    assert r2.status_code == 200
    assert r2.json() == first


def test_replay_response_order_tracks_request_order(client):
    _batch(client, [_ev("o1"), _ev("o2"), _ev("o3")])
    replay = client.post(
        "/events/batch", json={"events": [_ev("o3"), _ev("o1"), _ev("o2")]}
    )
    assert replay.status_code == 200
    assert [e["event_id"] for e in replay.json()] == ["o3", "o1", "o2"]


def test_partial_existence_returns_409(client):
    _batch(client, [_ev("exist", points=5)])
    r = client.post(
        "/events/batch",
        json={"events": [_ev("exist", points=5), _ev("brand-new")]},
    )
    assert r.status_code == 409
    assert _revision(client) == 1  # brand-new was not inserted


def test_same_id_different_payload_returns_409(client):
    _batch(client, [_ev("k1", points=5)])
    r = client.post("/events/batch", json={"events": [_ev("k1", points=99)]})
    assert r.status_code == 409
    # stored payload untouched
    assert client.get("/events/k1").json()["points"] == 5


def test_conflict_alongside_new_events_writes_nothing(client):
    _batch(client, [_ev("k1", points=5)])
    r = client.post(
        "/events/batch",
        json={"events": [_ev("fresh", points=1), _ev("k1", points=99)]},
    )
    assert r.status_code == 409
    assert client.get("/events/fresh").status_code == 404
    assert _revision(client) == 1


def test_batch_then_single_event_share_revision_sequence(client):
    body = _batch(client, [_ev("m1"), _ev("m2")])
    assert [e["revision"] for e in body] == [1, 2]
    single = client.post("/events", json=_ev("m3"))
    assert single.status_code == 201
    assert single.json()["revision"] == 3
    again = _batch(client, [_ev("m4"), _ev("m5")])
    assert [e["revision"] for e in again] == [4, 5]


def test_single_event_endpoint_unchanged(client):
    r = client.post("/events", json=_ev("solo", points=3))
    assert r.status_code == 201
    assert r.json()["revision"] == 1
    replay = client.post("/events", json=_ev("solo", points=3))
    assert replay.status_code == 200
    conflict = client.post("/events", json=_ev("solo", points=4))
    assert conflict.status_code == 409


def test_batch_409_then_retry_with_valid_batch_succeeds(client):
    _batch(client, [_ev("z1")])
    bad = client.post(
        "/events/batch", json={"events": [_ev("z1"), _ev("z2")]}
    )
    assert bad.status_code == 409
    body = _batch(client, [_ev("z2"), _ev("z3")])
    assert [e["revision"] for e in body] == [2, 3]


def test_concurrent_batches_get_disjoint_contiguous_revisions(client):
    n_batches = 20
    size = 5

    def write(b):
        return client.post(
            "/events/batch",
            json={"events": [_ev(f"b{b}-{i}", points=b * size + i) for i in range(size)]},
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(write, range(n_batches)))

    assert all(r.status_code == 201 for r in results)
    all_revisions = sorted(
        e["revision"] for r in results for e in r.json()
    )
    assert all_revisions == list(range(1, n_batches * size + 1))
    # each batch kept its own contiguous block in array order
    for r in results:
        revs = [e["revision"] for e in r.json()]
        assert revs == list(range(revs[0], revs[0] + size))


def test_concurrent_batch_and_single_writes_serialize(client):
    def write_batch(b):
        return client.post(
            "/events/batch",
            json={"events": [_ev(f"batch-{b}-{i}") for i in range(3)]},
        )

    def write_single(i):
        return client.post("/events", json=_ev(f"single-{i}"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        batch_futs = [pool.submit(write_batch, b) for b in range(10)]
        single_futs = [pool.submit(write_single, i) for i in range(15)]
        batch_results = [f.result() for f in batch_futs]
        single_results = [f.result() for f in single_futs]

    assert all(r.status_code == 201 for r in batch_results + single_results)
    revisions = sorted(
        [e["revision"] for r in batch_results for e in r.json()]
        + [r.json()["revision"] for r in single_results]
    )
    assert revisions == list(range(1, 10 * 3 + 15 + 1))


def test_batch_created_at_shared_and_replayed_unchanged(client):
    first = _batch(client, [_ev("t1"), _ev("t2")])
    assert first[0]["created_at"] == first[1]["created_at"]
    replay = client.post("/events/batch", json={"events": [_ev("t2"), _ev("t1")]})
    assert replay.status_code == 200
    by_id = {e["event_id"]: e for e in replay.json()}
    assert by_id["t1"] == first[0]
    assert by_id["t2"] == first[1]
