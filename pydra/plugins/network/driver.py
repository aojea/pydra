import asyncio
import json
import logging
import os
import sys

from pydra.core.server import DraNodeServer

class NetworkDraPlugin(DraNodeServer):
    def __init__(self, socket_path: str, kubelet_socket_path: str = None, registration_socket_path: str = None, cdi_dir: str = None):
        plugin_name = "dra.net"
        super().__init__(plugin_name=plugin_name, socket_path=socket_path, kubelet_socket_path=kubelet_socket_path, registration_socket_path=registration_socket_path)
        self.cdi_dir = cdi_dir or "/var/run/cdi"
        self.logger.info(f"Initialized NetworkDraPlugin. cdi_dir: {self.cdi_dir}")

    def get_devices(self) -> list:
        import re
        dns1123_pattern = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
        try:
            sys_class_net = os.environ.get("SYS_CLASS_NET", "/sys/class/net/")
            ifaces = os.listdir(sys_class_net)
            devices = []
            for i in ifaces:
                if i == "lo" or i.startswith("veth") or not dns1123_pattern.match(i):
                    continue
                dev_type = "unknown"
                if os.path.exists(os.path.join(sys_class_net, i, "device")):
                    dev_type = "physical"
                else:
                    try:
                        with open(os.path.join(sys_class_net, i, "uevent"), "r") as f:
                            uevent = f.read()
                            if "DEVTYPE=dummy" in uevent or i.startswith("dummy"):
                                dev_type = "dummy"
                    except Exception:
                        pass
                devices.append({
                    "name": i,
                    "attributes": {
                        "dra.net/type": dev_type
                    }
                })
            return devices
        except Exception as e:
            self.logger.error(f"Error listing network interfaces: {e}")
            return []

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

        # Fallback to the first available device if we couldn't determine it
        if not device_id:
            devices = self.get_devices()
            if devices:
                device_id = devices[0]
                self.logger.warning(f"Fallback to device_id = {device_id}")
            else:
                raise RuntimeError("No network devices available to allocate")

        cdi_device_str = f"network.pydra.io/device={device_id}"

        # Generate CDI v1.1.0 specification
        cdi_spec = {
            "cdiVersion": "1.1.0",
            "kind": "network.pydra.io/device",
            "devices": [
                {
                    "name": device_id,
                    "containerEdits": {
                        "netDevices": [
                            {
                                "hostInterfaceName": device_id,
                                "name": device_id
                            }
                        ]
                    }
                }
            ]
        }

        # Ensure CDI directory exists
        os.makedirs(self.cdi_dir, exist_ok=True)
        
        # Write CDI JSON file
        cdi_file_path = os.path.join(self.cdi_dir, f"network.pydra.io_{claim_uid}.json")
        self.logger.info(f"Writing CDI JSON spec to {cdi_file_path}")
        with open(cdi_file_path, "w") as f:
            json.dump(cdi_spec, f, indent=2)

        return [cdi_device_str]

    async def unprepare_hardware(self, claim_uid: str, namespace: str, name: str):
        self.logger.info(f"unprepare_hardware: claim_uid={claim_uid}, namespace={namespace}, name={name}")
        
        cdi_file_path = os.path.join(self.cdi_dir, f"network.pydra.io_{claim_uid}.json")
        if os.path.exists(cdi_file_path):
            self.logger.info(f"Removing CDI JSON spec file {cdi_file_path}")
            os.remove(cdi_file_path)
        else:
            self.logger.warning(f"CDI JSON spec file {cdi_file_path} not found during unprepare")

async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    
    # Read environment overrides or use defaults
    socket_path = os.environ.get("SOCKET_PATH", "/var/lib/kubelet/plugins/network.pydra.io/plugin.sock")
    kubelet_socket_path = os.environ.get("KUBELET_SOCKET_PATH", socket_path)
    registration_socket_path = os.environ.get("REGISTRATION_SOCKET_PATH", None)
    cdi_dir = os.environ.get("CDI_DIR", "/var/run/cdi")
    
    plugin = NetworkDraPlugin(
        socket_path=socket_path,
        kubelet_socket_path=kubelet_socket_path,
        registration_socket_path=registration_socket_path,
        cdi_dir=cdi_dir
    )
    try:
        await plugin.serve()
    except Exception as e:
        logging.getLogger("main").exception("Plugin server crashed")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
