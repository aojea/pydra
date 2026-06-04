# # pydra (Python Dynamic Resource Allocation)

`pydra` is an experimental Python-based Kubernetes Dynamic Resource Allocation (DRA) driver designed to orchestrate and expose Tensor Processing Units (TPUs).

## Why Python for TPU DRA?

Traditional Kubernetes resource drivers are written in Go. However, the Cloud TPU runtime ecosystem relies on `libtpu.so`, which exposes public Python bindings. 

By implementing a DRA driver in Python, we can:
- **Expose Hardware Telemetry:** Access and publish critical internal TPU details, particularly multihost and multi-slice topologies.
- **Simplify Integration:** Avoid complex Go-to-C wrapper maintenance by utilizing Python bindings natively.
- **Accelerate Adoption:** Seamlessly bridge the gap between user notebooks and underlying hardware:
  `Notebooks (Users) ➔ DRA ➔ Python Bindings ➔ libtpu.so ➔ TPUs`

## Features (Work in Progress)

- **Direct TPU Bindings:** Integration with `libtpu.so`'s public API to manage accelerator states.
- **Multihost Topology Handling:** Python-based management of multi-node TPU configurations.
- **Kubernetes DRA Compatibility:** Implements standard ResourceClaim and ResourceSlice lifecycles.
