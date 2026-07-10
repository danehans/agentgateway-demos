#!/usr/bin/env bash

set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${ROOT_DIR}/.." && pwd)"
CONFIG_DIR="${ROOT_DIR}/config"
WORK_DIR="${REPO_ROOT}/.work/cost-based-semantic-routing"
CHECKOUT_DIR="${WORK_DIR}/agentgateway"
EXAMPLE_DIR="${CHECKOUT_DIR}/examples/llm-semantic-routing"
RESULTS_DIR="${ROOT_DIR}/results"

# shellcheck disable=SC1091
source "${CONFIG_DIR}/versions.env"

CLUSTER_NAME="${CLUSTER_NAME:-agentgateway-cost-routing}"
KIND_NODE_IMAGE="${KIND_NODE_IMAGE:-kindest/node:v1.34.0}"
NAMESPACE="${NAMESPACE:-agentgateway-system}"
TELEMETRY_NAMESPACE="${TELEMETRY_NAMESPACE:-telemetry}"
if [[ -z "${OBSERVABILITY_PROFILE:-}" && -f "${WORK_DIR}/observability-profile" ]]; then
  OBSERVABILITY_PROFILE="$(cat "${WORK_DIR}/observability-profile")"
fi
OBSERVABILITY_PROFILE="${OBSERVABILITY_PROFILE:-full}"
EVAL_LIMIT="${EVAL_LIMIT:-20}"
SMOKE_LIMIT="${SMOKE_LIMIT:-2}"
EVAL_DELAY_SEC="${EVAL_DELAY_SEC:-1}"
CAPTURE_OUTPUT="${CAPTURE_OUTPUT:-false}"
PROMETHEUS_PORT="${PROMETHEUS_PORT:-19090}"
PROMETHEUS_WINDOW="${PROMETHEUS_WINDOW:-30m}"
EXAMPLE_REPO_URL="${EXAMPLE_REPO_URL:-https://github.com/agentgateway/agentgateway.git}"
METALLB_IP_RANGE="${METALLB_IP_RANGE:-}"
MIN_FREE_DISK_GB="${MIN_FREE_DISK_GB:-}"
YES=false

log() {
  printf '\n==> %s\n' "$*"
}

warn() {
  printf 'warning: %s\n' "$*" >&2
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

on_error() {
  printf 'error: command failed at %s:%s\n' "${BASH_SOURCE[1]}" "${BASH_LINENO[0]}" >&2
}
trap on_error ERR

usage() {
  cat <<'EOF'
Usage: ./demo.sh COMMAND [--yes]

Commands:
  all        Set up the stack, verify streamed ExtProc, and run the evaluation
  setup      Create the cluster and install agentgateway, observability, and vSR
  verify     Run the zero-token immediate-response ExtProc probe
  eval       Run a paid smoke test, the three-lane corpus, and a result summary
  report     Regenerate the latest text and JSON result summaries
  router     Redeploy vSR and experiment resources after tuning the fetched config
  refresh    Replace the fetched PR #2486 checkout with EXAMPLE_REF
  status     Show the deployed resources and resolved example revision
  dashboard  Port-forward Grafana to http://localhost:3000
  cleanup    Delete the demo cluster, or demo namespaces on a reused cluster
  help       Show this help

Important environment variables:
  OPENAI_API_KEY          Required by setup and all; not written to local files
  HF_TOKEN                Optional; raises Hugging Face download rate limits
  OBSERVABILITY_PROFILE   full (default), metrics, or none
  EVAL_LIMIT              Corpus rows to run; defaults to 20 (60 requests)
  CAPTURE_OUTPUT          true to save model responses for satisfaction scoring
  EXAMPLE_REF             Defaults to refs/pull/2486/head; use a SHA to pin a run
  CLUSTER_NAME            Defaults to agentgateway-cost-routing
  METALLB_IP_RANGE        Optional explicit range, for example 172.18.250.10-172.18.250.60
  MIN_FREE_DISK_GB        Override the host free-space guard; set 0 to disable it

The eval command sends billable OpenAI requests and asks for confirmation unless
--yes is supplied.
EOF
}

confirm() {
  local prompt="$1"
  if [[ "${YES}" == "true" ]]; then
    return
  fi
  if [[ ! -t 0 ]]; then
    die "${prompt} Re-run with --yes to confirm non-interactively."
  fi
  read -r -p "${prompt} [y/N] " answer
  case "${answer}" in
    y|Y|yes|YES) ;;
    *) die "cancelled" ;;
  esac
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

