"""Time-driven source rotation checks without network, models, or database writes."""

from copy import deepcopy
import importlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
health = importlib.import_module("source_health")


def observe(previous=None, *, now=0, status="success", http_status=None, policy=None, **values):
    return health.observe(
        previous,
        {"status": status, "http_status": http_status, **values},
        now=now,
        interval_seconds=600,
        policy=policy,
    )


def test_useful_success_recovers_without_erasing_failure_evidence():
    failed = observe(now=10, status="http_error", http_status=410)
    old = deepcopy(failed)
    recovered = observe(failed, now=100, http_status=200, content_hash="a" * 64)
    assert failed == old
    assert recovered["health"] == "healthy"
    assert recovered["failure_count"] == 1
    assert recovered["success_count"] == 1
    assert recovered["consecutive_failures"] == 0
    assert recovered["first_failure_at"] is None
    assert recovered["first_ever_failure_at"] == 10
    assert recovered["last_content_hash"] == "a" * 64
    assert recovered["next_at"] == 700
    assert not recovered["replacement_eligible"]
    assert [row["http_status"] for row in recovered["evidence"]] == [410, 200]
    assert json.loads(json.dumps(recovered)) == recovered


@pytest.mark.parametrize("code", [401, 403])
def test_access_restriction_is_not_expiration_and_receives_low_frequency_recheck(code):
    state = observe(status="http_error", http_status=code)
    assert state["health"] == "restricted"
    assert state["replacement_reason"] == f"http_{code}"
    assert state["replacement_eligible"]
    assert state["next_at"] == 86400
    assert not health.due(state, 86399)
    assert health.due(state, 86400)
    assert "expired" not in json.dumps(state)


def test_404_requires_spaced_confirmations_and_elapsed_window():
    state = observe(status="http_error", http_status=404)
    for _ in range(10):
        state = observe(state, status="http_error", http_status=404)
    assert state["not_found_count"] == 1
    state = observe(state, now=3600, status="http_error", http_status=404)
    state = observe(state, now=7200, status="http_error", http_status=404)
    assert state["not_found_count"] == 3
    assert state["health"] == "not_found"
    assert not state["replacement_eligible"]
    state = observe(state, now=86400, status="http_error", http_status=404)
    assert state["health"] == "stale"
    assert state["replacement_reason"] == "confirmed_http_404"
    assert state["next_at"] == 86400 + 7 * 86400


def test_non_404_breaks_consecutive_resource_not_found_confirmations():
    state = observe(status="http_error", http_status=404)
    state = observe(state, now=86400, status="http_error", http_status=503)
    state = observe(state, now=172800, status="http_error", http_status=404)
    assert state["not_found_count"] == 1
    assert state["first_not_found_at"] == 172800
    assert state["health"] == "not_found"


def test_410_retains_evidence_and_still_allows_future_recovery():
    state = observe(now=50, status="http_error", http_status=410)
    assert state["health"] == "gone"
    assert state["replacement_eligible"]
    assert health.due(state, 50 + 7 * 86400)
    state = observe(state, now=50 + 7 * 86400, status="network_error", error_type="ConnectError")
    assert state["health"] == "gone"
    assert state["replacement_reason"] == "http_410"
    assert state["last_observation"]["status"] == "network_error"
    state = observe(state, now=50 + 14 * 86400, http_status=200)
    assert state["health"] == "healthy"


def test_long_dns_failure_is_unavailable_never_claimed_domain_expired():
    state = None
    for day in range(5):
        state = observe(state, now=day * 86400, status="network_error", error_type="DNSResolutionError")
    assert state["health"] == "unavailable"
    assert state["replacement_reason"] == "persistent_failure"
    assert state["replacement_eligible"]
    assert state["first_failure_at"] == 0
    assert state["failure_count"] == 5
    assert state["next_at"] == 5 * 86400
    assert "expired" not in json.dumps(state)


def test_burst_of_network_failures_does_not_satisfy_elapsed_unavailability_window():
    state = None
    for second in range(20):
        state = observe(state, now=second, status="network_error")
    assert state["health"] == "degraded"
    assert not state["replacement_eligible"]


