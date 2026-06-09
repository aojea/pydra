import abc
import os
import json
import fcntl
import asyncio
import contextlib
import logging
import threading
import grpc

from pydra.core.generated.pluginregistration import pluginregistration_pb2 as reg_pb2
from pydra.core.generated.pluginregistration import pluginregistration_pb2_grpc as reg_pb2_grpc
from pydra.core.generated.dra import dra_pb2 as dra_pb2
from pydra.core.generated.dra import dra_pb2_grpc as dra_pb2_grpc
from pydra.core.generated.deviceplugin import deviceplugin_pb2 as deviceplugin_pb2
from pydra.core.generated.deviceplugin import deviceplugin_pb2_grpc as deviceplugin_pb2_grpc

class RegistrationWrapper(reg_pb2_grpc.RegistrationServicer):
    def __init__(self, plugin_type, plugin_name, endpoint, supported_versions, logger):
        self.plugin_type = plugin_type
        self.plugin_name = plugin_name
        self.endpoint = endpoint
        self.supported_versions = supported_versions
        self.logger = logger

    async def GetInfo(self, request, context):
        self.logger.info(f"GetInfo called for {self.plugin_type}")
        try:
            endpoint_path = self.endpoint
            if endpoint_path.startswith("unix://"):
                endpoint_path = endpoint_path[7:]
            self.logger.info(f"Returning PluginInfo with endpoint: {endpoint_path}")
            return reg_pb2.PluginInfo(
                type=self.plugin_type,
                name=self.plugin_name,
                endpoint=endpoint_path,
                supported_versions=self.supported_versions
            )
        except Exception as e:
            self.logger.exception("Error in GetInfo")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            raise

    async def NotifyRegistrationStatus(self, request, context):
        self.logger.info(f"NotifyRegistrationStatus for {self.plugin_type}: registered={request.plugin_registered}, error={request.error}")
        return reg_pb2.RegistrationStatusResponse()