preflight() {
  local command
  for command in docker kind kubectl helm curl git python3; do
    require_command "${command}"
  done
  docker info >/dev/null 2>&1 || die "Docker is not running"
  case "${OBSERVABILITY_PROFILE}" in
    full|metrics|none) ;;
    *) die "OBSERVABILITY_PROFILE must be full, metrics, or none" ;;
  esac
  [[ "${EVAL_LIMIT}" =~ ^[0-9]+$ ]] || die "EVAL_LIMIT must be an integer"
  [[ "${SMOKE_LIMIT}" =~ ^[0-9]+$ ]] || die "SMOKE_LIMIT must be an integer"
  mkdir -p "${WORK_DIR}" "${RESULTS_DIR}"
}

check_disk_space() {
  local available_kb required_kb
  if [[ -z "${MIN_FREE_DISK_GB}" ]]; then
    case "${OBSERVABILITY_PROFILE}" in
      full) MIN_FREE_DISK_GB=30 ;;
      metrics) MIN_FREE_DISK_GB=20 ;;
      none) MIN_FREE_DISK_GB=15 ;;
    esac
  fi
  [[ "${MIN_FREE_DISK_GB}" =~ ^[0-9]+$ ]] || die "MIN_FREE_DISK_GB must be an integer"
  if [[ "${MIN_FREE_DISK_GB}" -eq 0 ]]; then
    return
  fi
  available_kb="$(df -Pk "${HOME}" | awk 'NR == 2 {print $4}')"
  required_kb=$((MIN_FREE_DISK_GB * 1024 * 1024))
  if [[ -n "${available_kb}" && "${available_kb}" -lt "${required_kb}" ]]; then
    die "at least ${MIN_FREE_DISK_GB} GiB of free host disk is required; only $((available_kb / 1024 / 1024)) GiB is available"
  fi
}

ensure_cluster() {
  log "Creating or reusing kind cluster ${CLUSTER_NAME}"
  if ! kind get clusters 2>/dev/null | grep -Fxq "${CLUSTER_NAME}"; then
    kind create cluster --name "${CLUSTER_NAME}" --image "${KIND_NODE_IMAGE}"
    touch "${WORK_DIR}/cluster-created"
  fi
  kubectl config use-context "kind-${CLUSTER_NAME}" >/dev/null
  kubectl wait --for=condition=Ready nodes --all --timeout=300s
  kubectl get storageclass standard >/dev/null 2>&1 || \
    die "kind cluster must provide the standard storage class used by vSR persistence"
}

use_cluster() {
  kind get clusters 2>/dev/null | grep -Fxq "${CLUSTER_NAME}" || \
    die "kind cluster ${CLUSTER_NAME} does not exist; run ./demo.sh setup"
  kubectl config use-context "kind-${CLUSTER_NAME}" >/dev/null
}

