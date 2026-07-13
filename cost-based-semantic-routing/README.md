# Cost-based semantic routing

This demo combines agentgateway and vLLM Semantic Router (vSR) to route routine
prompts to a lower-cost model and advanced prompts to a higher-capability model.
It then measures whether that policy reduces realized LLM cost without hiding
the effects on answer quality or latency. The checked-in corpus is a tuning
fixture; publication-quality outcome claims require a separate frozen holdout
and blinded review.

The reusable routing configuration comes from the agentgateway semantic-routing
example. This repository owns the disposable cluster, forced-model baselines,
corpora, evaluation tooling, observability verification, and result chart used
to measure that configuration.

## Architecture

```mermaid
flowchart LR
    A[Application] -->|OpenAI request: model auto| G[agentgateway]
    G -->|Buffered ExtProc| S[vLLM Semantic Router]
    S -->|routine prompt| L[Lower-cost model]
    S -->|advanced prompt| H[Higher-capability model]
    L --> G
    H --> G
    G --> O[Logs, traces, metrics]
    C[Model cost catalog] --> G
    O --> P[Prometheus and Grafana]
```

vSR does not learn from historical request costs in this example. It classifies
each incoming prompt from semantic, complexity, keyword, context, and structure
signals. The policy maps that estimated need to a model tier. Agentgateway then
prices the realized request and provides the evidence used to tune the policy.

## Requirements

- Docker with at least 12 GB of memory for the default full observability stack
- At least 30 GB of free disk for images, the vSR model cache, and telemetry data
- `kind` 0.29 or newer, `kubectl`, `helm`, `curl`, `git`, and Python 3
- An OpenAI API key with access to both models in the upstream routing values

The script installs Kubernetes components, but it does not install host CLI
tools. It supports macOS and Linux and downloads a checksum-verified `agctl`
binary when one is not already installed.

The preflight check requires 30 GB free for the full profile, 20 GB for the
metrics profile, and 15 GB without observability. Override `MIN_FREE_DISK_GB`
only when the host filesystem does not reflect the capacity available to your
Docker runtime.

## Quick start

```bash
git clone https://github.com/danehans/agentgateway-demos.git
cd agentgateway-demos/cost-based-semantic-routing

export OPENAI_API_KEY='sk-...'
# Optional: export HF_TOKEN='hf_...' for higher model-download rate limits
./demo.sh all
```

The command pauses before sending billable OpenAI traffic. Use `--yes` for a
non-interactive run:

```bash
CAPTURE_OUTPUT=true ./demo.sh all --yes
```

The dedicated cluster always uses the `agentgateway-system` and `telemetry`
namespaces. The script intentionally ignores generic `NAMESPACE` and
`TELEMETRY_NAMESPACE` variables inherited from another Kubernetes workflow.

The default evaluation replays 50 four-turn Go and Rust developer conversations:
200 routing decisions across routine, escalating, and advanced work. Each turn
includes its canonical prior user and assistant context, so later requests
exercise a realistic growing transcript. This checked-in data is intentionally
for tuning and regression checks, not a proxy for production quality. It first
runs a two-turn smoke test and stops if either model is unavailable, preventing
a full run of known failures. Each corpus turn is sent through the three lanes,
for 600 billable model requests.

## What the script does

`./demo.sh all` performs the following steps:

1. Creates or reuses the dedicated `agentgateway-cost-routing` kind cluster.
2. Installs MetalLB, Gateway API, and stable agentgateway Helm charts.
3. Downloads `agctl` and imports current OpenAI pricing into a model catalog.
4. Stores the catalog in a ConfigMap and attaches it to the Gateway-level
   `AgentgatewayParameters` resource.
5. Installs Prometheus, Grafana, OpenTelemetry collectors, Loki, and Tempo.
6. Fetches the selected upstream agentgateway revision and records its SHA.
7. Installs vSR from the upstream Helm values, then applies the core routed
   configuration and this demo's forced-model baseline lanes.
8. Verifies buffered ExtProc by routing small routine and advanced coding
   prompts to their expected models.
