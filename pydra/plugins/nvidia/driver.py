import asyncio
import json
import logging
import os
import sys
import glob

from pydra.core.server import DraNodeServer

class NvidiaDraPlugin(DraNodeServer):
    def __init__(self, socket_path: str, kubelet_socket_path: str = None, registration_socket_path: str = None, cdi_dir: str = None):
        plugin_name = "nvidia.com/gpu"
        super().__init__(plugin_name=plugin_name, socket_path=socket_path, kubelet_socket_path=kubelet_socket_path, registration_socket_path=registration_socket_path)
        self.cdi_dir = cdi_dir or "/var/run/cdi"
        self.logger.info(f"Initialized NvidiaDraPlugin. cdi_dir: {self.cdi_dir}")

    def get_devices(self) -> list:
        devices = []
        try:
            import pynvml
            pynvml.nvmlInit()
            device_count = pynvml.nvmlDeviceGetCount()
            for i in range(device_count):
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                name = pynvml.nvmlDeviceGetName(handle)
                mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                
                devices.append({
                    "name": str(i),
                    "attributes": {
                        "nvidia.com/gpu-model": name if isinstance(name, str) else name.decode('utf-8'),
                        "nvidia.com/gpu-memory": f"{mem_info.total // (1024 * 1024)}MiB",
                    }
                })
            pynvml.nvmlShutdown()
        except ImportError:
            self.logger.warning("pynvml not installed, falling back to basic glob")
            for dev_path in glob.glob("/dev/nvidia[0-9]*"):
                dev_name = dev_path.replace("/dev/nvidia", "")
                devices.append({
                    "name": dev_name,
                    "attributes": {
                        "nvidia.com/gpu-model": "unknown",
                        "nvidia.com/gpu-memory": "unknown",
                    }
                })
        except Exception as e:
            self.logger.warning(f"Failed to query NVML: {e}")

        return devices

    async def prepare_hardware(self, claim_uid: str, namespace: str, name: str) -> list[str]:
        self.logger.info(f"prepare_hardware: claim_uid={claim_uid}, namespace={namespace}, name={name}")

        device_id = None
        # Try to read the allocated device from the API Server
        if self.k8s_api:
            from kubernetes import client
            try:
                api = client.CustomObjectsApi()
                claim = api.get_namespaced_custom_object(
                    group="resource.k8s.io",
                    version="v1",
                    namespace=namespace,
                    plural="resourceclaims",
                    name=name,
                )

                results = claim.get("status", {}).get("allocation", {}).get("devices", {}).get("results", [])
                for result in results:
                    if result.get("pool") == self.plugin_name:
                        device_id = result.get("device")
                        break
            except Exception as e:
                self.logger.warning(f"Failed to fetch claim {namespace}/{name} using CustomObjectsApi: {e}")

        if not device_id:
            devices = self.get_devices()
            if devices:
                device_id = devices[0]["name"]
                self.logger.warning(f"Fallback to device_id = {device_id}")
            else:
                raise RuntimeError("No NVIDIA devices available to allocate")

        cdi_device_str = f"nvidia.com/gpu/device={device_id}"

        # Generate CDI v1.1.0 specification
        cdi_spec = {
            "cdiVersion": "1.1.0",
            "kind": "nvidia.com/gpu",
            "devices": [
                {
                    "name": str(device_id),
                    "containerEdits": {
                        "deviceNodes": [
                            {
                                "path": f"/dev/nvidia{device_id}",
                                "hostPath": f"/dev/nvidia{device_id}",
                                "type": "c"
                            },
                            {
                                "path": "/dev/nvidiactl",
                                "hostPath": "/dev/nvidiactl",
                                "type": "c"
                            },
                            {
                                "path": "/dev/nvidia-uvm",
                                "hostPath": "/dev/nvidia-uvm",
                                "type": "c"
                            }
                        ]
                    }
                }
            ]
        }

        # Ensure CDI directory exists
        os.makedirs(self.cdi_dir, exist_ok=True)

        # Write CDI JSON file
        cdi_file_path = os.path.join(self.cdi_dir, f"nvidia.com_{claim_uid}.json")
        self.logger.info(f"Writing CDI JSON spec to {cdi_file_path}")
        with open(cdi_file_path, "w") as f:
            json.dump(cdi_spec, f, indent=2)

        return [cdi_device_str]

    async def unprepare_hardware(self, claim_uid: str, namespace: str, name: str):
        self.logger.info(f"unprepare_hardware: claim_uid={claim_uid}, namespace={namespace}, name={name}")

        cdi_file_path = os.path.join(self.cdi_dir, f"nvidia.com_{claim_uid}.json")
        if os.path.exists(cdi_file_path):
            self.logger.info(f"Removing CDI JSON spec file {cdi_file_path}")
            os.remove(cdi_file_path)
        else:
            self.logger.warning(f"CDI JSON spec file {cdi_file_path} not found during unprepare")

async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    socket_path = os.environ.get("SOCKET_PATH", "/var/lib/kubelet/plugins/nvidia.com-gpu/plugin.sock")
    kubelet_socket_path = os.environ.get("KUBELET_SOCKET_PATH", socket_path)
    registration_socket_path = os.environ.get("REGISTRATION_SOCKET_PATH", None)
    cdi_dir = os.environ.get("CDI_DIR", "/var/run/cdi")

    plugin = NvidiaDraPlugin(
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