derive_metallb_range() {
  if [[ -n "${METALLB_IP_RANGE}" ]]; then
    printf '%s\n' "${METALLB_IP_RANGE}"
    return
  fi

  local network_json
  network_json="$(docker network inspect kind)"
  DOCKER_NETWORK_JSON="${network_json}" DEMO_CLUSTER_NAME="${CLUSTER_NAME}" python3 - <<'PY'
import hashlib
import json
import os
from ipaddress import IPv4Address, ip_network

network = json.loads(os.environ["DOCKER_NETWORK_JSON"])[0]
subnets = [
    item.get("Subnet")
    for item in network.get("IPAM", {}).get("Config", [])
    if item.get("Subnet") and ":" not in item.get("Subnet")
]
if not subnets:
    raise SystemExit("no IPv4 subnet found in Docker network 'kind'")

subnet = ip_network(subnets[0])
first = int(subnet.network_address) + 1
last = int(subnet.broadcast_address) - 1
usable = last - first + 1
block_size = min(51, max(1, usable // 8))
reserve_top = min(256, max(block_size, usable // 4))
slot_count = max(1, (usable - reserve_top) // max(64, block_size))
digest = hashlib.sha256(os.environ["DEMO_CLUSTER_NAME"].encode()).digest()
slot = int.from_bytes(digest[:4], "big") % slot_count
end = last - reserve_top - slot * max(64, block_size)
start = max(first, end - block_size + 1)
if end < start:
    raise SystemExit(f"Docker subnet {subnet} is too small for a MetalLB range")
print(f"{IPv4Address(start)}-{IPv4Address(end)}")
PY
}

apply_metallb_config() {
  local range="$1"
  kubectl apply -f - <<EOF
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata:
  name: demo-pool
  namespace: metallb-system
spec:
  addresses:
  - ${range}
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata:
  name: demo-l2
  namespace: metallb-system
spec:
  ipAddressPools:
  - demo-pool
EOF
}

install_metallb() {
  log "Installing MetalLB ${METALLB_VERSION}"
  kubectl apply -f "https://raw.githubusercontent.com/metallb/metallb/${METALLB_VERSION}/config/manifests/metallb-native.yaml"
  kubectl rollout status deployment/controller -n metallb-system --timeout=300s
  kubectl rollout status daemonset/speaker -n metallb-system --timeout=300s

  local range attempt
  range="$(derive_metallb_range)"
  log "Configuring MetalLB range ${range}"
  for attempt in 1 2 3 4 5; do
    if apply_metallb_config "${range}"; then
      return
    fi
    warn "MetalLB webhook is not ready yet (attempt ${attempt}/5)"
    sleep 3
  done
  die "failed to configure MetalLB"
}

install_agentgateway() {
  log "Installing Gateway API ${GATEWAY_API_VERSION}"
  kubectl apply --server-side -f \
    "https://github.com/kubernetes-sigs/gateway-api/releases/download/${GATEWAY_API_VERSION}/standard-install.yaml"

  log "Installing agentgateway ${AGENTGATEWAY_VERSION}"
  helm upgrade --install agentgateway-crds \
    oci://cr.agentgateway.dev/charts/agentgateway-crds \
    --create-namespace \
    --namespace "${NAMESPACE}" \
    --version "${AGENTGATEWAY_VERSION}" \
    --wait --timeout 10m
  helm upgrade --install agentgateway \
    oci://cr.agentgateway.dev/charts/agentgateway \
    --namespace "${NAMESPACE}" \
    --version "${AGENTGATEWAY_VERSION}" \
    --set controller.extraEnv.KGW_ENABLE_GATEWAY_API_EXPERIMENTAL_FEATURES=true \
    --wait --timeout 10m
  kubectl wait --for=condition=Accepted gatewayclass/agentgateway --timeout=300s
}

fetch_example() {
  if [[ -d "${CHECKOUT_DIR}/.git" ]]; then
    return
  fi

  log "Fetching agentgateway/agentgateway#2486 (${EXAMPLE_REF})"
  mkdir -p "${CHECKOUT_DIR}"
  git -C "${CHECKOUT_DIR}" init --quiet
  git -C "${CHECKOUT_DIR}" remote add origin "${EXAMPLE_REPO_URL}"
  git -C "${CHECKOUT_DIR}" fetch --depth 1 origin "${EXAMPLE_REF}"
  git -C "${CHECKOUT_DIR}" checkout --detach --quiet FETCH_HEAD
  git -C "${CHECKOUT_DIR}" rev-parse HEAD > "${WORK_DIR}/example-revision"
  [[ -d "${EXAMPLE_DIR}" ]] || die "${EXAMPLE_REF} does not contain examples/llm-semantic-routing"
}

agctl_path() {
  if [[ -n "${AGCTL_BIN:-}" ]]; then
    printf '%s\n' "${AGCTL_BIN}"
    return
  fi
  if command -v agctl >/dev/null 2>&1; then
    command -v agctl
    return
  fi

  local os arch asset bin_dir checksum_tool
  case "$(uname -s)" in
    Darwin) os=darwin ;;
    Linux) os=linux ;;
    *) die "automatic agctl download supports macOS and Linux" ;;
  esac
  case "$(uname -m)" in
    arm64|aarch64) arch=arm64 ;;
    x86_64|amd64) arch=amd64 ;;
    *) die "unsupported architecture for agctl: $(uname -m)" ;;
  esac

  asset="agctl-${os}-${arch}"
  bin_dir="${WORK_DIR}/bin"
  mkdir -p "${bin_dir}"
  if [[ ! -x "${bin_dir}/agctl" ]]; then
    log "Downloading agctl v${AGENTGATEWAY_VERSION}" >&2
    curl -fsSL \
      "https://github.com/agentgateway/agentgateway/releases/download/v${AGENTGATEWAY_VERSION}/${asset}" \
      -o "${bin_dir}/${asset}"
    curl -fsSL \
      "https://github.com/agentgateway/agentgateway/releases/download/v${AGENTGATEWAY_VERSION}/${asset}.sha256" \
      -o "${bin_dir}/${asset}.sha256"
    if command -v sha256sum >/dev/null 2>&1; then
      checksum_tool=sha256sum
      (cd "${bin_dir}" && "${checksum_tool}" -c "${asset}.sha256")
    else
      (cd "${bin_dir}" && shasum -a 256 -c "${asset}.sha256")
    fi
    mv "${bin_dir}/${asset}" "${bin_dir}/agctl"
    chmod +x "${bin_dir}/agctl"
  fi
  printf '%s\n' "${bin_dir}/agctl"
}

