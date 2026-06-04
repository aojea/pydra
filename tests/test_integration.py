import subprocess
import time
import json
import pytest
from kubernetes import client, config

CLUSTER_NAME = "pydra-test"
HOST_DRA_DIR = "/tmp/k8s-dra"


def test_end_to_end_tpu_allocation(test_namespace):
    # Load kubernetes client config
    config.load_kube_config()
    core_api = client.CoreV1Api()
    
    # Apply the manifests in the test_namespace
    subprocess.run(["kubectl", "apply", "-n", test_namespace, "-f", "tests/test-claim.yaml"], check=True)

    # Wait for the pod to become Ready / Running
    print("Waiting for test-pod to reach Running state...")
    for _ in range(60):
        try:
            pod = core_api.read_namespaced_pod("test-pod", test_namespace)
            if pod.status.phase == "Running":
                # Check if Ready condition is True
                ready_condition = next((c for c in pod.status.conditions if c.type == "Ready"), None)
                if ready_condition and ready_condition.status == "True":
                    break
        except Exception:
            pass
        time.sleep(2)
    else:
        # Collect diagnostics on failure
        print("\n=== DIAGNOSTICS ===")
        subprocess.run(["kubectl", "get", "pods,resourceclaims,deviceclasses,resourceslices", "-A"], check=False)
        subprocess.run(["kubectl", "describe", "pod", "test-pod", "-n", test_namespace], check=False)
        subprocess.run(["kubectl", "describe", "resourceclaim", "tpu-claim", "-n", test_namespace], check=False)
        print("===================\n")
        pytest.fail("test-pod did not reach Running/Ready status")

    # Validate that the CDI JSON file was written
    cdi_ls = subprocess.run(["docker", "exec", f"{CLUSTER_NAME}-control-plane", "ls", "/var/run/cdi"], capture_output=True, text=True, check=True)
    cdi_dir_contents = cdi_ls.stdout.splitlines()
    cdi_files = [f for f in cdi_dir_contents if f.startswith("tpu.google.com_") and f.endswith(".json")]
    assert len(cdi_files) >= 1, f"Expected CDI file, found: {cdi_dir_contents}"

    # Verify we check the one for our namespace/claim if possible, but since the claim UID is unique
    # we just parse the last one or anyone that matches
    # Let's get the UID of our claim
    claim_api = client.CustomObjectsApi()
    try:
        claim = claim_api.get_namespaced_custom_object("resource.k8s.io", "v1", test_namespace, "resourceclaims", "tpu-claim")
        claim_uid = claim["metadata"]["uid"]
        expected_file = f"tpu.google.com_{claim_uid}.json"
        assert expected_file in cdi_dir_contents, f"Expected {expected_file} in {cdi_dir_contents}"
        cdi_file_name = expected_file
    except Exception:
        # fallback to the first found file
        cdi_file_name = cdi_files[-1]

    cdi_cat = subprocess.run(["docker", "exec", f"{CLUSTER_NAME}-control-plane", "cat", f"/var/run/cdi/{cdi_file_name}"], capture_output=True, text=True, check=True)
    cdi_data = json.loads(cdi_cat.stdout)

    # Validate CDI structure
    assert cdi_data["cdiVersion"] == "1.1.0"
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
