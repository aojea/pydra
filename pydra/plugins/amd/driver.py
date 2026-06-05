import asyncio
import json
import logging
import os
import sys
import glob

from pydra.core.server import DraNodeServer

class AmdDraPlugin(DraNodeServer):
    def __init__(self, socket_path: str, kubelet_socket_path: str = None, registration_socket_path: str = None, cdi_dir: str = None):
        plugin_name = "amd.com/gpu"
        super().__init__(plugin_name=plugin_name, socket_path=socket_path, kubelet_socket_path=kubelet_socket_path, registration_socket_path=registration_socket_path)
        self.cdi_dir = cdi_dir or "/var/run/cdi"
        self.logger.info(f"Initialized AmdDraPlugin. cdi_dir: {self.cdi_dir}")

    def get_devices(self) -> list:
        devices = []
        try:
            import amdsmi
            amdsmi.amdsmi_init()
            device_handles = amdsmi.amdsmi_get_processor_handles()
            for i, handle in enumerate(device_handles):
                try:
                    name = amdsmi.amdsmi_get_gpu_vendor_name(handle)
                except Exception:
                    name = "AMD_GPU"
                devices.append({
                    "name": str(i),
                    "attributes": {
                        "amd.com/gpu-model": name,
                    }
                })
            amdsmi.amdsmi_shut_down()
        except ImportError:
            self.logger.warning("amdsmi not installed, falling back to basic glob")
            for dev_path in glob.glob("/dev/dri/renderD*"):
                dev_name = dev_path.replace("/dev/dri/renderD", "")
                # usually render nodes start at 128, so 128 -> index 0
                try:
                    idx = int(dev_name) - 128
                    if idx < 0:
                        idx = 0
                except ValueError:
                    idx = 0
                devices.append({
                    "name": str(idx),
                    "attributes": {
                        "amd.com/gpu-model": "unknown",
                    }
                })
        except Exception as e:
            self.logger.warning(f"Failed to query SMI: {e}")

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
                raise RuntimeError("No AMD devices available to allocate")

        cdi_device_str = f"amd.com/gpu/device={device_id}"

        # render node offset
        try:
            render_id = 128 + int(device_id)
        except ValueError:
            render_id = 128

        # Generate CDI v1.1.0 specification
        cdi_spec = {
            "cdiVersion": "1.1.0",
            "kind": "amd.com/gpu",
            "devices": [
                {
                    "name": str(device_id),
                    "containerEdits": {
                        "deviceNodes": [
                            {
                                "path": f"/dev/dri/renderD{render_id}",
                                "hostPath": f"/dev/dri/renderD{render_id}",
                                "type": "c"
                            },
                            {
                                "path": "/dev/kfd",
                                "hostPath": "/dev/kfd",
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
        cdi_file_path = os.path.join(self.cdi_dir, f"amd.com_{claim_uid}.json")
        self.logger.info(f"Writing CDI JSON spec to {cdi_file_path}")
        with open(cdi_file_path, "w") as f:
            json.dump(cdi_spec, f, indent=2)

        return [cdi_device_str]

    async def unprepare_hardware(self, claim_uid: str, namespace: str, name: str):
        self.logger.info(f"unprepare_hardware: claim_uid={claim_uid}, namespace={namespace}, name={name}")

        cdi_file_path = os.path.join(self.cdi_dir, f"amd.com_{claim_uid}.json")
        if os.path.exists(cdi_file_path):
            self.logger.info(f"Removing CDI JSON spec file {cdi_file_path}")
            os.remove(cdi_file_path)
        else:
            self.logger.warning(f"CDI JSON spec file {cdi_file_path} not found during unprepare")

async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    socket_path = os.environ.get("SOCKET_PATH", "/var/lib/kubelet/plugins/amd.com-gpu/plugin.sock")
    kubelet_socket_path = os.environ.get("KUBELET_SOCKET_PATH", socket_path)
    registration_socket_path = os.environ.get("REGISTRATION_SOCKET_PATH", None)
    cdi_dir = os.environ.get("CDI_DIR", "/var/run/cdi")

    plugin = AmdDraPlugin(
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
