import asyncio
import json
import logging
import os
import sys

from pydra.core.server import DraNodeServer

class TpuDraPlugin(DraNodeServer):
    def __init__(self, socket_path: str, kubelet_socket_path: str = None, registration_socket_path: str = None, cdi_dir: str = None):
        plugin_name = "tpu.google.com"
        super().__init__(plugin_name=plugin_name, socket_path=socket_path, kubelet_socket_path=kubelet_socket_path, registration_socket_path=registration_socket_path)
        self.cdi_dir = cdi_dir or "/var/run/cdi"
        self.logger.info(f"Initialized TpuDraPlugin. cdi_dir: {self.cdi_dir}")

    def get_devices(self) -> list[str]:
        return ["0"]

    async def prepare_hardware(self, claim_uid: str, namespace: str, name: str) -> list[str]:
        self.logger.info(f"prepare_hardware: claim_uid={claim_uid}, namespace={namespace}, name={name}")

        # Simulate hardware allocation by assigning device_id = "0"
        device_id = "0"
        cdi_device_str = f"tpu.google.com/device={device_id}"

        # Generate CDI v0.5.0 specification
        cdi_spec = {
            "cdiVersion": "1.1.0",
            "kind": "tpu.google.com/device",
            "devices": [
                {
                    "name": device_id,
                    "containerEdits": {
                        "deviceNodes": [
                            {
                                "path": "/dev/accel0",
                                "hostPath": "/dev/accel0",
                                "type": "c"
                            }
                        ],
                        "mounts": [
                            {
                                "hostPath": "/usr/lib/libtpu.so",
                                "containerPath": "/usr/lib/libtpu.so",
                                "options": ["ro", "nosuid", "nodev", "bind"]
                            }
                        ]
                    }
                }
            ]
        }

        # Ensure CDI directory exists
        os.makedirs(self.cdi_dir, exist_ok=True)

        # Write CDI JSON file
        cdi_file_path = os.path.join(self.cdi_dir, f"tpu.google.com_{claim_uid}.json")
        self.logger.info(f"Writing CDI JSON spec to {cdi_file_path}")
        with open(cdi_file_path, "w") as f:
            json.dump(cdi_spec, f, indent=2)

        return [cdi_device_str]

    async def unprepare_hardware(self, claim_uid: str, namespace: str, name: str):
        self.logger.info(f"unprepare_hardware: claim_uid={claim_uid}, namespace={namespace}, name={name}")

        cdi_file_path = os.path.join(self.cdi_dir, f"tpu.google.com_{claim_uid}.json")
        if os.path.exists(cdi_file_path):
            self.logger.info(f"Removing CDI JSON spec file {cdi_file_path}")
            os.remove(cdi_file_path)
        else:
            self.logger.warning(f"CDI JSON spec file {cdi_file_path} not found during unprepare")

async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # Read environment overrides or use defaults
    socket_path = os.environ.get("SOCKET_PATH", "/var/lib/kubelet/plugins/tpu.google.com/plugin.sock")
    kubelet_socket_path = os.environ.get("KUBELET_SOCKET_PATH", socket_path)
    registration_socket_path = os.environ.get("REGISTRATION_SOCKET_PATH", None)
    cdi_dir = os.environ.get("CDI_DIR", "/var/run/cdi")

    plugin = TpuDraPlugin(
        socket_path=socket_path,
        kubelet_socket_path=kubelet_socket_path,
        registration_socket_path=registration_socket_path,
        cdi_dir=cdi_dir
    )
    try:
        await plugin.serve()
    except Exception:
        logging.getLogger("main").exception("Plugin server crashed")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
