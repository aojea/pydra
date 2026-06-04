import os
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