class DraNodeServer(dra_pb2_grpc.DRAPluginServicer, deviceplugin_pb2_grpc.DevicePluginServicer, abc.ABC):
    def __init__(self, plugin_name: str, socket_path: str, kubelet_socket_path: str = None, registration_socket_path: str = None, enable_device_metadata: bool = False, cdi_directory: str = "/var/run/cdi", enable_dra: bool = None, enable_device_plugin: bool = None):
        self.plugin_name = plugin_name
        self.socket_path = socket_path
        self.kubelet_socket_path = kubelet_socket_path or socket_path
        self.registration_socket_path = registration_socket_path
        self.enable_device_metadata = enable_device_metadata
        self.cdi_directory = cdi_directory
        self.logger = logging.getLogger(self.__class__.__name__)
        
        if enable_dra is None:
            self.enable_dra = self._discover_dra_enabled()
            self.logger.info(f"Discovered DRA enabled: {self.enable_dra}")
        else:
            self.enable_dra = enable_dra

        if enable_device_plugin is None:
            self.enable_device_plugin = not self.enable_dra
        else:
            self.enable_device_plugin = enable_device_plugin
        
        self.logger.info(f"Configured with DRA={self.enable_dra}, DevicePlugin={self.enable_device_plugin}")
        self.k8s_api = None
        self._watcher_task = None
        self._stop_event = threading.Event()

    @contextlib.asynccontextmanager
    async def _lock(self):
        plugin_dir = os.path.dirname(self.socket_path)
        os.makedirs(plugin_dir, exist_ok=True)
        lock_path = os.path.join(plugin_dir, "serialize.lock")
        f = await asyncio.to_thread(open, lock_path, "w")
        try:
            await asyncio.to_thread(fcntl.flock, f.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            await asyncio.to_thread(fcntl.flock, f.fileno(), fcntl.LOCK_UN)
            f.close()

    def _discover_dra_enabled(self):
        from kubernetes import client, config
        try:
            config.load_incluster_config()
        except Exception:
            try:
                config.load_kube_config()
            except Exception:
                self.logger.warning("Could not load k8s config, assuming DRA is disabled")
                return False
        api = client.ApisApi()
        try:
            groups = api.get_api_versions().groups
            for group in groups:
                if group.name == "resource.k8s.io":
                    return True
        except Exception as e:
            self.logger.warning(f"Failed to fetch k8s API groups: {e}")
        return False

    async def NodePrepareResources(self, request, context):
        self.logger.info(f"NodePrepareResources called for {len(request.claims)} claims")
        prepared_claims = {}
        async with self._lock():
            for claim in request.claims:
                self.logger.info(f"Preparing resource for claim UID: {claim.uid}, Namespace: {claim.namespace}, Name: {claim.name}")
                try:
                    task = asyncio.create_task(self.prepare_hardware(claim.uid, claim.namespace, claim.name))
                    def on_rpc_done():
                        if not context.is_active():
                            task.cancel()
                    context.add_done_callback(on_rpc_done)

                    cdi_devices = await task
                    devices = []
                    for dev_info in cdi_devices:
                        if isinstance(dev_info, str):
                            cdi_id = dev_info
                            metadata = None
                        else:
                            cdi_id = dev_info.get("cdi_id")
                            metadata = dev_info.get("metadata")

                        dev_name = "0"
                        if cdi_id and "=" in cdi_id:
                            dev_name = cdi_id.split("=")[-1]

                        if self.enable_device_metadata and metadata:
                            plugin_dir = os.path.dirname(self.socket_path)
                            meta_dir = os.path.join(plugin_dir, claim.uid, claim.name)
                            os.makedirs(meta_dir, exist_ok=True)
                            meta_file = os.path.join(meta_dir, "metadata.json")
                            with open(meta_file, "w") as f:
                                json.dump(metadata, f)
                            
                            os.makedirs(self.cdi_directory, exist_ok=True)
                            cdi_spec_name = f"{self.plugin_name.replace('/', '_')}_metadata_{claim.uid}_{claim.name}.json"
                            cdi_spec_path = os.path.join(self.cdi_directory, cdi_spec_name)
                            
                            cdi_vendor = self.plugin_name
                            cdi_class = "metadata"
                            cdi_device_name = f"{claim.uid}-{claim.name}"
                            generated_cdi_id = f"{cdi_vendor}/{cdi_class}={cdi_device_name}"
                            
                            cdi_spec = {
                                "cdiVersion": "0.5.0",
                                "kind": cdi_vendor + "/" + cdi_class,
                                "devices": [
                                    {
                                        "name": cdi_device_name,
                                        "containerEdits": {
                                            "mounts": [
                                                {
                                                    "hostPath": meta_file,
                                                    "containerPath": f"/var/run/kubernetes.io/dra-device-attributes/{claim.name}/metadata.json",
                                                    "options": ["ro"]
                                                }
                                            ]
                                        }
                                    }
                                ]
                            }
                            with open(cdi_spec_path, "w") as f:
                                json.dump(cdi_spec, f)
                            
                            cdi_id = generated_cdi_id

                        devices.append(dra_pb2.Device(
                            pool_name=self.plugin_name,
                            device_name=dev_name,
                            cdi_device_ids=[cdi_id] if cdi_id else []
                        ))
                    prepared_claims[claim.uid] = dra_pb2.NodePrepareResourceResponse(devices=devices, error="")
                except asyncio.CancelledError:
                    self.logger.warning(f"Preparation for claim {claim.uid} cancelled by Kubelet.")
                    prepared_claims[claim.uid] = dra_pb2.NodePrepareResourceResponse(error="cancelled")
                except Exception as e:
                    self.logger.exception(f"Failed to prepare resource for claim {claim.uid}")
                    prepared_claims[claim.uid] = dra_pb2.NodePrepareResourceResponse(error=str(e))

        return dra_pb2.NodePrepareResourcesResponse(claims=prepared_claims)

    async def NodeUnprepareResources(self, request, context):
        self.logger.info(f"NodeUnprepareResources called for {len(request.claims)} claims")
        unprepared_claims = {}
        async with self._lock():
            for claim in request.claims:
                self.logger.info(f"Unpreparing resource for claim UID: {claim.uid}")
                try:
                    task = asyncio.create_task(self.unprepare_hardware(claim.uid, claim.namespace, claim.name))
                    def on_rpc_done():
                        if not context.is_active():
                            task.cancel()
                    context.add_done_callback(on_rpc_done)

                    await task

                    if self.enable_device_metadata:
                        plugin_dir = os.path.dirname(self.socket_path)
                        meta_dir = os.path.join(plugin_dir, claim.uid, claim.name)
                        meta_file = os.path.join(meta_dir, "metadata.json")
                        if os.path.exists(meta_file):
                            os.remove(meta_file)
                        if os.path.exists(meta_dir):
                            try:
                                os.rmdir(meta_dir)
                            except OSError:
                                pass
                        
                        cdi_spec_name = f"{self.plugin_name.replace('/', '_')}_metadata_{claim.uid}_{claim.name}.json"
                        cdi_spec_path = os.path.join(self.cdi_directory, cdi_spec_name)
                        if os.path.exists(cdi_spec_path):
                            os.remove(cdi_spec_path)

                    unprepared_claims[claim.uid] = dra_pb2.NodeUnprepareResourceResponse(error="")
                except asyncio.CancelledError:
                    self.logger.warning(f"Unpreparation for claim {claim.uid} cancelled by Kubelet.")
                    unprepared_claims[claim.uid] = dra_pb2.NodeUnprepareResourceResponse(error="cancelled")
                except Exception as e:
                    self.logger.exception(f"Failed to unprepare resource for claim {claim.uid}")
                    unprepared_claims[claim.uid] = dra_pb2.NodeUnprepareResourceResponse(error=str(e))
        return dra_pb2.NodeUnprepareResourcesResponse(claims=unprepared_claims)

    async def GetDevicePluginOptions(self, request, context):
        return deviceplugin_pb2.DevicePluginOptions(pre_start_required=False)

    async def ListAndWatch(self, request, context):
        self.logger.info("ListAndWatch called")
        devices = []
        for dev in self.get_devices():
            dev_name = dev if isinstance(dev, str) else dev.get("name")
            devices.append(deviceplugin_pb2.Device(id=dev_name, health="Healthy"))
        
        yield deviceplugin_pb2.ListAndWatchResponse(devices=devices)
        
        try:
            while not self._stop_event.is_set():
                if not context.is_active():
                    break
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass

    async def Allocate(self, request, context):
        self.logger.info("Allocate called for legacy device plugin")
        responses = []
        for container_req in request.container_requests:
            cdi_devices = await self.allocate_legacy_devices(container_req.devicesIDs)
            responses.append(deviceplugin_pb2.ContainerAllocateResponse(
                cdi_devices=[deviceplugin_pb2.CDIDevice(name=dev) for dev in cdi_devices]
            ))
        return deviceplugin_pb2.AllocateResponse(container_responses=responses)

    async def GetPreferredAllocation(self, request, context):
        return deviceplugin_pb2.PreferredAllocationResponse()

    async def PreStartContainer(self, request, context):
        return deviceplugin_pb2.PreStartContainerResponse()

    @abc.abstractmethod
    def get_devices(self) -> list:
        pass

    @abc.abstractmethod
    async def prepare_hardware(self, claim_uid: str, namespace: str, name: str) -> list:
        pass

    @abc.abstractmethod
    async def unprepare_hardware(self, claim_uid: str, namespace: str, name: str):
        pass

    async def allocate_legacy_devices(self, device_ids: list[str]) -> list[str]:
        # To be implemented by subclasses if they support legacy device plugin
        return []

    def _init_kube_client(self):
        from kubernetes import client, config
        try:
            config.load_incluster_config()
        except Exception:
            try:
                config.load_kube_config()
            except Exception as e:
                self.logger.warning(f"Could not load kubeconfig: {e}. Kubernetes operations will be skipped.")
                self.k8s_api = None
                return
        self.k8s_api = client.ResourceV1Api()

    def _get_resource_slice_object(self):
        from kubernetes import client
        node_name = os.environ.get("NODE_NAME", "pydra-test-control-plane")
        device_ids = self.get_devices()

        devices = []
        for dev in device_ids:
            if isinstance(dev, str):
                devices.append(client.V1Device(name=dev))
            elif isinstance(dev, dict):
                attrs = dev.get("attributes", {})
                device_attrs = {}
                for k, v in attrs.items():
                    if isinstance(v, bool):
                        device_attrs[k] = client.V1DeviceAttribute(bool=v)
                    elif isinstance(v, int):
                        device_attrs[k] = client.V1DeviceAttribute(int=v)
                    else:
                        device_attrs[k] = client.V1DeviceAttribute(string=str(v))
                devices.append(client.V1Device(name=dev.get("name", "0"), attributes=device_attrs))

        pool = client.V1ResourcePool(
            name=self.plugin_name,
            generation=1,
            resource_slice_count=1
        )
        spec = client.V1ResourceSliceSpec(
            driver=self.plugin_name,
            node_name=node_name,
            pool=pool,
            devices=devices
        )
        slice_name = f"{node_name}-{self.plugin_name.replace('/', '-')}"
        return client.V1ResourceSlice(
            api_version="resource.k8s.io/v1",
            kind="ResourceSlice",
            metadata=client.V1ObjectMeta(name=slice_name),
            spec=spec
        )

    def _watch_resource_slice(self):
        from kubernetes import client, watch
        import time
        self._init_kube_client()
        if not self.k8s_api:
            return
        
        node_name = os.environ.get("NODE_NAME", "pydra-test-control-plane")
        slice_name = f"{node_name}-{self.plugin_name.replace('/', '-')}"
        
        w = watch.Watch()
        self.logger.info(f"Starting ResourceSlice watcher for {slice_name}")
        
        def sync_slice():
            if self._stop_event.is_set():
                return
            resource_slice = self._get_resource_slice_object()
            try:
                existing = self.k8s_api.read_resource_slice(name=slice_name)
                resource_slice.metadata.resource_version = existing.metadata.resource_version
                self.k8s_api.replace_resource_slice(name=slice_name, body=resource_slice)
            except client.exceptions.ApiException as e:
                if e.status == 404:
                    self.logger.info(f"Creating ResourceSlice {slice_name}")
                    self.k8s_api.create_resource_slice(body=resource_slice)
                else:
                    self.logger.error(f"Failed to publish ResourceSlice: {e}")

        sync_slice()

        while not self._stop_event.is_set():
            try:
                for event in w.stream(self.k8s_api.list_resource_slice, field_selector=f"metadata.name={slice_name}", timeout_seconds=10):
                    if self._stop_event.is_set():
                        w.stop()
                        break
                    if event['type'] == 'DELETED':
                        self.logger.info(f"ResourceSlice {slice_name} was deleted, recreating...")
                        sync_slice()
                    elif event['type'] == 'MODIFIED':
                        sync_slice()
            except Exception as e:
                if self._stop_event.is_set():
                    break
                self.logger.warning(f"Watcher disconnected, retrying: {e}")
                time.sleep(2)

    def delete_resource_slice(self):
        if not self.k8s_api:
            return
        from kubernetes import client
        node_name = os.environ.get("NODE_NAME", "pydra-test-control-plane")
        slice_name = f"{node_name}-{self.plugin_name.replace('/', '-')}"
        try:
            self.logger.info(f"Deleting ResourceSlice {slice_name}")
            self.k8s_api.delete_resource_slice(name=slice_name)
        except client.exceptions.ApiException as e:
            if e.status != 404:
                self.logger.error(f"Failed to delete ResourceSlice: {e}")

    async def serve(self):
        self.servers = []
        clean_paths = []

        if self.enable_dra:
            server_dra = grpc.aio.server()
            dra_pb2_grpc.add_DRAPluginServicer_to_server(self, server_dra)
            reg_dra = RegistrationWrapper("DRAPlugin", self.plugin_name, self.kubelet_socket_path, ["v1.DRAPlugin"], self.logger)
            reg_pb2_grpc.add_RegistrationServicer_to_server(reg_dra, server_dra)
            
            addresses = [self.socket_path]
            if self.registration_socket_path:
                addresses.append(self.registration_socket_path)
            
            for addr in addresses:
                bind_address = addr
                if bind_address.startswith("unix://"):
                    clean_path = bind_address[7:]
                else:
                    clean_path = bind_address
                    bind_address = f"unix://{bind_address}"

                clean_paths.append(clean_path)
                os.makedirs(os.path.dirname(clean_path), exist_ok=True)

                if os.path.exists(clean_path):
                    self.logger.warning(f"Socket file {clean_path} already exists. Removing it.")
                    os.remove(clean_path)

                server_dra.add_insecure_port(bind_address)
                self.logger.info(f"DRA Server bound to address: {bind_address}")
            self.servers.append(server_dra)

        if self.enable_device_plugin:
            server_dp = grpc.aio.server()
            deviceplugin_pb2_grpc.add_DevicePluginServicer_to_server(self, server_dp)
            dp_socket = f"{self.socket_path}-legacy"
            dp_kubelet_socket = f"{self.kubelet_socket_path}-legacy"
            
            reg_dp = RegistrationWrapper("DevicePlugin", self.plugin_name, dp_kubelet_socket, ["v1beta1"], self.logger)
            reg_pb2_grpc.add_RegistrationServicer_to_server(reg_dp, server_dp)

            addresses_dp = [dp_socket]
            
            for addr in addresses_dp:
                bind_address = addr
                if bind_address.startswith("unix://"):
                    clean_path = bind_address[7:]
                else:
                    clean_path = bind_address
                    bind_address = f"unix://{bind_address}"

                clean_paths.append(clean_path)
                os.makedirs(os.path.dirname(clean_path), exist_ok=True)

                if os.path.exists(clean_path):
                    self.logger.warning(f"Socket file {clean_path} already exists. Removing it.")
                    os.remove(clean_path)

                server_dp.add_insecure_port(bind_address)
                self.logger.info(f"DevicePlugin Server bound to address: {bind_address}")
            self.servers.append(server_dp)

        for s in self.servers:
            await s.start()

        watcher_future = None
        if self.enable_dra:
            self._watcher_task = asyncio.to_thread(self._watch_resource_slice)
            loop = asyncio.get_running_loop()
            watcher_future = loop.create_task(self._watcher_task)

        try:
            await asyncio.gather(*(s.wait_for_termination() for s in self.servers))
        finally:
            self._stop_event.set()
            if watcher_future:
                watcher_future.cancel()
                await asyncio.to_thread(self.delete_resource_slice)
            for s in self.servers:
                await s.stop(0)
            for clean_path in clean_paths:
                if os.path.exists(clean_path):
                    try:
                        os.remove(clean_path)
                    except OSError:
                        pass