configure_openai_and_catalog() {
  [[ -n "${OPENAI_API_KEY:-}" ]] || die "OPENAI_API_KEY is required"
  log "Configuring the OpenAI credential"
  kubectl create secret generic openai-secret \
    --namespace "${NAMESPACE}" \
    --from-literal="Authorization=${OPENAI_API_KEY}" \
    --dry-run=client -o yaml | kubectl apply -f -

  local agctl
  agctl="$(agctl_path)"
  log "Generating the OpenAI model cost catalog with agctl"
  "${agctl}" costs import --pretty --providers openai --out "${WORK_DIR}/catalog.json"
  kubectl create configmap semantic-routing-model-costs \
    --namespace "${NAMESPACE}" \
    --from-file="catalog.json=${WORK_DIR}/catalog.json" \
    --dry-run=client -o yaml | kubectl apply -f -
}

install_observability() {
  local prometheus_values
  if [[ "${OBSERVABILITY_PROFILE}" == "none" ]]; then
    log "Skipping observability stack (OBSERVABILITY_PROFILE=none)"
    return
  fi

  log "Installing Prometheus and Grafana"
  prometheus_values="${CONFIG_DIR}/observability/kube-prometheus-stack-values.yaml"
  if [[ "${OBSERVABILITY_PROFILE}" == "full" ]]; then
    prometheus_values="${prometheus_values},${CONFIG_DIR}/observability/kube-prometheus-stack-full-values.yaml"
  fi
  helm upgrade --install kube-prometheus-stack kube-prometheus-stack \
    --repo https://prometheus-community.github.io/helm-charts \
    --version "${PROMETHEUS_STACK_VERSION}" \
    --namespace "${TELEMETRY_NAMESPACE}" \
    --create-namespace \
    --values "${prometheus_values}" \
    --wait --timeout 15m

  log "Installing the OpenTelemetry metrics collector"
  helm upgrade --install opentelemetry-collector-metrics opentelemetry-collector \
    --repo https://open-telemetry.github.io/opentelemetry-helm-charts \
    --version "${OTEL_COLLECTOR_CHART_VERSION}" \
    --namespace "${TELEMETRY_NAMESPACE}" \
    --values "${CONFIG_DIR}/observability/otel-metrics-values.yaml" \
    --wait --timeout 10m

  if [[ "${OBSERVABILITY_PROFILE}" == "full" ]]; then
    log "Installing Loki and Tempo"
    helm upgrade --install loki loki \
      --repo https://grafana.github.io/helm-charts \
      --version "${LOKI_CHART_VERSION}" \
      --namespace "${TELEMETRY_NAMESPACE}" \
      --values "${CONFIG_DIR}/observability/loki-values.yaml" \
      --wait --timeout 15m
    helm upgrade --install tempo tempo \
      --repo https://grafana.github.io/helm-charts \
      --version "${TEMPO_CHART_VERSION}" \
      --namespace "${TELEMETRY_NAMESPACE}" \
      --values "${CONFIG_DIR}/observability/tempo-values.yaml" \
      --wait --timeout 10m

    log "Installing the OpenTelemetry log and trace collectors"
    helm upgrade --install opentelemetry-collector-logs opentelemetry-collector \
      --repo https://open-telemetry.github.io/opentelemetry-helm-charts \
      --version "${OTEL_COLLECTOR_CHART_VERSION}" \
      --namespace "${TELEMETRY_NAMESPACE}" \
      --values "${CONFIG_DIR}/observability/otel-logs-values.yaml" \
      --wait --timeout 10m
    helm upgrade --install opentelemetry-collector-traces opentelemetry-collector \
      --repo https://open-telemetry.github.io/opentelemetry-helm-charts \
      --version "${OTEL_COLLECTOR_CHART_VERSION}" \
      --namespace "${TELEMETRY_NAMESPACE}" \
      --values "${CONFIG_DIR}/observability/otel-traces-values.yaml" \
      --wait --timeout 10m
  fi

  log "Installing the agentgateway Grafana dashboard"
  curl -fsSL \
    https://raw.githubusercontent.com/agentgateway/agentgateway/main/controller/install/helm/agentgateway/files/agentgateway-dashboard.json \
    -o "${WORK_DIR}/agentgateway-dashboard.json"
  kubectl create configmap agentgateway-dashboard \
    --namespace "${TELEMETRY_NAMESPACE}" \
    --from-file="agentgateway.json=${WORK_DIR}/agentgateway-dashboard.json" \
    --dry-run=client -o yaml | kubectl apply -f -
  kubectl label configmap agentgateway-dashboard \
    --namespace "${TELEMETRY_NAMESPACE}" \
    grafana_dashboard=1 --overwrite
}

