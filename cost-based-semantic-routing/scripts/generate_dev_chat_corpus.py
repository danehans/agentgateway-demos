#!/usr/bin/env python3
"""Generate the checked-in Go/Rust multi-turn evaluation corpus."""

import argparse
import json
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
LOW_COST_MODEL = "gpt-5.4-nano"
EXPENSIVE_MODEL = "gpt-5.5"
EXPECTED_TURN_COUNTS = {LOW_COST_MODEL: 90, EXPENSIVE_MODEL: 110}


ROUTINE_GO = [
    ("request-validator", "profile-api", "add validation for an update request", "trim whitespace before rejecting an empty display name"),
    ("config-loader", "billing-api", "load an optional timeout from environment configuration", "keep the current default when the value is absent"),
    ("page-token", "catalog-api", "parse and validate an opaque page token", "return a 400 error instead of silently starting at page one"),
    ("retry-budget", "notification-worker", "add a retry budget to one outbound request", "avoid sleeping in the unit test"),
    ("log-redaction", "identity-api", "redact credentials from structured request logs", "preserve the request id and tenant fields"),
    ("cron-parser", "report-worker", "validate a small cron-like schedule setting", "produce a useful error for an empty field"),
    ("shutdown-hook", "import-worker", "finish in-flight work during graceful shutdown", "stop accepting new jobs once the context is canceled"),
    ("metric-labels", "edge-proxy", "guard one metric label against unbounded values", "keep the existing dashboard label for known values"),
    ("cache-key", "pricing-api", "build a stable cache key from tenant and product ids", "escape a colon in either input"),
    ("worker-limit", "thumbnail-worker", "add a bounded worker pool around a list of jobs", "return the first worker error"),
]

ROUTINE_RUST = [
    ("cli-config", "release-tool", "add a required configuration path flag", "show a concise usage error when the path is missing"),
    ("serde-settings", "policy-daemon", "deserialize optional service settings", "fall back to the documented default timeout"),
    ("request-timeout", "webhook-gateway", "apply a timeout to one Tokio request", "surface a typed timeout error"),
    ("error-enum", "archive-worker", "replace string errors with a small error enum", "retain the source error for IO failures"),
    ("iterator-filter", "audit-cli", "filter malformed audit records before rendering", "report the number of skipped records"),
    ("trait-adapter", "storage-agent", "add a small trait adapter around a mockable clock", "keep production construction unchanged"),
    ("channel-close", "queue-worker", "handle a closed Tokio channel without panicking", "finish already received work"),
    ("axum-health", "control-api", "add an Axum health endpoint", "return a compact JSON response"),
    ("test-fixture", "config-linter", "create a temporary YAML fixture for one test", "avoid checking a fixture file into the repository"),
    ("path-normalize", "artifact-agent", "normalize an input path before use", "reject paths that escape the configured root"),
]

ESCALATING_GO = [
    ("worker-ack", "event-worker", "acknowledge completed queue work", "duplicate processing appears after a worker restart"),
    ("reconcile-loop", "cluster-controller", "make one reconciliation step idempotent", "two controller replicas sometimes act on the same object"),
    ("http-retry", "partner-gateway", "retry an outbound POST safely", "the partner occasionally times out after accepting the request"),
    ("cache-refresh", "inventory-api", "refresh a stale cache entry", "readers in another region observe conflicting inventory"),
    ("quota-counter", "usage-api", "increment a tenant usage counter", "regional failover can replay a batch"),
]

ESCALATING_RUST = [
    ("task-cancel", "stream-worker", "cancel a Tokio task cleanly", "cancellation races with a leased message becoming visible again"),
    ("grpc-stream", "replication-agent", "apply backpressure to a tonic stream", "a reconnect can replay messages after an acknowledgement"),
    ("lock-order", "index-worker", "avoid holding a mutex across an await", "a second shard can deadlock under high load"),
    ("outbox-publish", "orders-agent", "publish one database outbox record", "a crash can happen between publishing and marking it sent"),
    ("flag-update", "edge-agent", "apply a feature-flag update", "partitioned agents may receive the same version in different orders"),
]

