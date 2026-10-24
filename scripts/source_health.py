"""Pure, persisted-state policy for bounded source checks and relevant alternatives.

This module performs no I/O. A caller persists the returned observations and
records selection before starting a worker. Failure evidence never proves that
a domain registration expired: HTTP 404 requires spaced confirmations, HTTP
410 means the resource is gone, and access restrictions retain their own state.
"""

from __future__ import annotations

import hashlib
import math
import re
from copy import deepcopy
from urllib.parse import urlsplit, urlunsplit

PROTOCOL = "source-health-v1"
DEFAULT_POLICY = {
    "base_backoff_seconds": 60,
    "max_backoff_seconds": 6 * 3600,
    "jitter_ratio": 0.15,
    "restricted_recheck_seconds": 86400,
    "stale_confirmations": 3,
    "stale_window_seconds": 86400,
    "confirmation_spacing_seconds": 3600,
    "gone_recheck_seconds": 7 * 86400,
    "unavailable_confirmations": 5,
    "unavailable_window_seconds": 3 * 86400,
    "unavailable_recheck_seconds": 86400,
    "content_invalid_recheck_seconds": 6 * 3600,
    "resource_wait_seconds": 300,
    "evidence_limit": 32,
}
OUTCOMES = {
    "success",
    "http_error",
    "network_error",
    "content_invalid",
    "storage_wait",
    "budget_wait",
}
FAILURE_CODES = {
    "connection_interrupted",
    "connection_refused",
    "dns_no_addresses",
    "dns_resolution_failed",
    "dns_timeout",
    "dns_timeout_or_capacity",
    "http_protocol_error",
    "network_timeout",
    "network_transport_error",
    "public_address_policy_rejected",
    "redirect_scope_rejected",
    "request_deadline_exceeded",
    "request_rejected",
    "tls_certificate_verification_failed",
    "tls_handshake_failed",
    "transient_network_error",
}


def _number(value, name, *, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"{name} must be a finite number")
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"{name} must be finite and at least {minimum}")
    return value


def _policy(policy):
    if policy is not None and (not isinstance(policy, dict) or set(policy) - set(DEFAULT_POLICY)):
        raise ValueError("unknown source-health policy fields")
    result = {**DEFAULT_POLICY, **(policy or {})}
    for key, value in result.items():
        if key == "jitter_ratio":
            if _number(value, key) > 1:
                raise ValueError("jitter_ratio cannot exceed one")
        elif key in {"stale_confirmations", "unavailable_confirmations", "evidence_limit"}:
            if type(value) is not int or not 1 <= value <= 1000:
                raise ValueError(f"{key} must be an integer between 1 and 1000")
        else:
            _number(value, key, minimum=1)
    if result["base_backoff_seconds"] > result["max_backoff_seconds"]:
        raise ValueError("base backoff cannot exceed maximum backoff")
    return result


def _count(state, key):
    value = state.get(key, 0)
    if type(value) is not int or value < 0:
        raise ValueError(f"invalid persisted counter: {key}")
    return value


def _time(state, key):
    value = state.get(key)
    return None if value is None else _number(value, key)


def _observation(outcome, now):
    if not isinstance(outcome, dict) or outcome.get("status") not in OUTCOMES:
        raise ValueError("unknown source observation status")
    result = {"at": now, "status": outcome["status"]}
    code = outcome.get("http_status")
    if code is not None:
        if type(code) is not int or not 100 <= code <= 599:
            raise ValueError("invalid HTTP status")
        result["http_status"] = code
    if outcome["status"] == "success" and code is not None and not (200 <= code < 300 or code == 304):
        raise ValueError("successful observation has an unsuccessful HTTP status")
    if outcome["status"] == "http_error" and (code is None or code < 400):
        raise ValueError("HTTP failure requires an HTTP error status")
    retry_after = outcome.get("retry_after_seconds")
    if retry_after is not None:
        result["retry_after_seconds"] = _number(retry_after, "retry_after_seconds")
    for name, limit in (("error_type", 128), ("source_id", 256), ("content_hash", 128)):
        if outcome.get(name) is not None:
            if not isinstance(outcome[name], str) or len(outcome[name]) > limit:
                raise ValueError(f"invalid observation {name}")
            result[name] = outcome[name]
    failure_code = outcome.get("failure_code")
    if failure_code is not None:
        if not isinstance(failure_code, str) or not (
            re.fullmatch(r"http_[1-5][0-9]{2}", failure_code)
            or failure_code in FAILURE_CODES
        ):
            raise ValueError("invalid observation failure_code")
        result["failure_code"] = failure_code
    if outcome.get("url") is not None:
        url = outcome["url"]
        if not isinstance(url, str) or len(url) > 8192:
            raise ValueError("invalid observation URL")
        parsed = urlsplit(url)
        # Health evidence needs the origin/path, never credentials or query tokens.
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parsed.port is not None:
            host += f":{parsed.port}"
        result["url"] = urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    return result


