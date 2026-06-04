#!/usr/bin/env bash
# tests/run_integration.sh
#
# End-to-end integration test for the Python DRA driver.
#
# What this script does:
#   1. Creates local host directories shared into the kind cluster.
#   2. Spins up a kind cluster with DRA feature gates enabled.
#   3. Generates the gRPC stubs (make protos).
#   4. Installs Python dependencies.
#   5. Creates a stub /usr/lib/libtpu.so (so the CDI mount does not fail).
#   6. Starts the Python DRA driver as a background process, bound to the
#      shared host directories.
#   7. Applies the test ResourceClaim and Pod.
#   8. Waits for the Pod to become Ready.
#   9. Asserts the CDI JSON spec was written.
#  10. Tears everything down.
#
# Requirements:
#   kind, kubectl, python3 (3.11+), pip, curl, make
#
# Usage:
#   ./tests/run_integration.sh

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CLUSTER_NAME="pydra-test"
HOST_BASE_DIR="/tmp/k8s-dra"
HOST_PLUGINS_REGISTRY="${HOST_BASE_DIR}/plugins_registry"
HOST_PLUGINS="${HOST_BASE_DIR}/plugins"
HOST_CDI="${HOST_BASE_DIR}/cdi"

DRIVER_NAME="tpu.google.com"
HOST_PLUGIN_SOCKET="${HOST_PLUGINS}/${DRIVER_NAME}/plugin.sock"
HOST_REG_SOCKET="${HOST_PLUGINS_REGISTRY}/${DRIVER_NAME}.sock"
# The kubelet inside kind sees these paths via bind-mounts:
CONTAINER_PLUGIN_SOCKET="/var/lib/kubelet/plugins/${DRIVER_NAME}/plugin.sock"
CONTAINER_CDI_DIR="/var/run/cdi"

POD_NAME="tpu-test"
POD_NAMESPACE="default"
POD_TIMEOUT="300s"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

DRIVER_PID=""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()  { echo "[$(date '+%H:%M:%S')] INFO  $*"; }
warn() { echo "[$(date '+%H:%M:%S')] WARN  $*" >&2; }
die()  { echo "[$(date '+%H:%M:%S')] ERROR $*" >&2; exit 1; }

cleanup() {
    log "--- Cleanup ---"
    if [[ -n "${DRIVER_PID}" ]] && kill -0 "${DRIVER_PID}" 2>/dev/null; then
        log "Stopping Python driver (PID ${DRIVER_PID})..."
        kill "${DRIVER_PID}" || true
        wait "${DRIVER_PID}" 2>/dev/null || true
    fi
    if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
        log "Deleting kind cluster '${CLUSTER_NAME}'..."
        kind delete cluster --name "${CLUSTER_NAME}" || true
    fi
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
# Step 1: Create shared host directories
# ---------------------------------------------------------------------------
log "Step 1: Creating host directories..."
mkdir -p "${HOST_PLUGINS_REGISTRY}" \
         "${HOST_PLUGINS}/${DRIVER_NAME}" \
         "${HOST_CDI}"
chmod 777 "${HOST_PLUGINS_REGISTRY}" \
           "${HOST_PLUGINS}/${DRIVER_NAME}" \
           "${HOST_CDI}"

# ---------------------------------------------------------------------------
# Step 2: Create the kind cluster
# ---------------------------------------------------------------------------
log "Step 2: Creating kind cluster '${CLUSTER_NAME}'..."
kind create cluster \
    --name "${CLUSTER_NAME}" \
    --config "${SCRIPT_DIR}/kind-dra.yaml" \
    --wait 120s

export KUBECONFIG
KUBECONFIG="$(kind get kubeconfig --name "${CLUSTER_NAME}" 2>/dev/null || true)"
log "Kubeconfig set for cluster '${CLUSTER_NAME}'."

# ---------------------------------------------------------------------------
# Step 3: Generate gRPC stubs
# ---------------------------------------------------------------------------
log "Step 3: Generating gRPC stubs..."
cd "${REPO_ROOT}"
pip install --quiet -r requirements.txt
make protos

# ---------------------------------------------------------------------------
# Step 4: Create stub /usr/lib/libtpu.so on the host
# ---------------------------------------------------------------------------
log "Step 4: Creating stub /usr/lib/libtpu.so..."
if [[ ! -f /usr/lib/libtpu.so ]]; then
    # Create a tiny ELF stub so the CDI bind-mount does not fail.
    sudo touch /usr/lib/libtpu.so 2>/dev/null \
        || { warn "Cannot write /usr/lib/libtpu.so — CDI mount may fail inside container."; }
fi

# ---------------------------------------------------------------------------
# Step 5: Start the Python DRA driver in the background
# ---------------------------------------------------------------------------
log "Step 5: Starting Python DRA driver..."

export PLUGIN_SOCKET_PATH="${HOST_PLUGIN_SOCKET}"
export REGISTRATION_SOCKET_PATH="${HOST_REG_SOCKET}"
# The endpoint is the path the kubelet inside kind will dial:
export PLUGIN_ENDPOINT="${CONTAINER_PLUGIN_SOCKET}"
export CDI_DIR="${HOST_CDI}"

python3 -m pydra.plugins.tpu.driver &
DRIVER_PID=$!
log "Driver started with PID ${DRIVER_PID}."

# Wait for the sockets to appear (up to 30 s)
for i in $(seq 1 30); do
    if [[ -S "${HOST_PLUGIN_SOCKET}" && -S "${HOST_REG_SOCKET}" ]]; then
        log "Driver sockets are up."
        break
    fi
    sleep 1
done
if [[ ! -S "${HOST_PLUGIN_SOCKET}" ]]; then
    die "Plugin socket did not appear at ${HOST_PLUGIN_SOCKET} within 30 s."
fi

# ---------------------------------------------------------------------------
# Step 6: Apply test manifests
# ---------------------------------------------------------------------------
log "Step 6: Applying test manifests..."
kubectl apply -f "${SCRIPT_DIR}/test-claim.yaml"

# ---------------------------------------------------------------------------
# Step 7: Wait for Pod to become Ready
# ---------------------------------------------------------------------------
log "Step 7: Waiting for pod '${POD_NAME}' to become Running (timeout: ${POD_TIMEOUT})..."
kubectl wait pod "${POD_NAME}" \
    --namespace "${POD_NAMESPACE}" \
    --for=condition=Ready \
    --timeout="${POD_TIMEOUT}" \
    || die "Pod '${POD_NAME}' did not become Ready within ${POD_TIMEOUT}."

log "Pod '${POD_NAME}' is Ready!"

# ---------------------------------------------------------------------------
# Step 8: Assert CDI JSON was written
# ---------------------------------------------------------------------------
log "Step 8: Asserting CDI spec file was created..."
CDI_FILES=("${HOST_CDI}"/tpu.google.com_*.json)
if [[ ${#CDI_FILES[@]} -eq 0 || ! -f "${CDI_FILES[0]}" ]]; then
    die "No CDI spec files found in ${HOST_CDI}. Driver may not have been called."
fi
log "CDI spec files found:"
for f in "${CDI_FILES[@]}"; do
    log "  ${f}"
    python3 -c "import json, sys; d=json.load(open('${f}')); print('  kind:', d.get('kind')); print('  version:', d.get('cdiVersion'))"
done

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
log "=== Integration test PASSED ==="
