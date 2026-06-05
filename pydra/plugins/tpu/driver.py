import asyncio
import json
import logging
import os
import sys
import glob

from pydra.core.server import DraNodeServer

class TpuDraPlugin(DraNodeServer):
    def __init__(self, socket_path: str, kubelet_socket_path: str = None, registration_socket_path: str = None, cdi_dir: str = None):
        plugin_name = "tpu.google.com"
        super().__init__(plugin_name=plugin_name, socket_path=socket_path, kubelet_socket_path=kubelet_socket_path, registration_socket_path=registration_socket_path)
        self.cdi_dir = cdi_dir or "/var/run/cdi"
        self.logger.info(f"Initialized TpuDraPlugin. cdi_dir: {self.cdi_dir}")

    def get_devices(self) -> list:
        # Provide better insights on the resourceslice with the tpu characteristics, network topology and details
        characteristics = "unknown"
        topology = "unknown"
        details = "unknown"
        try:
            import urllib.request
            import json
            req = urllib.request.Request("http://metadata.google.internal/computeMetadata/v1/instance/attributes/?recursive=true", headers={"Metadata-Flavor": "Google"})
            with urllib.request.urlopen(req, timeout=2) as response:
                attrs = json.loads(response.read().decode())
                characteristics = attrs.get("accelerator-type", "unknown")
                topology = attrs.get("physical_host_topology", "unknown")
                details = attrs.get("tpu-env", "unknown")
        except Exception as e:
            self.logger.warning(f"Could not fetch metadata for TPU characteristics: {e}")

        # Fetch detailed topology and metrics using libtpu.sdk if available
        chip_sdk_info = {}
        monitoring_info = {}
        try:
            from libtpu import sdk
            try:
                chip_mappings = sdk.slice.get_chip_coordinates()
                for mapping in chip_mappings:
                    idx = str(mapping.chip_index())
                    chip_sdk_info[idx] = {
                        "hostname": mapping.hostname(),
                        "coordinates": str(mapping.coordinates())
                    }
            except Exception as e:
                self.logger.warning(f"Failed to fetch chip coordinates from sdk: {e}")

            monitoring_module = getattr(sdk, "tpumonitoring", getattr(sdk, "monitoring", None))
            if monitoring_module:
                try:
                    tc_util = monitoring_module.get_metric("tensorcore_util").data()
                    monitoring_info["tensorcore_util"] = str(tc_util)
                except Exception as e:
                    self.logger.warning(f"Failed to fetch TensorCore utilization: {e}")
                
                try:
                    hbm_usage = monitoring_module.get_metric("hbm_capacity_usage").data()
                    hbm_total = monitoring_module.get_metric("hbm_capacity_total").data()
                    monitoring_info["hbm_usage"] = str(hbm_usage)
                    monitoring_info["hbm_total"] = str(hbm_total)
                except Exception as e:
                    self.logger.warning(f"Failed to fetch HBM usage: {e}")
        except ImportError:
            self.logger.warning("libtpu.sdk not available, skipping detailed SDK metrics.")
        except Exception as e:
            self.logger.warning(f"Failed to initialize libtpu sdk: {e}")

        # Find all available accel devices
        accel_devices = glob.glob("/dev/accel*")
        if not accel_devices:
            return []

        devices = []
        for dev_path in accel_devices:
            dev_name = dev_path.replace("/dev/accel", "")
            
            # Base attributes from instance metadata
            attributes = {
                "tpu.google.com/characteristics": characteristics,
                "tpu.google.com/topology": topology,
                "tpu.google.com/details": details,
            }
            
            # SDK-specific attributes per chip
            if dev_name in chip_sdk_info:
                attributes["tpu.google.com/sdk-hostname"] = chip_sdk_info[dev_name]["hostname"]
                attributes["tpu.google.com/sdk-coordinates"] = chip_sdk_info[dev_name]["coordinates"]
            
            # Global monitoring attributes
            if "tensorcore_util" in monitoring_info:
                attributes["tpu.google.com/tensorcore-util"] = monitoring_info["tensorcore_util"]
            if "hbm_total" in monitoring_info:
                attributes["tpu.google.com/hbm-total"] = monitoring_info["hbm_total"]
                attributes["tpu.google.com/hbm-usage"] = monitoring_info["hbm_usage"]

            devices.append({
                "name": dev_name,
                "attributes": attributes
            })
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

        # Fallback to the first available device if we couldn't determine it
        if not device_id:
            devices = self.get_devices()
            if devices:
                device_id = devices[0]["name"]
                self.logger.warning(f"Fallback to device_id = {device_id}")
            else:
                raise RuntimeError("No TPU devices available to allocate")

        cdi_device_str = f"tpu.google.com/device={device_id}"
        
        # Use libtpu if available
        libtpu_path = "/usr/lib/libtpu.so"
        try:
            import libtpu
            libtpu_path = libtpu.get_library_path()
        except ImportError:
            self.logger.warning("libtpu package not found, using default libtpu.so path")

        # Generate CDI v1.1.0 specification
        cdi_spec = {
            "cdiVersion": "1.1.0",
            "kind": "tpu.google.com/device",
            "devices": [
                {
                    "name": str(device_id),
                    "containerEdits": {
                        "deviceNodes": [
                            {
                                "path": f"/dev/accel{device_id}",
                                "hostPath": f"/dev/accel{device_id}",
                                "type": "c"
                            }
                        ],
                        "mounts": [
                            {
                                "hostPath": libtpu_path,
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