def _backoff(state, observation, policy):
    failures = min(state["consecutive_failures"] - 1, 30)
    base = min(
        policy["max_backoff_seconds"],
        policy["base_backoff_seconds"] * 2**failures,
    )
    seed = "|".join(
        str(value)
        for value in (
            observation.get("source_id", state.get("source_id", "")),
            observation.get("url", ""),
            observation["status"],
            observation.get("http_status", ""),
            state["consecutive_failures"],
        )
    )
    fraction = int.from_bytes(hashlib.sha256(seed.encode()).digest()[:8], "big") / (2**64 - 1)
    # Positive-only jitter neither exceeds the configured ceiling nor schedules
    # an earlier retry than the nominal backoff or the upstream Retry-After.
    return min(policy["max_backoff_seconds"], base * (1 + policy["jitter_ratio"] * fraction))


def observe(previous, outcome, *, now, interval_seconds, policy=None):
    """Return a new bounded JSON state; no caller-owned value is mutated.

    ``success`` means the caller already validated useful content. A resource
    wait changes scheduling only; it must not make a healthy website faulty.
    Upstream Retry-After is an uncapped lower bound, including for 429/503.
    """
    _number(now, "now")
    _number(interval_seconds, "interval_seconds", minimum=1)
    settings = _policy(policy)
    if previous is not None and not isinstance(previous, dict):
        raise ValueError("previous source state must be an object")
    state = deepcopy(previous or {})
    if state.get("protocol", PROTOCOL) != PROTOCOL:
        raise ValueError("unknown source-health state protocol")
    # A backwards wall-clock adjustment must not erase elapsed failure evidence
    # or move a scheduled check before an observation already persisted.
    now = max(now, _time(state, "last_observed_at") or 0)
    observation = _observation(outcome, now)
    if observation.get("source_id") and state.get("source_id") not in (None, observation["source_id"]):
        raise ValueError("observation belongs to another source")
    state.update(
        protocol=PROTOCOL,
        source_id=observation.get("source_id", state.get("source_id")),
        attempt_count=_count(state, "attempt_count") + 1,
        success_count=_count(state, "success_count"),
        failure_count=_count(state, "failure_count"),
        consecutive_failures=_count(state, "consecutive_failures"),
        last_observed_at=now,
        last_observation=observation,
        scheduling_status=observation["status"],
    )
    evidence = state.get("evidence", [])
    if not isinstance(evidence, list):
        raise ValueError("persisted observation evidence must be a list")
    state["evidence"] = [*evidence, deepcopy(observation)][-settings["evidence_limit"] :]
    status = observation["status"]
    retry_after = observation.get("retry_after_seconds", 0)
    if status != "success" and retry_after:
        state["retry_not_before"] = max(_time(state, "retry_not_before") or 0, now + retry_after)
    if status in {"storage_wait", "budget_wait"}:
        state.setdefault("health", "unknown")
        state.setdefault("replacement_eligible", False)
        delay = max(settings["resource_wait_seconds"], retry_after)
    elif status == "success":
        state.update(
            health="healthy",
            success_count=state["success_count"] + 1,
            consecutive_failures=0,
            first_failure_at=None,
            last_success_at=now,
            last_attempt_at=now,
            not_found_count=0,
            first_not_found_at=None,
            last_not_found_confirmation_at=None,
            replacement_eligible=False,
            replacement_reason=None,
            retry_not_before=None,
        )
        if observation.get("content_hash"):
            state["last_content_hash"] = observation["content_hash"]
        delay = max(interval_seconds, retry_after)
    else:
        previous_health = state.get("health")
        previous_reason = state.get("replacement_reason")
        previous_replacement = state.get("replacement_eligible", False)
        state.update(
            failure_count=state["failure_count"] + 1,
            consecutive_failures=state["consecutive_failures"] + 1,
            last_failure_at=now,
            last_attempt_at=now,
        )
        if _time(state, "first_failure_at") is None:
            state["first_failure_at"] = now
        if _time(state, "first_ever_failure_at") is None:
            state["first_ever_failure_at"] = now
        delay = _backoff(state, observation, settings)
        code = observation.get("http_status")
        state.update(health="degraded", replacement_eligible=False, replacement_reason=None)
        if status != "http_error" or code != 404:
            state.update(not_found_count=0, first_not_found_at=None, last_not_found_confirmation_at=None)
        if status == "http_error" and code in {401, 403}:
            state.update(health="restricted", replacement_eligible=True, replacement_reason=f"http_{code}")
            delay = settings["restricted_recheck_seconds"]
        elif status == "http_error" and code == 410:
            state.update(health="gone", replacement_eligible=True, replacement_reason="http_410")
            delay = settings["gone_recheck_seconds"]
        elif status == "http_error" and code == 404:
            count = _count(state, "not_found_count")
            last = _time(state, "last_not_found_confirmation_at")
            if last is None or now - last >= settings["confirmation_spacing_seconds"]:
                count += 1
                state["last_not_found_confirmation_at"] = now
                if _time(state, "first_not_found_at") is None:
                    state["first_not_found_at"] = now
            state.update(health="not_found", not_found_count=count)
            if (
                count >= settings["stale_confirmations"]
                and now - state["first_not_found_at"] >= settings["stale_window_seconds"]
            ):
                state.update(
                    health="stale", replacement_eligible=True, replacement_reason="confirmed_http_404"
                )
                delay = settings["gone_recheck_seconds"]
        elif status == "http_error" and code == 429:
            state["health"] = "rate_limited"
        elif status == "content_invalid":
            state.update(
                health="content_invalid", replacement_eligible=True, replacement_reason="content_invalid"
            )
            delay = settings["content_invalid_recheck_seconds"]
        elif (
            state["consecutive_failures"] >= settings["unavailable_confirmations"]
            and now - state["first_failure_at"] >= settings["unavailable_window_seconds"]
        ):
            state.update(
                health="unavailable", replacement_eligible=True, replacement_reason="persistent_failure"
            )
            delay = settings["unavailable_recheck_seconds"]
        if previous_replacement and (
            status == "network_error" or (status == "http_error" and (code == 429 or code >= 500))
        ):
            # A DNS error/5xx/rate limit cannot establish that a previously
            # gone or restricted resource recovered. Keep its alternative
            # routing until useful content is actually observed again.
            state.update(
                health=previous_health,
                replacement_eligible=True,
                replacement_reason=previous_reason,
            )
            delay = max(
                delay,
                settings["gone_recheck_seconds"]
                if previous_health in {"gone", "stale"}
                else settings["unavailable_recheck_seconds"],
            )
        delay = max(delay, retry_after)
    state["next_at"] = now + delay
    if status != "success":
        state["next_at"] = max(state["next_at"], _time(state, "retry_not_before") or 0)
    if status in {"storage_wait", "budget_wait"}:
        state["next_at"] = max(state["next_at"], _time(previous or {}, "next_at") or 0)
    if not math.isfinite(state["next_at"]):
        raise ValueError("source retry time is not finite")
    return state