9. Sends every selected corpus turn through `routed`, `always_low_cost`, and
   `always_expensive`, then summarizes cost, tuning agreement, and latency.

Every phase is gated by a post-install check. The script stops at the first
component that remains unhealthy after its retry window:

| Phase | Required checks |
|---|---|
| kind and MetalLB | Kubernetes readiness, storage class, controller and speaker rollout, address-pool configuration |
| agentgateway | Gateway API and agentgateway CRDs, controller rollout, accepted GatewayClass, programmed proxy, successful catalog mount/load, and LoadBalancer HTTP access |
| OpenAI and catalog | authenticated `/v1/models` access, Kubernetes Secret, ConfigMap, and priced catalog entries for every eval model |
| vSR | deployment rollout, bound model-cache PVC, `/ready`, `/config/router`, ExtProc gRPC connectivity, and `/metrics` |
| Metrics stack | Prometheus and Grafana rollouts, PromQL execution, Grafana datasource health, OTel scrape/remote-write pipeline, and dashboard discovery |
| Full OTel stack | Loki and Tempo readiness, log and trace collector connectivity, Loki datasource health, and Tempo datasource configuration |
| Routing verification | buffered ExtProc model selection plus agentgateway request/latency metrics and correlated Loki/Tempo records |
| Paid smoke | nonzero catalog-priced cost derived from token metrics, exact catalog-lookup counters, token and LLM-duration metrics, all three experiment lanes, and cost-bearing correlated logs/traces |

The standalone `verify` and `eval` commands repeat the deployed-stack checks
before probing or sending paid traffic. This catches components that became
unhealthy after the original setup completed.

Port-forward checks use temporary free local ports and are cleaned up on exit.
Failed checks are retried without flooding the terminal; the final diagnostic is
printed from `.work/cost-based-semantic-routing/last-verification.log`.

The default timeout is five minutes, vSR receives twenty minutes for model
downloads, and telemetry signals receive three minutes for export and indexing.
Tune these windows for slower environments:

```bash
VERIFY_TIMEOUT_SEC=600 \
VSR_READY_TIMEOUT_SEC=1800 \
SIGNAL_TIMEOUT_SEC=300 \
VERIFY_INTERVAL_SEC=5 \
./demo.sh all
```

The demo consumes these reusable configuration files from the selected
agentgateway revision:

- `k8s/semantic-router-values.yaml`
- `k8s/agentgateway-routing.yaml`

The corpus, runner, local summary, baseline routes, blinded-review tooling,
PromQL reference, and chart renderer live in this repository. The forced lanes accept
the `X-Eval-Lane` header only in the dedicated demo cluster; they are not part
of the upstream routing configuration.

The upstream values enable persistent vSR model storage on kind's `standard` storage class.
The first setup downloads the local classification and embedding models; tuning
redeploys reuse that cache.

The demo pins the vSR `0.3.0` Helm chart and `v0.3.0` `extproc` image so repeated
runs use the same release. Override both pins together when testing an upgrade:

```bash
VSR_CHART_VERSION=0.3.0 VSR_IMAGE_TAG=v0.3.0 ./demo.sh setup
```

The default upstream revision is `main`. Use a merged commit SHA for a
reproducible publication run:

```bash
EXAMPLE_REF=<agentgateway-commit-sha> ./demo.sh refresh
```

## Commands

```bash
./demo.sh setup       # Install the cluster components without paid traffic
./demo.sh verify      # Verify buffered ExtProc selects both expected models
./demo.sh eval        # Run the paid smoke test and experiment
./demo.sh report      # Regenerate local and Prometheus summary artifacts
./demo.sh chart       # Render an SVG chart from the latest result summary
./demo.sh review      # Create a blinded answer-quality review package
./demo.sh score       # Score the completed review and refresh report/chart
./demo.sh status      # Inspect resources and the resolved upstream revision
./demo.sh dashboard   # Open a Grafana port-forward on localhost:3000
./demo.sh cleanup     # Delete the dedicated cluster
```

`verify` sends two small billable requests: one routine coding prompt and one
advanced distributed-systems prompt. `setup` remains free of OpenAI requests.

