import os
import subprocess
import pytest

CLUSTER_NAME = "pydra-test"
HOST_DRA_DIR = "/tmp/k8s-dra"

def cleanup_host_dir():
    print("Cleaning up host directory via Docker...")
    if os.path.exists(HOST_DRA_DIR):
        subprocess.run(["docker", "run", "--rm", "-v", "/tmp:/tmp", "alpine", "chmod", "-R", "777", HOST_DRA_DIR], check=False)
        subprocess.run(["docker", "run", "--rm", "-v", f"{HOST_DRA_DIR}:/k8s-dra", "alpine", "sh", "-c", "rm -rf /k8s-dra/*"], check=False)

@pytest.fixture(scope="session", autouse=True)
def global_cluster_setup():
    print("Building Docker image...")
    subprocess.run(["make", "build-images", "REGISTRY=pydra", "TAG=test"], check=True)

    print("Recreating Kind cluster...")
    subprocess.run(["kind", "delete", "cluster", "--name", CLUSTER_NAME], check=False)
    subprocess.run(["kind", "create", "cluster", "--name", CLUSTER_NAME, "--config", "tests/kind-dra.yaml"], check=True)

    print("Loading image into Kind...")
    subprocess.run(["kind", "load", "docker-image", "pydra/network:test", "--name", CLUSTER_NAME], check=True)
    subprocess.run(["kind", "load", "docker-image", "pydra/tpu:test", "--name", CLUSTER_NAME], check=True)
    print("Pulling and loading ubuntu:24.04 image into Kind to speed up tests...")
    subprocess.run(["docker", "pull", "ubuntu:24.04"], check=True)
    subprocess.run(["kind", "load", "docker-image", "ubuntu:24.04", "--name", CLUSTER_NAME], check=True)

    control_plane_container = f"{CLUSTER_NAME}-control-plane"

    # Setup TPU mock hardware
    subprocess.run(["docker", "exec", control_plane_container, "mkdir", "-p", "/usr/lib"], check=True)
    subprocess.run(["docker", "exec", control_plane_container, "touch", "/usr/lib/libtpu.so"], check=True)
    check_dev = subprocess.run(["docker", "exec", control_plane_container, "test", "-c", "/dev/accel0"], check=False)
    if check_dev.returncode != 0:
        subprocess.run(["docker", "exec", control_plane_container, "mknod", "-m", "666", "/dev/accel0", "c", "1", "3"], check=True)

    # Setup Network mock hardware
    subprocess.run(["docker", "exec", control_plane_container, "ip", "link", "add", "dummy0", "type", "dummy"], check=False)
    subprocess.run(["docker", "exec", control_plane_container, "ip", "link", "add", "dummy1", "type", "dummy"], check=True)
    subprocess.run(["docker", "exec", control_plane_container, "ip", "link", "set", "up", "dev", "dummy0"], check=False)
    subprocess.run(["docker", "exec", control_plane_container, "ip", "addr", "add", "169.254.169.13/32", "dev", "dummy0"], check=False)

    # Untaint control plane nodes
    subprocess.run(["kubectl", "taint", "nodes", "--all", "node-role.kubernetes.io/control-plane-"], check=False)
    subprocess.run(["kubectl", "taint", "nodes", "--all", "node-role.kubernetes.io/master-"], check=False)

    # Apply DeviceClasses and Driver RBAC + DaemonSet
    device_classes = """apiVersion: resource.k8s.io/v1
kind: DeviceClass
metadata:
  name: dra.net
spec: {}
---
apiVersion: resource.k8s.io/v1
kind: DeviceClass
metadata:
  name: tpu.google.com
spec: {}
"""
    subprocess.run(["kubectl", "apply", "-f", "-"], input=device_classes.encode('utf-8'), check=True)
    subprocess.run(["kubectl", "apply", "-f", "tests/driver-rbac.yaml"], check=True)
    subprocess.run(["kubectl", "apply", "-f", "tests/network-driver.yaml"], check=True)
    subprocess.run(["kubectl", "apply", "-f", "tests/tpu-driver.yaml"], check=True)

    # Wait for daemonsets to be ready
    print("Waiting for DaemonSets to become ready...")
    subprocess.run(["kubectl", "wait", "--for=condition=ready", "pod", "-l", "app=pydra-network-plugin", "-n", "kube-system", "--timeout=60s"], check=True)
    subprocess.run(["kubectl", "wait", "--for=condition=ready", "pod", "-l", "app=pydra-tpu-plugin", "-n", "kube-system", "--timeout=60s"], check=True)

    yield

    print("Cleaning up cluster...")
    subprocess.run(["kind", "delete", "cluster", "--name", CLUSTER_NAME], check=False)

@pytest.fixture
def test_namespace():
    import uuid
    import time
    from kubernetes import client, config
    from kubernetes.client.rest import ApiException
    config.load_kube_config()
    core_api = client.CoreV1Api()

    ns_name = f"test-ns-{uuid.uuid4().hex[:8]}"
    ns = client.V1Namespace(metadata=client.V1ObjectMeta(name=ns_name))
    core_api.create_namespace(ns)

    # Wait for default service account
    for _ in range(30):
        try:
            core_api.read_namespaced_service_account("default", ns_name)
            break
        except Exception:
            time.sleep(1)

    yield ns_name

    core_api.delete_namespace(ns_name)

    # Wait for namespace to be completely deleted to free up DRA allocations
    for _ in range(60):
        try:
            core_api.read_namespace(ns_name)
            time.sleep(2)
        except ApiException as e:
            if e.status == 404:
                break
            time.sleep(2)