def due(source_state, now):
    """Every health state can recover through a scheduled low-frequency check."""
    _number(now, "now")
    if source_state is None:
        return True
    if not isinstance(source_state, dict):
        raise ValueError("source state must be an object")
    if source_state.get("enabled") is False:
        return False
    return now >= _number(source_state.get("next_at", 0), "next_at")


def _sources(config_sources):
    if not isinstance(config_sources, list):
        raise ValueError("configured sources must be a list")
    result = []
    identities = set()
    for source in config_sources:
        if not isinstance(source, dict) or not isinstance(source.get("id"), str) or not source["id"]:
            raise ValueError("each configured source needs a stable id")
        if source["id"] in identities:
            raise ValueError("duplicate configured source id")
        identities.add(source["id"])
        country = source.get("country", "")
        topics = source.get("topics", source.get("subjects", []))
        if not isinstance(country, str) or not isinstance(topics, list):
            raise ValueError("source country/topics must be a string and list")
        if any(not isinstance(topic, str) or not topic.strip() for topic in topics):
            raise ValueError("source topics must be nonempty strings")
        priority = source.get("priority", 0)
        _number(priority, "source priority")
        result.append(
            {
                **source,
                "country": country.strip().upper(),
                "topics": {topic.strip().casefold() for topic in topics},
            }
        )
    return result


