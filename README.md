# pydra

`pydra` is a high-performance, resilient Python framework for building Kubernetes Dynamic Resource Allocation (DRA) hardware drivers.

By handling the intricate, low-level Kubernetes gRPC node plumbing natively in Python, `pydra` eliminates the need for hardware vendors to maintain complex Go codebases or fragile Cgo wrappers just to expose their chips to the cluster control plane.

## Overview

Traditional Kubernetes device plugins require Go. However, the AI hardware ecosystem—encompassing PJRT, OpenXLA, PyTorch, JAX, and vendor monitoring tools—is natively Python-centric. `pydra` bridges this gap, allowing infrastructure engineers to write production-grade, topology-aware scheduling drivers utilizing the exact same Python SDKs running the AI workloads.

## Architecture: Microkernel Design

`pydra` enforces a strict separation between Kubernetes protocol mechanics and raw silicon management.

```
[ Kubernetes Kubelet ]
             |
             | (gRPC over Unix Domain Socket)
             v
+-------------------------------------------------------+
|               pydra-core (The Library)                |
|                                                       |
|  - UDS gRPC Server Engine    - Unix Signal Handling   |
|  - Kubelet Plugin Registry   - Retries & Backoffs     |
|  - CDI Spec Validator        - Robust Error Boundary  |
+-------------------------------------------------------+
            |
            | (Python Abstract Base Class / Inheritance)
            v
+-------------------------------------------------------+
|            Hardware Drivers (Independent)             |
|                                                       |
|   pydra-tpu          pydra-nvidia         pydra-amd   |
|  (Imports JAX/SDK)  (Imports NVML)      (Imports SMI) |
+-------------------------------------------------------+
```

### 1. `pydra-core`

The engine of the framework. It operates completely agnostic of specific hardware types.

* **Resilient UDS Server:** Manages connection lifecycles, socket cleanups on termination, and maps incoming Kubelet DRA requests into structured Python primitives.
* **Exception Shielding:** If a hardware vendor's underlying C-library throws a segmentation fault or an unhandled exception during allocation, `pydra-core` catches it, emits a high-fidelity diagnostic trace, and reports a clear `TerminalError` back to the Kubelet to prevent hung pods.
* **CDI Generator:** Provides a fluid API to assemble and validate Container Device Interface (CDI) v1.1.0 specs before writing them to the node.

### 2. `pydra-plugins`

Lean, independent packages that inherit from the core.

* **Deep Telemetry:** Queries the physical hardware directly via native SDKs (`libtpu.sdk`, `pynvml`, etc.) to expose HBM memory capacity, link errors, and real-time topology layout back to the scheduler via `ResourceSlices`.
* **Custom Slicing Logic:** Translates generic user scheduling requests into exact hardware configurations (e.g., configuring an NVIDIA MIG profile or partitioning a TPU v5e mesh topology).