@pytest.mark.parametrize("code", [429, 503])
def test_retry_after_is_not_capped_to_local_backoff(code):
    state = observe(now=100, status="http_error", http_status=code, retry_after_seconds=10 * 86400)
    assert state["next_at"] == 100 + 10 * 86400
    assert not health.due(state, state["next_at"] - 1)
    assert health.due(state, state["next_at"])
    assert state["last_observation"]["retry_after_seconds"] == 10 * 86400


def test_429_has_distinct_health_and_does_not_retire_a_live_source():
    state = observe(status="http_error", http_status=429, retry_after_seconds=0)
    assert state["health"] == "rate_limited"
    assert not state["replacement_eligible"]
    assert state["next_at"] >= 60


def test_deterministic_jitter_stays_in_bounds_at_large_failure_count():
    prior = {"consecutive_failures": 10000}
    first = observe(prior, status="network_error", source_id="one", url="https://one.test/file")
    second = observe(prior, status="network_error", source_id="one", url="https://one.test/file")
    assert first == second
    assert first["next_at"] == 6 * 3600
    early = observe(status="network_error", source_id="one")
    assert 60 <= early["next_at"] <= 69
    different = observe(status="network_error", source_id="two")
    assert early["next_at"] != different["next_at"]


@pytest.mark.parametrize("status", ["storage_wait", "budget_wait"])
def test_resource_wait_neither_faults_website_nor_resets_existing_failure(status):
    healthy = observe(now=10)
    waiting = observe(healthy, now=20, status=status)
    assert waiting["health"] == "healthy"
    assert waiting["failure_count"] == 0
    assert waiting["success_count"] == 1
    assert waiting["last_success_at"] == 10
    assert waiting["last_attempt_at"] == 10
    assert waiting["next_at"] == 610
    failed = observe(now=10, status="http_error", http_status=403)
    waiting = observe(failed, now=20, status=status, retry_after_seconds=600)
    assert waiting["health"] == "restricted"
    assert waiting["consecutive_failures"] == 1
    assert waiting["replacement_eligible"]
    assert waiting["next_at"] == 86410


def test_retry_after_lower_bound_survives_resource_wait_and_another_failure():
    state = observe(now=0, status="http_error", http_status=429, retry_after_seconds=10 * 86400)
    state = observe(state, now=10, status="storage_wait")
    assert state["next_at"] == 10 * 86400
    state = observe(state, now=20, status="network_error")
    assert state["next_at"] == 10 * 86400
    state = observe(state, now=10 * 86400, http_status=200)
    assert state["retry_not_before"] is None
    assert state["next_at"] == 10 * 86400 + 600


def test_content_invalid_is_recorded_without_fake_success():
    state = observe(now=10, status="content_invalid", http_status=200, error_type="UnexpectedLoginPage")
    assert state["health"] == "content_invalid"
    assert state["success_count"] == 0
    assert state["failure_count"] == 1
    assert state["replacement_eligible"]
    assert state["next_at"] == 10 + 6 * 3600


def test_bounded_evidence_omits_url_credentials_and_query_tokens():
    state = None
    for second in range(40):
        state = observe(
            state,
            now=second,
            source_id="official-a",
            url="https://name:password@official.test:8443/path?access_token=secret#fragment",
        )
    assert state["attempt_count"] == 40
    assert len(state["evidence"]) == 32
    assert state["evidence"][0]["at"] == 8
    assert state["last_observation"]["url"] == "https://official.test:8443/path"
    assert "secret" not in json.dumps(state)
    assert "password" not in json.dumps(state)


def test_backwards_clock_does_not_move_observation_or_recheck_backwards():
    state = observe(now=100)
    state = observe(state, now=10)
    assert state["last_observed_at"] == 100
    assert state["next_at"] == 700


def test_source_identity_cannot_cross_contaminate_state():
    state = observe(source_id="a")
    with pytest.raises(ValueError, match="another source"):
        observe(state, source_id="b")