For a smaller evaluation:

```bash
EVAL_LIMIT=50 ./demo.sh eval --yes
```

`EVAL_LIMIT=50` uses the checked-in [fixed tuning manifest](data/tuning-50-manifest.json),
which contains 25 expected lower-cost and 25 expected expensive selections.
It sends 150 corpus requests and lets tuning runs compare exactly the same
prompts. Do not use its label agreement as a publication-quality quality metric.
The separate two-turn smoke test sends six additional requests. Other nonzero
values select a deterministic, model-balanced subset. Set
`EVAL_MANIFEST=/path/to/manifest.json` to use a different fixed set; it must
contain the same number of IDs as `EVAL_LIMIT`.

For a lower-resource metrics-only stack:

```bash
OBSERVABILITY_PROFILE=metrics ./demo.sh all
```

Set `OBSERVABILITY_PROFILE=none` to skip Prometheus, Grafana, Loki, Tempo, and
the OpenTelemetry collectors. The local JSONL summary still works, but
agentgateway's catalog-backed Prometheus cost report is unavailable.

## Experiment lanes

| Lane | Model selection | Purpose |
|---|---|---|
| `routed` | vSR selects the model | Treatment |
| `always_low_cost` | Lower-cost model is forced | Cost and quality floor |
| `always_expensive` | Higher-capability model is forced | Savings counterfactual |

The runner randomizes prompt/lane order and adds request, experiment, eval, and
lane identifiers. Agentgateway adds `experiment_id` and `eval_lane` to
Prometheus metrics and sends the identifiers to access logs and traces when the
full profile is enabled.

Results are written to `results/`:

- `<RUN_ID>.jsonl`: request-level decisions, usage, cost estimate, and latency
- `<RUN_ID>-metadata.json`: component versions, source revision, dataset role, and dataset hash
- `<RUN_ID>-blind-review.csv`: anonymized transcripts and shuffled A/B/C answers
- `<RUN_ID>-blind-review-instructions.md`: rubric supplied to reviewers
- `<RUN_ID>-blind-review-key.json`: lane/model mapping; keep this from reviewers
- `<RUN_ID>-quality-review.json`: scored blinded-review outcomes
- `<RUN_ID>-summary.json`: structured local and catalog-backed Prometheus results
- `<RUN_ID>-summary.txt`: readable cost, quality, and latency report
- `<RUN_ID>-chart.svg`: blog-ready comparison of spend, quality retention, and latency

The local summary estimates cost from response token usage and the example's
rate table. The experiment-scoped Prometheus summary uses the run's unique
experiment ID, records the first and last request timestamps, prices
agentgateway token metrics with the generated model catalog, and requires every
catalog lookup to be `Exact`. Report generation waits until the lookup count
covers every result row, preventing a partial scrape from understating cost.
Agentgateway's access logs and traces provide the independent per-request
realized-cost signal. Treat the catalog-priced Prometheus summary
as the experiment's cost source of record. `./demo.sh report` regenerates both
summary artifacts for `results/<RUN_ID>.jsonl`, or for `RESULT_FILE` when it is
set. When Prometheus is disabled or unavailable, that status and reason are
preserved in both artifacts instead of silently omitting the section.
For manual inspection, `promql/queries.promql` contains queries scoped to one
`experiment_id`, rather than a shared time window.
The fetched checkout records both `EXAMPLE_REPO_URL` and `EXAMPLE_REF`. Setup
and evaluation reuse it only when both still match, preventing a local
development checkout from silently supplying a different routing configuration.
Run `./demo.sh refresh --yes` to deliberately discard local tuning and fetch
the requested source again.

The evaluation and `report` command generate the SVG chart automatically. It
prefers catalog-priced agentgateway metrics when Prometheus is available and
otherwise labels the local token-cost estimate. Regenerate a chart later without
requiring a Kubernetes cluster:

```bash
SUMMARY_FILE=results/<RUN_ID>-summary.json ./demo.sh chart
```

