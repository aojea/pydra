"""
pydra/plugins/tpu/driver.py

TPU (Google Tensor Processing Unit) DRA plugin implementation.

This is a **mock** driver intended as a Proof of Concept.  It does not
interact with real hardware; instead it:

* Generates a CDI (Container Device Interface) JSON spec per ResourceClaim,
  injecting ``/dev/accel0`` and mounting a stub ``/usr/lib/libtpu.so``.
* Returns the CDI device ID ``tpu.google.com/device=0`` to kubelet.

Environment variables (override defaults for local/kind testing):
  PLUGIN_SOCKET_PATH        — host path the Python process binds to
                              (default: /var/lib/kubelet/plugins/tpu.google.com/plugin.sock)
  PLUGIN_ENDPOINT           — path kubelet sees inside the container
                              (default: same as PLUGIN_SOCKET_PATH)
  REGISTRATION_SOCKET_PATH  — host path for the registration socket
                              (default: /var/lib/kubelet/plugins_registry/tpu.google.com.sock)
  CDI_DIR                   — directory where CDI JSON specs are written
                              (default: /var/run/cdi)

Run directly:
  python -m pydra.plugins.tpu.driver
"""

import asyncio
import json
import logging
import os
import signal
import sys
from typing import List

from pydra.core.server import DraNodeServer

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
_DEFAULT_PLUGIN_SOCKET      = "/var/lib/kubelet/plugins/tpu.google.com/plugin.sock"
_DEFAULT_REG_SOCKET         = "/var/lib/kubelet/plugins_registry/tpu.google.com.sock"
_DEFAULT_CDI_DIR            = "/var/run/cdi"

_PLUGIN_NAME                = "tpu.google.com"
_CDI_KIND                   = "tpu.google.com/device"
_DEVICE_NAME                = "0"           # Mock: always device 0
_DEV_NODE_PATH              = "/dev/accel0"
_DEV_NODE_TYPE              = "c"           # character device
_DEV_NODE_MAJOR             = 243          # Typical accel major on Linux
_DEV_NODE_MINOR             = 0
_LIBTPU_HOST_PATH           = "/usr/lib/libtpu.so"
_LIBTPU_CONTAINER_PATH      = "/usr/lib/libtpu.so"
_CDI_VERSION                = "0.5.0"


def _build_cdi_spec(claim_uid: str) -> dict:
    """Return a CDI v0.5.0 spec dictionary for a TPU device allocation."""
    return {
        "cdiVersion": _CDI_VERSION,
        "kind": _CDI_KIND,
        "devices": [
            {
                "name": _DEVICE_NAME,
                "containerEdits": {
                    "deviceNodes": [
                        {
                            "path": _DEV_NODE_PATH,
                            "type": _DEV_NODE_TYPE,
                            "major": _DEV_NODE_MAJOR,
                            "minor": _DEV_NODE_MINOR,
                            "permissions": "rw",
                        }
                    ],
                    "mounts": [
                        {
                            "hostPath": _LIBTPU_HOST_PATH,
                            "containerPath": _LIBTPU_CONTAINER_PATH,
                            "options": ["ro", "nosuid", "nodev"],
                        }
                    ],
                },
            }
        ],
    }


class TpuDraPlugin(DraNodeServer):
    """Mock DRA plugin for Google TPU hardware."""

    def __init__(self) -> None:
        plugin_socket = os.environ.get("PLUGIN_SOCKET_PATH", _DEFAULT_PLUGIN_SOCKET)
        reg_socket    = os.environ.get("REGISTRATION_SOCKET_PATH", _DEFAULT_REG_SOCKET)
        # The endpoint is what the kubelet dials — may differ from the host bind
        # path when running with kind bind-mounts.
        endpoint      = os.environ.get("PLUGIN_ENDPOINT", plugin_socket)
        self._cdi_dir = os.environ.get("CDI_DIR", _DEFAULT_CDI_DIR)

        super().__init__(
            plugin_name=_PLUGIN_NAME,
            plugin_socket_path=plugin_socket,
            registration_socket_path=reg_socket,
            plugin_endpoint=endpoint,
        )
        logger.info(
            "TpuDraPlugin initialised — socket=%s reg=%s endpoint=%s cdi_dir=%s",
            plugin_socket,
            reg_socket,
            endpoint,
            self._cdi_dir,
        )

    # ------------------------------------------------------------------
    # Hardware allocation
    # ------------------------------------------------------------------

    def prepare_hardware(
        self,
        claim_uid: str,
        namespace: str,
        name: str,
    ) -> List[str]:
        """
        Simulate TPU allocation and write a CDI spec for the claim.

        Steps:
        1. Assign mock device ID ``0``.
        2. Build a CDI v0.5.0 JSON spec.
        3. Write the spec to ``{cdi_dir}/tpu.google.com_{claim_uid}.json``.
        4. Return the CDI device ID string ``tpu.google.com/device=0``.
        """
        device_id = _DEVICE_NAME
        logger.info(
            "Allocating TPU device '%s' for claim %s (%s/%s)",
            device_id,
            claim_uid,
            namespace,
            name,
        )

        # Build CDI spec
        cdi_spec = _build_cdi_spec(claim_uid)

        # Ensure CDI directory exists
        os.makedirs(self._cdi_dir, exist_ok=True)

        cdi_file = os.path.join(
            self._cdi_dir, f"tpu.google.com_{claim_uid}.json"
        )
        try:
            with open(cdi_file, "w", encoding="utf-8") as fh:
                json.dump(cdi_spec, fh, indent=2)
            logger.info("CDI spec written to %s", cdi_file)
        except OSError as exc:
            raise RuntimeError(
                f"Failed to write CDI spec to {cdi_file}: {exc}"
            ) from exc

        cdi_device_id = f"{_CDI_KIND}={device_id}"
        return [cdi_device_id]

    def unprepare_hardware(
        self,
        claim_uid: str,
        namespace: str,
        name: str,
    ) -> None:
        """
        Release TPU allocation and remove the CDI spec for the claim.
        """
        logger.info(
            "Releasing TPU device for claim %s (%s/%s)",
            claim_uid,
            namespace,
            name,
        )
        cdi_file = os.path.join(
            self._cdi_dir, f"tpu.google.com_{claim_uid}.json"
        )
        if os.path.exists(cdi_file):
            try:
                os.remove(cdi_file)
                logger.info("Removed CDI spec: %s", cdi_file)
            except OSError as exc:
                logger.warning("Could not remove CDI spec %s: %s", cdi_file, exc)
        else:
            logger.warning("CDI spec not found (already removed?): %s", cdi_file)


# ---------------------------------------------------------------------------
# Entry-point
# ---------------------------------------------------------------------------

async def _main() -> None:
    plugin = TpuDraPlugin()

    loop = asyncio.get_event_loop()

    # Graceful shutdown on SIGTERM / SIGINT
    def _handle_signal(sig: signal.Signals) -> None:
        logger.info("Received signal %s — initiating graceful shutdown.", sig.name)
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal, sig)

    try:
        await plugin.serve()
    except asyncio.CancelledError:
        logger.info("TPU DRA plugin shut down cleanly.")


if __name__ == "__main__":
    asyncio.run(_main())