ADVANCED_GO = [
    ("etcd-election", "scheduler-control-plane", "sporadic leadership changes produce overlapping job execution", "quorum health, leases, and fencing tokens"),
    ("distributed-lock", "migration-service", "a paused lock holder resumes after its lease has expired", "clock uncertainty and stale writers"),
    ("multi-region-order", "checkout-api", "client retries can cross regions while inventory is reserved", "idempotency, reservations, and exactly-once business effects"),
    ("outbox-replay", "ledger-api", "outbox replay duplicates an external side effect after a failover", "transaction boundaries, deduplication, and reconciliation"),
    ("config-rollout", "gateway-control-plane", "a bad configuration needs staged delivery to thousands of gateways", "acknowledgements, rollback, and blast-radius limits"),
    ("metrics-backpressure", "telemetry-ingest", "a downstream metrics store slows during an incident", "queueing, cardinality controls, and tenant fairness"),
    ("cache-invalidation", "catalog-api", "regional cache invalidations arrive late and out of order", "bounded staleness and checkout correctness"),
    ("payment-ledger", "payments-api", "retries can race with captures and reversals", "immutable accounting, reconciliation, and operator repair"),
    ("storage-migration", "document-store", "dual writes diverge during a zero-downtime storage migration", "ordering, verification, repair, and cutover"),
    ("mesh-identity", "mesh-control-plane", "certificate rotation partially completes during a regional outage", "trust roots, fail-open policy, and recovery"),
]

ADVANCED_RUST = [
    ("tokio-cancel", "workflow-worker", "cancellation and timeout race in a long-running Tokio workflow", "ownership, await points, and durable progress"),
    ("raft-state", "metadata-store", "a Raft state machine shows rare replica divergence after disk stalls", "term invariants, log repair, and instrumentation"),
    ("sharded-state", "session-store", "cross-shard updates need ordering during regional failover", "partition routing, idempotency, and convergence"),
    ("unsafe-buffer", "packet-agent", "an unsafe buffer optimization crashes only under sustained load", "minimal reproduction, sanitizers, and ownership boundaries"),
    ("distributed-scheduler", "batch-control-plane", "jobs must not run twice across partitions", "leases, fencing, and at-least-once task delivery"),
    ("durable-workflow", "approval-engine", "business workflows run for days across deploys and manual approvals", "durability, replay, and exactly-once side effects"),
    ("config-replication", "edge-agent", "configuration replicas disagree after recovering from an outage", "snapshots, versions, and safe repair"),
    ("task-leak", "async-ingest", "pending Tokio tasks grow slowly under backpressure", "cancellation, reference retention, and flow control"),
    ("event-replication", "audit-stream", "messages can be delayed, duplicated, and reordered", "receiver invariants and replay strategy"),
    ("regional-rate-limit", "api-gateway", "rate limits must remain fair while the shared store fails over", "local fallback, reconciliation, and tenant quotas"),
]


def identifier(value):
    return value.replace("-", "_")


def go_code(slug):
    name = "Handle" + "".join(part.title() for part in slug.split("-"))
    return f'''```go
type Request struct {{
    Key string `json:"key"`
}}

func (s *Service) {name}(ctx context.Context, req Request) error {{
    if req.Key == "" {{
        return nil
    }}
    return s.store.Save(ctx, req.Key)
}}
```'''


def rust_code(slug):
    function = identifier(slug)
    return f'''```rust
#[derive(Deserialize)]
struct Request {{
    key: String,
}}

async fn {function}(State(service): State<Service>, Json(req): Json<Request>) -> Result<StatusCode, AppError> {{
    service.store.save(req.key).await?;
    Ok(StatusCode::NO_CONTENT)
}}
```'''


def language_code(language, slug):
    return go_code(slug) if language == "go" else rust_code(slug)


def language_name(language):
    return "Go" if language == "go" else "Rust"