The SVG compares the routed lane with the always-expensive counterfactual,
reports blinded quality retention when available, and shows routed p50/p95
latency. Before review is scored, it explicitly reports quality as pending;
it never presents corpus-label agreement as an outcome-quality result.

## Tune the routing policy

After setup, the editable vSR values are located at:

```text
../.work/cost-based-semantic-routing/agentgateway/examples/llm-semantic-routing/k8s/semantic-router-values.yaml
```

The first tuning lever is the threshold for `advanced_need_band`. Lowering it
routes more boundary prompts to the higher-capability model; raising it favors
the lower-cost model. You can also adjust signal weights, keyword sets,
embedding candidates, and complexity examples.

Redeploy and rerun after editing:

```bash
./demo.sh router
./demo.sh eval
```

Review false negatives first: advanced prompts sent to the lower-cost model.
Then inspect false positives, where routine prompts consumed the expensive
model. Keep `EVAL_LIMIT=50` for tuning comparisons so each iteration uses the
same prompt set. Freeze the routing policy before evaluating a holdout; do not
adjust it after looking at holdout misses. A production threshold should
optimize for an explicit quality SLO, not the highest possible savings
percentage.

`./demo.sh refresh` discards local tuning and fetches `EXAMPLE_REF` again.

## Measure answer quality

The tuning corpus answers whether a routing policy behaves consistently. It
does not establish that lower-cost answers meet a real developer's quality bar.
For a publishable result, use a separate, frozen holdout of complete developer
conversations. Keep every turn of a conversation in one split, sample approved
and redacted production-like work where possible, and include both routine
tasks containing advanced terms and advanced tasks that avoid the policy's
marker phrases.

Run that holdout through all three lanes with response capture enabled:

```bash
EVAL_DATASET=/absolute/path/holdout-corpus.jsonl \
EVAL_DATASET_ROLE=holdout \
CAPTURE_OUTPUT=true \
./demo.sh eval --yes
```

`EVAL_DATASET_ROLE=holdout` and the dataset SHA-256 are persisted in the run
metadata. The script accepts the same JSONL schema as the tuning corpus. Keep
the holdout outside this public repository when it contains approved internal
traffic.

Create the blinded review package after the run:

```bash
RESULT_FILE=results/<RUN_ID>.jsonl ./demo.sh review
```

Give reviewers `<RUN_ID>-blind-review.csv` and its instructions, not the
corresponding key.
For each A/B/C answer, reviewers record a 1-to-5 quality score, whether it is
acceptable for the task, and whether one answer is materially better. After
ratings are complete, score and regenerate the report:

```bash
RESULT_FILE=results/<RUN_ID>.jsonl ./demo.sh score
```

The primary outcome is **quality retained**:

```text
acceptable routed answers / acceptable always-expensive answers
```

The summary also reports blinded-review coverage and how often the routed answer
was materially worse than the expensive baseline. These answer-quality measures
belong next to savings and latency. Corpus-label selection agreement remains in
the JSON and text report as a tuning diagnostic only.

Blinded review is a quality proxy, not direct user satisfaction. Before broad
rollout, validate the policy in shadow mode against sanitized production traffic
and pair it with product signals such as task completion, explicit feedback,
retry rate, and escalation rate.

## Model availability

The configured model names come from the fetched upstream revision. Model
catalog entries do not grant API access. If OpenAI returns `model_not_found`,
verify the model IDs and project access, update the selected upstream
configuration, refresh this demo, and rerun. The smoke result file preserves the
exact error response for diagnosis.

## Resources

- [agentgateway semantic-routing example](https://github.com/agentgateway/agentgateway/tree/main/examples/llm-semantic-routing)
- [agentgateway model costs](https://agentgateway.dev/docs/kubernetes/main/llm/costs/)
- [agentgateway cost tracking](https://agentgateway.dev/docs/kubernetes/main/llm/cost-tracking/)
- [agentgateway OTel stack](https://agentgateway.dev/docs/kubernetes/main/observability/otel-stack/)
- [vSR agentgateway integration](https://vllm-semantic-router.com/docs/installation/k8s/agentgateway/)
