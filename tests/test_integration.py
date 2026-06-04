import os
import subprocess
import time
import json
import pytest
from kubernetes import client, config

CLUSTER_NAME = "pydra-test"
HOST_DRA_DIR = "/tmp/k8s-dra"

def cleanup_host_dir():
    print("Cleaning up host directory via Docker...")
    if os.path.exists(HOST_DRA_DIR):
        subprocess.run(["docker", "run", "--rm", "-v", "/tmp:/tmp", "alpine", "chmod", "-R", "777", HOST_DRA_DIR], check=False)
        subprocess.run(["docker", "run", "--rm", "-v", f"{HOST_DRA_DIR}:/k8s-dra", "alpine", "sh", "-c", "rm -rf /k8s-dra/*"], check=False)

@pytest.fixture(scope="module", autouse=True)
def cluster_lifecycle():
    # 1. Setup host directories
    print("Setting up host directories...")
    cleanup_host_dir()
    os.makedirs(f"{HOST_DRA_DIR}/plugins_registry", exist_ok=True)
    os.makedirs(f"{HOST_DRA_DIR}/plugins/tpu.google.com", exist_ok=True)
    os.makedirs(f"{HOST_DRA_DIR}/cdi", exist_ok=True)
    subprocess.run(["docker", "run", "--rm", "-v", "/tmp:/tmp", "alpine", "chmod", "-R", "777", HOST_DRA_DIR], check=False)

    # 2. Recreate Kind cluster
    print("Recreating Kind cluster...")
    subprocess.run(["kind", "delete", "cluster", "--name", CLUSTER_NAME], check=False)
    subprocess.run(["kind", "create", "cluster", "--name", CLUSTER_NAME, "--config", "tests/kind-dra.yaml"], check=True)

    # 3. Setup mock hardware inside control-plane container
    print("Setting up mock hardware inside the control-plane container...")
    control_plane_container = f"{CLUSTER_NAME}-control-plane"
    subprocess.run(["docker", "exec", control_plane_container, "mkdir", "-p", "/usr/lib"], check=True)
    subprocess.run(["docker", "exec", control_plane_container, "touch", "/usr/lib/libtpu.so"], check=True)
    
    # Check if character device already exists; if not, create it
    check_dev = subprocess.run(["docker", "exec", control_plane_container, "test", "-c", "/dev/accel0"], check=False)
    if check_dev.returncode != 0:
        subprocess.run(["docker", "exec", control_plane_container, "mknod", "-m", "666", "/dev/accel0", "c", "1", "3"], check=True)

    # Untaint control plane nodes to ensure user pods can be scheduled
    subprocess.run(["kubectl", "taint", "nodes", "--all", "node-role.kubernetes.io/control-plane-"], check=False)
    subprocess.run(["kubectl", "taint", "nodes", "--all", "node-role.kubernetes.io/master-"], check=False)

    yield

    # Cleanup cluster and directories
    print("Cleaning up cluster...")
    subprocess.run(["kind", "delete", "cluster", "--name", CLUSTER_NAME], check=False)
    cleanup_host_dir()

@pytest.fixture(scope="module")
def driver_process():
    print("Starting Python DRA driver...")
    env = os.environ.copy()
    env.update({
        "SOCKET_PATH": f"{HOST_DRA_DIR}/plugins/tpu.google.com/plugin.sock",
        "KUBELET_SOCKET_PATH": "/var/lib/kubelet/plugins/tpu.google.com/plugin.sock",
        "REGISTRATION_SOCKET_PATH": f"{HOST_DRA_DIR}/plugins_registry/tpu.google.com-reg.sock",
        "CDI_DIR": f"{HOST_DRA_DIR}/cdi",
        "NODE_NAME": f"{CLUSTER_NAME}-control-plane",
        "PYTHONPATH": "."
    })

    proc = subprocess.Popen([
        ".venv/bin/python3", "-m", "pydra.plugins.tpu.driver"
    ], env=env)

    # Allow startup time
    time.sleep(3)
    yield proc

    print("Killing driver process...")
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

def test_end_to_end_tpu_allocation(driver_process):
    # Load kubernetes client config
    config.load_kube_config()
    
    # Wait for the default service account to exist
    print("Waiting for default service account...")
    core_api = client.CoreV1Api()
    for _ in range(30):
        try:
            core_api.read_namespaced_service_account("default", "default")
            break
        except Exception:
            time.sleep(1)
    else:
        pytest.fail("Default service account was not created in time")

    # Apply the manifests
    print("Applying manifests...")
    subprocess.run(["kubectl", "apply", "-f", "tests/test-claim.yaml"], check=True)

    # Wait for the pod to become Ready / Running
    print("Waiting for test-pod to reach Running state...")
    pod_running = False
    for _ in range(60):
        try:
            pod = core_api.read_namespaced_pod("test-pod", "default")
            if pod.status.phase == "Running":
                # Check if Ready condition is True
                ready_condition = next((c for c in pod.status.conditions if c.type == "Ready"), None)
                if ready_condition and ready_condition.status == "True":
                    pod_running = True
                    break
        except Exception:
            pass
        time.sleep(2)
    else:
        # Collect diagnostics on failure
        print("\n=== DIAGNOSTICS ===")
        subprocess.run(["kubectl", "get", "pods,resourceclaims,deviceclasses,resourceslices", "-A"], check=False)
        subprocess.run(["kubectl", "describe", "pod", "test-pod"], check=False)
        subprocess.run(["kubectl", "describe", "resourceclaim", "tpu-claim"], check=False)
        print("===================\n")
        pytest.fail("test-pod did not reach Running/Ready status")

    # Validate that the CDI JSON file was written
    cdi_dir_contents = os.listdir(f"{HOST_DRA_DIR}/cdi")
    cdi_files = [f for f in cdi_dir_contents if f.startswith("tpu.google.com_") and f.endswith(".json")]
    assert len(cdi_files) == 1, f"Expected 1 CDI file, found {len(cdi_files)}: {cdi_dir_contents}"

    cdi_file_path = os.path.join(f"{HOST_DRA_DIR}/cdi", cdi_files[0])
    with open(cdi_file_path, "r") as f:
        cdi_data = json.load(f)

    # Validate CDI structure
    assert cdi_data["cdiVersion"] == "0.5.0"
    assert cdi_data["kind"] == "tpu.google.com/device"
    assert len(cdi_data["devices"]) == 1
    assert cdi_data["devices"][0]["name"] == "0"
    
    device_nodes = cdi_data["devices"][0]["containerEdits"]["deviceNodes"]
    assert device_nodes[0]["path"] == "/dev/accel0"
    assert device_nodes[0]["hostPath"] == "/dev/accel0"

    mounts = cdi_data["devices"][0]["containerEdits"]["mounts"]
    assert mounts[0]["hostPath"] == "/usr/lib/libtpu.so"
    assert mounts[0]["containerPath"] == "/usr/lib/libtpu.so"

    print("SUCCESS: Integration test completed successfully!")