def routine_turns(language, scenario, escalating=False):
    slug, service, task, complication = scenario
    name = language_name(language)
    code = language_code(language, slug)
    high_family = f"advanced_{language}_distributed_debugging"
    turns = [
        {
            "family": f"routine_{language}_implementation",
            "expected_model": LOW_COST_MODEL,
            "max_tokens": 240,
            "user": f'''I am working on `{service}`, a {name} service. I need to {task}. This should be a narrow change that preserves the current public API. Here is the relevant handler:\n\n{code}\n\nPlease suggest the smallest implementation change and one focused test.''',
            "assistant_context": f'''Keep the change local to the handler. Validate the input before calling the store, return a typed application error, and add a table-driven test for the success and invalid-input paths. Do not introduce a new abstraction for this single call site.''',
        },
        {
            "family": high_family if escalating else f"routine_{language}_testing",
            "expected_model": EXPENSIVE_MODEL if escalating else LOW_COST_MODEL,
            "max_tokens": 360 if escalating else 240,
            "user": (
                f'''Production evidence changes the scope: {complication}. Before changing the {name} handler, determine whether a narrow patch remains safe. Explain the idempotency or concurrency risk, the durable state transition or invariant that matters, and the focused test that would prove the fix.'''
                if escalating
                else f'''I applied the patch and the reviewer added one requirement: {complication}. The existing tests use an in-memory store. What is the smallest update to the {name} code and test that covers this without changing the handler signature?'''
            ),
            "assistant_context": (
                '''This is no longer only a local handler change. Identify the durable idempotency boundary, make the unsafe transition explicit, and test the retry or recovery window before choosing an implementation.'''
                if escalating
                else '''Add one explicit branch for the new edge case and make the in-memory store record calls. The test should assert both the returned error or result and whether Save was called, keeping the behavior visible at the API boundary.'''
            ),
        },
    ]
    if escalating:
        turns.extend(
            [
                {
                    "family": high_family,
                    "expected_model": EXPENSIVE_MODEL,
                    "max_tokens": 440,
                    "user": f'''The simple patch works in one process, but production evidence changes the problem. In `{service}`, {complication}. We now have retries from two regions and a process can crash after an external action but before its local acknowledgement. The current logs show the same request key on separate instances. Analyze the failure modes and propose a durable design, including the invariant that prevents duplicate business effects.''',
                    "assistant_context": f'''Treat the request key as an idempotency boundary backed by durable state, not an in-memory guard. Separate recording the intent from delivering the external effect, make replay safe, and use a stable operation identifier in every log and metric. A distributed lease alone is insufficient without fencing or a compare-and-set state transition.''',
                },
                {
                    "family": high_family,
                    "expected_model": EXPENSIVE_MODEL,
                    "max_tokens": 460,
                    "user": f'''Turn that design into an implementation and verification plan for the {name} service. Include the state transitions, retry ownership, recovery after a crash at each transition, and integration tests that simulate duplicate delivery and regional failover. Call out what must be atomic and what can be eventually consistent.''',
                },
            ]
        )
    else:
        turns.extend(
            [
                {
                    "family": f"routine_{language}_debugging",
                    "expected_model": LOW_COST_MODEL,
                    "max_tokens": 220,
                    "user": f'''CI now reports that the invalid-input test expected the store to remain untouched, but the current implementation calls it once. Identify the likely ordering mistake and show the small correction. Keep the answer focused on this {name} handler.''',
                    "assistant_context": f'''Move validation ahead of all side effects and return immediately on invalid input. The existing in-memory-store assertion is sufficient once the call is ordered after the validation branch.''',
                },
                {
                    "family": f"routine_{language}_code_review",
                    "expected_model": LOW_COST_MODEL,
                    "max_tokens": 200,
                    "user": f'''Please give a concise final review summary for this {name} change: behavior changed, tests added, and one operational note. Do not redesign the service.''',
                },
            ]
        )
    return turns