def choose_sources(config_sources, state_by_id, *, now, max_sources):
    """Choose a deterministic, bounded, coverage-aware fair round of due ids.

    The caller records ``last_selected_at`` and optionally ``selection_count``
    before dispatch. Without that optional counter, observations' attempt_count
    supplies the fair round. Within the oldest round, prefer country and topic
    coverage, then configured priority. A failing source's cooldown is local.
    """
    _number(now, "now")
    if type(max_sources) is not int or max_sources < 0:
        raise ValueError("max_sources must be a nonnegative integer")
    if not isinstance(state_by_id, dict):
        raise ValueError("source states must be keyed by source id")
    candidates = [
        row
        for row in _sources(config_sources)
        if row.get("enabled", True) is not False and due(state_by_id.get(row["id"]), now)
    ]
    selected = []
    countries = set()
    topics = set()
    while candidates and len(selected) < max_sources:

        def order(source):
            state = state_by_id.get(source["id"]) or {}
            count = _count(state, "selection_count" if "selection_count" in state else "attempt_count")
            last = _time(state, "last_selected_at")
            if last is None:
                last = _time(state, "last_attempt_at")
            return (
                count,
                source["country"] in countries,
                -len(source["topics"] - topics),
                -source.get("priority", 0),
                -1 if last is None else last,
                source["id"],
            )

        chosen = min(candidates, key=order)
        selected.append(chosen["id"])
        countries.add(chosen["country"])
        topics.update(chosen["topics"])
        candidates.remove(chosen)
    return selected


def replacement_decisions(config_sources, state_by_id, selected_ids, *, now):
    """Explain relevant alternatives; observations remain separate per source.

    Selection is not proof that a replacement is authoritative or legally
    equivalent. Only explicitly configured, enabled sources with the same
    country and an overlapping subject receive a decision record.
    """
    _number(now, "now")
    sources = _sources(config_sources)
    selected = set(selected_ids)
    known = {source["id"] for source in sources}
    if selected - known:
        raise ValueError("unknown selected replacement source")
    result = []
    for failed in sources:
        state = state_by_id.get(failed["id"]) or {}
        if not state.get("replacement_eligible"):
            continue
        alternatives = [
            source
            for source in sources
            if source["id"] in selected
            and source["id"] != failed["id"]
            and source.get("enabled", True) is not False
            and source["country"]
            and source["country"] == failed["country"]
            and source["topics"] & failed["topics"]
            and not (state_by_id.get(source["id"]) or {}).get("replacement_eligible")
        ]
        if not alternatives:
            continue
        target = max(
            alternatives,
            key=lambda source: (
                len(source["topics"] & failed["topics"]),
                source.get("priority", 0),
                source["id"],
            ),
        )
        result.append(
            {
                "at": now,
                "source_id": failed["id"],
                "target_id": target["id"],
                "reason": state.get("replacement_reason") or state.get("health", "persistent_failure"),
                "country": target["country"],
                "shared_topics": sorted(failed["topics"] & target["topics"]),
            }
        )
    return result