def test_due_excludes_explicitly_disabled_sources_only():
    assert health.due(None, 0)
    assert health.due({"health": "gone", "next_at": 10}, 10)
    assert not health.due({"enabled": False, "next_at": 0}, 10)


def sources():
    return [
        {"id": "sg-main", "country": "SG", "topics": ["food"], "priority": 100},
        {"id": "sg-extra", "country": "SG", "topics": ["food"], "priority": 90},
        {"id": "sg-tax", "country": "SG", "topics": ["tax"], "priority": 1},
        {"id": "id-main", "country": "ID", "topics": ["food"], "priority": 10},
    ]


def test_selection_prefers_country_and_topic_coverage_with_configured_priority():
    original = sources()
    assert health.choose_sources(original, {}, now=0, max_sources=3) == ["sg-main", "id-main", "sg-tax"]
    assert original == sources()
    assert health.choose_sources(original, {}, now=0, max_sources=0) == []


def test_selected_attempts_rotate_fairly_even_with_overwhelming_priority():
    configured = sources()
    configured[0]["priority"] = 10**9
    states = {}
    selected = []
    for turn in range(12):
        chosen = health.choose_sources(configured, states, now=turn * 1000, max_sources=1)
        assert len(chosen) == 1
        selected.extend(chosen)
        identity = chosen[0]
        states[identity] = observe(states.get(identity), now=turn * 1000, source_id=identity)
        states[identity]["last_selected_at"] = turn * 1000
    assert set(selected[:4]) == {row["id"] for row in configured}
    assert all(selected.count(row["id"]) == 3 for row in configured)


def test_selection_counter_allows_rotation_after_dispatch_crash_without_observation():
    configured = sources()
    states = {"sg-main": {"selection_count": 1, "last_selected_at": 0}}
    chosen = health.choose_sources(configured, states, now=1, max_sources=1)
    assert chosen == ["sg-extra"]


def test_failure_and_disabled_source_do_not_block_other_sources():
    configured = sources()
    configured[1]["enabled"] = False
    states = {"sg-main": observe(status="http_error", http_status=429, retry_after_seconds=100000)}
    assert health.choose_sources(configured, states, now=100, max_sources=10) == ["id-main", "sg-tax"]
    assert health.choose_sources(configured, states, now=100000, max_sources=10) == [
        "id-main",
        "sg-tax",
        "sg-main",
    ]


def test_replacement_records_require_same_country_shared_subject_and_configured_target():
    configured = sources() + [{"id": "sg-food-new", "country": "sg", "subjects": ["food"], "priority": 50}]
    states = {"sg-main": observe(status="http_error", http_status=410)}
    before = deepcopy(states)
    decisions = health.replacement_decisions(
        configured, states, ["id-main", "sg-tax", "sg-food-new"], now=100
    )
    assert decisions == [
        {
            "at": 100,
            "source_id": "sg-main",
            "target_id": "sg-food-new",
            "reason": "http_410",
            "country": "SG",
            "shared_topics": ["food"],
        }
    ]
    assert states == before
    states["sg-food-new"] = observe(status="http_error", http_status=403)
    assert health.replacement_decisions(configured, states, ["sg-food-new"], now=100) == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"now": float("nan")},
        {"status": "anything"},
        {"status": "success", "http_status": 404},
        {"status": "http_error", "http_status": None},
        {"status": "http_error", "http_status": True},
        {"retry_after_seconds": -1},
        {"policy": {"unknown": 1}},
        {"policy": {"base_backoff_seconds": 200, "max_backoff_seconds": 100}},
    ],
)
def test_invalid_observation_or_policy_fails_closed(kwargs):
    with pytest.raises(ValueError):
        observe(**kwargs)


def test_invalid_or_ambiguous_config_fails_closed():
    with pytest.raises(ValueError, match="duplicate"):
        health.choose_sources([sources()[0], sources()[0]], {}, now=0, max_sources=1)
    with pytest.raises(ValueError, match="unknown selected"):
        health.replacement_decisions(sources(), {}, ["unconfigured.example"], now=0)