def advanced_turns(language, scenario):
    slug, service, incident, focus = scenario
    name = language_name(language)
    code = language_code(language, slug)
    family = f"advanced_{language}_distributed_systems"
    return [
        {
            "family": family,
            "expected_model": EXPENSIVE_MODEL,
            "max_tokens": 440,
            "user": f'''I am debugging `{service}`, a {name} service. {incident}. The local code path looks ordinary:\n\n{code}\n\nThis only appears during failure recovery, not in a single-node test. Build a hypothesis tree that connects the code path to {focus}.''',
            "assistant_context": f'''Start by separating local correctness from distributed correctness. Correlate every operation with a durable identifier, reconstruct ordering from logs and storage, and test the pause, retry, and failover windows independently. The key question is which component is authorized to make the irreversible transition.''',
        },
        {
            "family": family,
            "expected_model": EXPENSIVE_MODEL,
            "max_tokens": 460,
            "user": f'''We collected more evidence. One instance logs success, another retries the same operation after a timeout, and the backing store later shows a partially completed transition. Some nodes were partitioned for thirty seconds. Explain which outcomes should be impossible, which are merely eventual-consistency effects, and what instrumentation would distinguish them.''',
            "assistant_context": f'''A timeout does not establish whether the remote action happened. Use a durable operation state machine, compare-and-set transitions, and explicit ownership epochs. Metrics should expose duplicate attempts, stale owners, transition age, and reconciliation outcomes; logs must retain the operation id and epoch.''',
        },
        {
            "family": family,
            "expected_model": EXPENSIVE_MODEL,
            "max_tokens": 480,
            "user": f'''Propose a production design for `{service}` that addresses {focus}. Compare at least two approaches, identify the safety invariant, state the liveness assumptions, and explain recovery when a process dies between each external and local step. Keep the recommendation grounded in this {name} implementation.''',
            "assistant_context": f'''Prefer the design that makes the business effect idempotent and records intent before delivery. Where exclusive ownership is required, combine leases with fencing tokens or monotonic versions. Reconciliation should be a normal workflow, not an operator-only repair path.''',
        },
        {
            "family": family,
            "expected_model": EXPENSIVE_MODEL,
            "max_tokens": 500,
            "user": f'''Write the rollout and verification plan. Include schema or state changes, compatibility during deployment, fault-injection tests, observability signals, rollback boundaries, and the exact conditions that must block promotion. Also list one counterexample the test plan must catch.''',
        },
    ]


def conversation(identifier_value, language, turns):
    return {"id": identifier_value, "language": language, "turns": turns}


def build_conversations():
    conversations = []
    for language, scenarios in (("go", ROUTINE_GO), ("rust", ROUTINE_RUST)):
        for scenario in scenarios:
            conversations.append(
                conversation(f"{language}-routine-{scenario[0]}", language, routine_turns(language, scenario))
            )
    for language, scenarios in (("go", ESCALATING_GO), ("rust", ESCALATING_RUST)):
        for scenario in scenarios:
            conversations.append(
                conversation(f"{language}-escalating-{scenario[0]}", language, routine_turns(language, scenario, escalating=True))
            )
    for language, scenarios in (("go", ADVANCED_GO), ("rust", ADVANCED_RUST)):
        for scenario in scenarios:
            conversations.append(
                conversation(f"{language}-advanced-{scenario[0]}", language, advanced_turns(language, scenario))
            )
    return conversations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT_DIR / "data" / "tuning-corpus.jsonl")
    args = parser.parse_args()

    conversations = build_conversations()
    turns = [turn for conversation_item in conversations for turn in conversation_item["turns"]]
    expected = {model: sum(turn["expected_model"] == model for turn in turns) for model in (LOW_COST_MODEL, EXPENSIVE_MODEL)}
    if len(conversations) != 50 or len(turns) != 200 or expected != EXPECTED_TURN_COUNTS:
        raise SystemExit(f"invalid corpus shape: conversations={len(conversations)} turns={len(turns)} expected={expected}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as stream:
        for item in conversations:
            stream.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"wrote {args.output}: conversations={len(conversations)} turns={len(turns)} expected={expected}")


if __name__ == "__main__":
    main()