deploy_gateway() {
  log "Creating the catalog-backed agentgateway proxy"
  kubectl apply -f "${CONFIG_DIR}/gateway.yaml"
  kubectl wait --for=condition=Programmed gateway/agentgateway-proxy \
    --namespace "${NAMESPACE}" --timeout=600s
  kubectl rollout status deployment/agentgateway-proxy \
    --namespace "${NAMESPACE}" --timeout=300s
}

deploy_router() {
  fetch_example
  if [[ -n "${HF_TOKEN:-}" ]]; then
    kubectl create secret generic hf-token-secret \
      --namespace "${NAMESPACE}" \
      --from-literal="token=${HF_TOKEN}" \
      --dry-run=client -o yaml | kubectl apply -f -
  fi
  log "Installing vLLM Semantic Router ${VSR_CHART_VERSION}"
  helm upgrade --install semantic-router \
    oci://ghcr.io/vllm-project/charts/semantic-router \
    --version "${VSR_CHART_VERSION}" \
    --namespace "${NAMESPACE}" \
    --values "${EXAMPLE_DIR}/k8s/semantic-router-values.yaml" \
    --wait --timeout 20m

  log "Applying the three experiment lanes and streamed ExtProc policy"
  kubectl apply -f "${EXAMPLE_DIR}/k8s/agentgateway-experiment.yaml"
  kubectl wait --for=condition=Accepted agentgatewaybackend --all \
    --namespace "${NAMESPACE}" --timeout=300s
  kubectl wait --for=condition=Accepted httproute --all \
    --namespace "${NAMESPACE}" --timeout=300s

  if [[ "${OBSERVABILITY_PROFILE}" == "full" ]]; then
    kubectl apply -f "${CONFIG_DIR}/telemetry-full.yaml"
  else
    kubectl apply -f "${CONFIG_DIR}/telemetry-metrics.yaml"
  fi
}

gateway_url() {
  local address attempt
  for attempt in $(seq 1 60); do
    address="$(kubectl get gateway agentgateway-proxy \
      --namespace "${NAMESPACE}" \
      -o jsonpath='{.status.addresses[0].value}' 2>/dev/null || true)"
    if [[ -n "${address}" ]]; then
      printf 'http://%s\n' "${address}"
      return
    fi
    sleep 2
  done
  die "Gateway did not receive an address"
}

