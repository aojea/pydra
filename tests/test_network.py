import subprocess
import time
import json
import pytest
from kubernetes import client, config

CLUSTER_NAME = "pydra-test"
HOST_DRA_DIR = "/tmp/k8s-dra"


def test_end_to_end_network_allocation(test_namespace):
    config.load_kube_config()
    core_api = client.CoreV1Api()

    # Apply network test manifests
    subprocess.run(["kubectl", "apply", "-n", test_namespace, "-f", "tests/test-network.yaml"], check=True)

    # Wait for the pod to become Ready / Running
    pod_running = False
    for _ in range(60):
        try:
            pod = core_api.read_namespaced_pod("pod-network-test", test_namespace)
            if pod.status.phase == "Running":
                pod_running = True
                break
        except Exception:
            pass
        time.sleep(2)

    if not pod_running:
        subprocess.run(["kubectl", "get", "pods,resourceclaims,deviceclasses,resourceslices", "-A"], check=False)
        subprocess.run(["kubectl", "describe", "pod", "pod-network-test", "-n", test_namespace], check=False)
        subprocess.run(["kubectl", "describe", "resourceclaim", "dummy-interface-static-ip", "-n", test_namespace], check=False)
        pytest.fail("pod-network-test did not reach Running status")

    # Check CDI file
    claim_api = client.CustomObjectsApi()
    claim = claim_api.get_namespaced_custom_object("resource.k8s.io", "v1", test_namespace, "resourceclaims", "dummy-interface-static-ip")
    claim_uid = claim["metadata"]["uid"]
    expected_file = f"network.pydra.io_{claim_uid}.json"

    cdi_cat = subprocess.run(["docker", "exec", f"{CLUSTER_NAME}-control-plane", "cat", f"/var/run/cdi/{expected_file}"], capture_output=True, text=True, check=True)
    cdi_data = json.loads(cdi_cat.stdout)

    assert cdi_data["cdiVersion"] == "1.1.0"
    assert cdi_data["kind"] == "network.pydra.io/device"
    assert len(cdi_data["devices"]) == 1
    device_name = cdi_data["devices"][0]["name"]
    net_devices = cdi_data["devices"][0]["containerEdits"]["netDevices"]
    assert net_devices[0]["hostInterfaceName"] == device_name
    assert net_devices[0]["name"] == device_name

    # Exec into the pod and verify the interface is present
    exec_result = subprocess.run(
        ["kubectl", "exec", "-n", test_namespace, "pod-network-test", "--", "ip", "a"],
        capture_output=True, text=True, check=True
    )
    assert "dummy0" in exec_result.stdout, f"Expected dummy0 interface in pod, but got:\n{exec_result.stdout}"
    print("SUCCESS: Network interface injected correctly!")

def test_kubelet_restart_survivability(test_namespace):
    """
    Validates that a running pod with a DRA network allocation survives a Kubelet restart.
    This simulates the scenario discussed in kubernetes/kubernetes#137919 where pods
    could be evicted if Kubelet restarts and loses state before the plugin reconnects.
    """
    config.load_kube_config()
    core_api = client.CoreV1Api()

    # Apply network test manifests
    subprocess.run(["kubectl", "apply", "-n", test_namespace, "-f", "tests/test-network-restart.yaml"], check=True)

    # Immediately restart kubelet to simulate the "Fresh-pod-during-restart race"
    # defined in K8s PR 137919. The pod has been admitted but container creation
    # is likely still in progress or hasn't started.
    print("Restarting kubelet on the node to simulate race condition...")
    subprocess.run(["docker", "exec", f"{CLUSTER_NAME}-control-plane", "systemctl", "restart", "kubelet"], check=True)

    # Wait for kubelet to be back and node to be Ready
    print("Waiting for node to become Ready after kubelet restart...")
    node_ready = False
    for _ in range(30):
        try:
            node = core_api.read_node(f"{CLUSTER_NAME}-control-plane")
            for condition in node.status.conditions:
                if condition.type == "Ready" and condition.status == "True":
                    node_ready = True
                    break
            if node_ready:
                break
        except Exception:
            pass
        time.sleep(2)

    assert node_ready, "Node did not become Ready after kubelet restart"

    # Wait for the pod to become Ready / Running after Kubelet recovers
    print("Waiting for pod to become Running...")
    pod_running = False
    for _ in range(60):
        try:
            pod = core_api.read_namespaced_pod("pod-network-restart-test", test_namespace)
            if pod.status.phase == "Running":
                pod_running = True
                break
        except Exception:
            pass
        time.sleep(2)

    if not pod_running:
        print("Pod failed to start. Dumping pod description:")
        subprocess.run(["kubectl", "describe", "pod", "pod-network-restart-test", "-n", test_namespace])
        pytest.fail("pod-network-restart-test did not reach Running status after kubelet restart")

    # Exec into the pod and verify the interface is still present
    # Retry a few times because kubelet might take some time to sync pod workers after restart
    for _ in range(15):
        exec_result = subprocess.run(
            ["kubectl", "exec", "-n", test_namespace, "pod-network-restart-test", "--", "ip", "a"],
            capture_output=True, text=True, check=False
        )
        if exec_result.returncode == 0 and "dummy1" in exec_result.stdout:
            break
        time.sleep(2)

    if exec_result.returncode != 0:
        print("Exec failed. Dumping pod description:")
        subprocess.run(["kubectl", "describe", "pod", "pod-network-restart-test", "-n", test_namespace])
        print("Dumping kubelet logs:")
        subprocess.run(["docker", "exec", f"{CLUSTER_NAME}-control-plane", "journalctl", "-u", "kubelet", "--no-pager", "-n", "100"])

    assert exec_result.returncode == 0, f"Failed to exec into pod: {exec_result.stderr}"
    assert "dummy1" in exec_result.stdout, f"Expected dummy1 interface in pod to persist, but got:\n{exec_result.stdout}"
    print("SUCCESS: Pod and network interface survived kubelet restart!")