cmd_setup() {
  preflight
  check_disk_space
  printf '%s\n' "${OBSERVABILITY_PROFILE}" > "${WORK_DIR}/observability-profile"
  ensure_cluster
  install_metallb
  install_agentgateway
  fetch_example
  configure_openai_and_catalog
  install_observability
  deploy_gateway
  deploy_router
  log "Setup complete"
  printf 'Gateway: %s\n' "$(gateway_url)"
  printf 'Example revision: %s\n' "$(cat "${WORK_DIR}/example-revision")"
}

cmd_verify() {
  preflight
  use_cluster
  fetch_example
  local url headers body status
  url="$(gateway_url)"
  headers="${WORK_DIR}/immediate-response.headers"
  body="${WORK_DIR}/immediate-response.json"

  log "Verifying streamed ExtProc and immediate responses without calling OpenAI"
  status="$(curl -sS --max-time 60 -D "${headers}" -o "${body}" -w '%{http_code}' \
    "${url}/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -H 'X-VSR-Debug: true' \
    -H 'X-Request-ID: semantic-routing-demo-immediate-response' \
    -d '{"model":"auto","messages":[{"role":"user","content":"VSR_IMMEDIATE_RESPONSE_PROBE"}],"max_tokens":16}')"

  [[ "${status}" == "200" ]] || {
    cat "${body}" >&2
    die "immediate-response probe returned HTTP ${status}"
  }
  grep -Eiq '^x-vsr-fast-response:[[:space:]]*true' "${headers}" || \
    die "response did not contain x-vsr-fast-response: true"
  grep -Eiq '^x-vsr-selected-decision:[[:space:]]*immediate_response_probe' "${headers}" || \
    die "response did not select immediate_response_probe"

  printf 'Streamed ExtProc probe passed: HTTP 200, fast response, zero upstream tokens.\n'
}

write_metadata() {
  local run_id="$1" result_file="$2"
  RUN_ID="${run_id}" \
  RESULT_FILE="${result_file}" \
  EXAMPLE_SHA="$(cat "${WORK_DIR}/example-revision")" \
  AGW_VERSION="${AGENTGATEWAY_VERSION}" \
  VSR_VERSION="${VSR_CHART_VERSION}" \
  OBS_PROFILE="${OBSERVABILITY_PROFILE}" \
  python3 - <<'PY'
import json
import os
from datetime import datetime, timezone

with open(os.environ["RESULT_FILE"], encoding="utf-8") as stream:
    rows = [json.loads(line) for line in stream if line.strip()]

metadata = {
    "run_id": os.environ["RUN_ID"],
    "created_at": datetime.now(timezone.utc).isoformat(),
    "agentgateway_version": os.environ["AGW_VERSION"],
    "semantic_router_chart_version": os.environ["VSR_VERSION"],
    "example_commit": os.environ["EXAMPLE_SHA"],
    "observability_profile": os.environ["OBS_PROFILE"],
    "requests": len(rows),
    "selected_models": sorted({row.get("selected_model", "") for row in rows if row.get("selected_model")}),
}
path = os.path.join(os.path.dirname(os.environ["RESULT_FILE"]), os.environ["RUN_ID"] + "-metadata.json")
with open(path, "w", encoding="utf-8") as stream:
    json.dump(metadata, stream, indent=2)
    stream.write("\n")
print(f"metadata={path}")
PY
}

run_eval_file() {
  local run_id="$1" output="$2" limit="$3"
  local capture_args=()
  if [[ "${CAPTURE_OUTPUT}" == "true" ]]; then
    capture_args+=(--capture-output)
  fi
  python3 "${EXAMPLE_DIR}/scripts/run_eval.py" \
    --gateway-url "$(gateway_url)" \
    --run-id "${run_id}" \
    --output "${output}" \
    --limit "${limit}" \
    --delay-sec "${EVAL_DELAY_SEC}" \
    "${capture_args[@]}"
  python3 "${ROOT_DIR}/scripts/validate_results.py" "${output}"
}

cmd_eval() {
  preflight
  use_cluster
  fetch_example
  confirm "This evaluation sends billable requests to OpenAI. Continue?"

  local run_id smoke_id smoke_file result_file
  run_id="$(date -u +%Y%m%dT%H%M%SZ)"
  smoke_id="${run_id}-smoke"
  smoke_file="${RESULTS_DIR}/${smoke_id}.jsonl"
  result_file="${RESULTS_DIR}/${run_id}.jsonl"

  log "Running a ${SMOKE_LIMIT}-prompt model-access smoke test"
  run_eval_file "${smoke_id}" "${smoke_file}" "${SMOKE_LIMIT}"

  log "Running ${EVAL_LIMIT} prompts through routed, always_low_cost, and always_expensive"
  run_eval_file "${run_id}" "${result_file}" "${EVAL_LIMIT}"
  printf '%s\n' "${result_file}" > "${RESULTS_DIR}/latest-result"
  write_metadata "${run_id}" "${result_file}"

  if [[ "${CAPTURE_OUTPUT}" == "true" ]]; then
    cp "${EXAMPLE_DIR}/data/ratings-template.csv" "${RESULTS_DIR}/${run_id}-ratings.csv"
  fi

  cmd_report
}

cmd_report() {
  preflight
  use_cluster
  fetch_example
  local result_file result_base summary_json summary_text
  local local_json local_text prometheus_json prometheus_text ratings_file
  local port_forward_pid ready attempt prometheus_status prometheus_reason
  local summary_args=() ratings_args=()
  if [[ -n "${RESULT_FILE:-}" ]]; then
    result_file="${RESULT_FILE}"
  elif [[ -f "${RESULTS_DIR}/latest-result" ]]; then
    result_file="$(cat "${RESULTS_DIR}/latest-result")"
  else
    die "no result file found; run ./demo.sh eval or set RESULT_FILE"
  fi
  [[ -f "${result_file}" ]] || die "result file does not exist: ${result_file}"
  python3 "${EXAMPLE_DIR}/scripts/summarize_results.py" --help \
    | grep -Fq -- '--json-output' \
    || die "the fetched example predates persisted summaries; run ./demo.sh refresh --yes"

  result_base="${result_file%.jsonl}"
  summary_json="${result_base}-summary.json"
  summary_text="${result_base}-summary.txt"
  local_json="${WORK_DIR}/$(basename "${result_base}")-local-summary.json"
  local_text="${WORK_DIR}/$(basename "${result_base}")-local-summary.txt"
  prometheus_json="${WORK_DIR}/$(basename "${result_base}")-prometheus-summary.json"
  prometheus_text="${WORK_DIR}/$(basename "${result_base}")-prometheus-summary.txt"
  ratings_file="${result_base}-ratings.csv"
  rm -f "${prometheus_json}" "${prometheus_text}"

  if [[ -f "${ratings_file}" ]]; then
    ratings_args+=(--ratings "${ratings_file}")
  fi
  python3 "${EXAMPLE_DIR}/scripts/summarize_results.py" \
    "${result_file}" \
    --json-output "${local_json}" \
    --text-output "${local_text}" \
    "${ratings_args[@]}" \
    >/dev/null

  prometheus_status=disabled
  prometheus_reason="observability_profile_none"
  if [[ "${OBSERVABILITY_PROFILE}" != "none" ]]; then
    prometheus_status=unavailable
    prometheus_reason=port_forward_not_ready

    kubectl port-forward \
      --namespace "${TELEMETRY_NAMESPACE}" \
      service/kube-prometheus-stack-prometheus \
      "${PROMETHEUS_PORT}:9090" >"${WORK_DIR}/prometheus-port-forward.log" 2>&1 &
    port_forward_pid=$!
    ready=false
    for attempt in $(seq 1 30); do
      if curl -fsS "http://127.0.0.1:${PROMETHEUS_PORT}/-/ready" >/dev/null 2>&1; then
        ready=true
        break
      fi
      sleep 1
    done
    if [[ "${ready}" == "true" ]]; then
      if python3 "${ROOT_DIR}/scripts/prometheus_report.py" \
        --url "http://127.0.0.1:${PROMETHEUS_PORT}" \
        --window "${PROMETHEUS_WINDOW}" \
        --json-output "${prometheus_json}" \
        >"${prometheus_text}"; then
        prometheus_status=collected
        prometheus_reason=""
      else
        prometheus_reason=query_failed
        warn "Prometheus report query failed"
      fi
    else
      warn "Prometheus port-forward did not become ready"
    fi
    kill "${port_forward_pid}" >/dev/null 2>&1 || true
    wait "${port_forward_pid}" 2>/dev/null || true
  fi

  summary_args=(
    --results "${result_file}"
    --local-json "${local_json}"
    --local-text "${local_text}"
    --prometheus-status "${prometheus_status}"
    --prometheus-reason "${prometheus_reason}"
    --output-json "${summary_json}"
    --output-text "${summary_text}"
  )
  if [[ "${prometheus_status}" == "collected" ]]; then
    summary_args+=(
      --prometheus-json "${prometheus_json}"
      --prometheus-text "${prometheus_text}"
    )
  fi
  python3 "${ROOT_DIR}/scripts/assemble_summary.py" "${summary_args[@]}"

  log "Experiment summary"
  cat "${summary_text}"
  printf 'JSON summary: %s\n' "${summary_json}"
  printf 'Text summary: %s\n' "${summary_text}"
}

cmd_router() {
  preflight
  use_cluster
  deploy_router
  cmd_verify
  printf 'Tuned values: %s\n' "${EXAMPLE_DIR}/k8s/semantic-router-values.yaml"
}

cmd_refresh() {
  preflight
  confirm "Replace the fetched example and discard local tuning changes?"
  [[ "${CHECKOUT_DIR}" == "${WORK_DIR}"/* ]] || die "unsafe checkout path"
  rm -rf "${CHECKOUT_DIR}"
  fetch_example
  printf 'Example revision: %s\n' "$(cat "${WORK_DIR}/example-revision")"
}

cmd_status() {
  preflight
  use_cluster
  printf 'Example revision: '
  if [[ -f "${WORK_DIR}/example-revision" ]]; then
    cat "${WORK_DIR}/example-revision"
  else
    printf 'not fetched\n'
  fi
  kubectl get gateway,httproute,agentgatewaybackend,agentgatewaypolicy \
    --namespace "${NAMESPACE}" 2>/dev/null || true
  kubectl get pods --namespace "${NAMESPACE}" 2>/dev/null || true
  if [[ "${OBSERVABILITY_PROFILE}" != "none" ]]; then
    kubectl get pods --namespace "${TELEMETRY_NAMESPACE}" 2>/dev/null || true
  fi
}

cmd_dashboard() {
  preflight
  use_cluster
  printf 'Grafana: http://localhost:3000 (admin / prom-operator)\n'
  kubectl port-forward deployment/kube-prometheus-stack-grafana \
    --namespace "${TELEMETRY_NAMESPACE}" 3000:3000
}

cmd_cleanup() {
  preflight
  use_cluster
  confirm "Delete the semantic routing demo resources?"
  if [[ -f "${WORK_DIR}/cluster-created" ]] && kind get clusters | grep -Fxq "${CLUSTER_NAME}"; then
    kind delete cluster --name "${CLUSTER_NAME}"
    rm -f "${WORK_DIR}/cluster-created"
    return
  fi
  warn "The named cluster predates this checkout; deleting only demo namespaces."
  kubectl delete namespace "${NAMESPACE}" "${TELEMETRY_NAMESPACE}" metallb-system --ignore-not-found
}

COMMAND="${1:-help}"
if [[ $# -gt 0 ]]; then
  shift
fi
while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes) YES=true ;;
    -h|--help) COMMAND=help ;;
    *) die "unknown option: $1" ;;
  esac
  shift
done

case "${COMMAND}" in
  all)
    cmd_setup
    cmd_verify
    cmd_eval
    ;;
  setup) cmd_setup ;;
  verify) cmd_verify ;;
  eval) cmd_eval ;;
  report) cmd_report ;;
  router) cmd_router ;;
  refresh) cmd_refresh ;;
  status) cmd_status ;;
  dashboard) cmd_dashboard ;;
  cleanup) cmd_cleanup ;;
  help|-h|--help) usage ;;
  *) usage; die "unknown command: ${COMMAND}" ;;
esac
