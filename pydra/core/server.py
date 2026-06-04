import abc
import os
import asyncio
import logging
import grpc

from pydra.core.generated.pluginregistration import pluginregistration_pb2 as reg_pb2
from pydra.core.generated.pluginregistration import pluginregistration_pb2_grpc as reg_pb2_grpc
from pydra.core.generated.dra import dra_pb2 as dra_pb2
from pydra.core.generated.dra import dra_pb2_grpc as dra_pb2_grpc

class DraNodeServer(reg_pb2_grpc.RegistrationServicer, dra_pb2_grpc.DRAPluginServicer, abc.ABC):
    def __init__(self, plugin_name: str, socket_path: str, kubelet_socket_path: str = None, registration_socket_path: str = None):
        self.plugin_name = plugin_name
        # socket_path is where the server binds locally.
        self.socket_path = socket_path
        # kubelet_socket_path is the socket path from the perspective of kubelet.
        self.kubelet_socket_path = kubelet_socket_path or socket_path
        self.registration_socket_path = registration_socket_path
        self.logger = logging.getLogger(self.__class__.__name__)
        self.k8s_api = None

    async def GetInfo(self, request, context):
        self.logger.info("GetInfo called")
        try:
            # The endpoint field must be the socket path relative or absolute from Kubelet perspective.
            # Usually it is the absolute path inside the kubelet plugin dir, e.g. /var/lib/kubelet/plugins/tpu.google.com/plugin.sock
            # We strip unix:// prefix if present.
            endpoint_path = self.kubelet_socket_path
            if endpoint_path.startswith("unix://"):
                endpoint_path = endpoint_path[7:]

            self.logger.info(f"Returning PluginInfo with endpoint: {endpoint_path}")
            return reg_pb2.PluginInfo(
                type="DRAPlugin",
                name=self.plugin_name,
                endpoint=endpoint_path,
                supported_versions=["v1.DRAPlugin"]
            )
        except Exception as e:
            self.logger.exception("Error in GetInfo")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            raise

    async def NotifyRegistrationStatus(self, request, context):
        self.logger.info(f"NotifyRegistrationStatus: registered={request.plugin_registered}, error={request.error}")
        return reg_pb2.RegistrationStatusResponse()

    async def NodePrepareResources(self, request, context):
        self.logger.info(f"NodePrepareResources called for {len(request.claims)} claims")
        prepared_claims = {}
        for claim in request.claims:
            self.logger.info(f"Preparing resource for claim UID: {claim.uid}, Namespace: {claim.namespace}, Name: {claim.name}")
            try:
                cdi_devices = await self.prepare_hardware(claim.uid, claim.namespace, claim.name)
                # Build Device message for each allocated device
                devices = []
                for cdi_id in cdi_devices:
                    # cdi_id is formatted as vendor/device_class=device_name, e.g., tpu.google.com/device=0
                    # Let's parse device_name from cdi_id
                    dev_name = "0"
                    if "=" in cdi_id:
                        dev_name = cdi_id.split("=")[-1]
                    devices.append(dra_pb2.Device(
                        pool_name=self.plugin_name,
                        device_name=dev_name,
                        cdi_device_ids=[cdi_id]
                    ))
                prepared_claims[claim.uid] = dra_pb2.NodePrepareResourceResponse(
                    devices=devices,
                    error=""
                )
            except Exception as e:
                self.logger.exception(f"Failed to prepare resource for claim {claim.uid}")
                prepared_claims[claim.uid] = dra_pb2.NodePrepareResourceResponse(
                    error=str(e)
                )

        return dra_pb2.NodePrepareResourcesResponse(claims=prepared_claims)

    async def NodeUnprepareResources(self, request, context):
        self.logger.info(f"NodeUnprepareResources called for {len(request.claims)} claims")
        unprepared_claims = {}
        for claim in request.claims:
            self.logger.info(f"Unpreparing resource for claim UID: {claim.uid}")
            try:
                await self.unprepare_hardware(claim.uid, claim.namespace, claim.name)
                unprepared_claims[claim.uid] = dra_pb2.NodeUnprepareResourceResponse(
                    error=""
                )
            except Exception as e:
                self.logger.exception(f"Failed to unprepare resource for claim {claim.uid}")
                unprepared_claims[claim.uid] = dra_pb2.NodeUnprepareResourceResponse(
                    error=str(e)
                )
        return dra_pb2.NodeUnprepareResourcesResponse(claims=unprepared_claims)

    @abc.abstractmethod
    def get_devices(self) -> list[str]:
        pass

    @abc.abstractmethod
    async def prepare_hardware(self, claim_uid: str, namespace: str, name: str) -> list[str]:
        pass

    @abc.abstractmethod
    async def unprepare_hardware(self, claim_uid: str, namespace: str, name: str):
        pass

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

    def publish_resource_slice(self):
        self._init_kube_client()
        if not self.k8s_api:
            return
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
                devices.append(client.V1Device(name=dev["name"], attributes=device_attrs))

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
        resource_slice = client.V1ResourceSlice(
            api_version="resource.k8s.io/v1",
            kind="ResourceSlice",
            metadata=client.V1ObjectMeta(
                name=slice_name
            ),
            spec=spec
        )
        
        try:
            # Try to read first
            existing = self.k8s_api.read_resource_slice(name=slice_name)
            self.logger.info(f"ResourceSlice {slice_name} already exists. Replacing it.")
            resource_slice.metadata.resource_version = existing.metadata.resource_version
            self.k8s_api.replace_resource_slice(name=slice_name, body=resource_slice)
        except client.exceptions.ApiException as e:
            if e.status == 404:
                self.logger.info(f"Creating ResourceSlice {slice_name}")
                self.k8s_api.create_resource_slice(body=resource_slice)
            else:
                self.logger.error(f"Failed to publish ResourceSlice: {e}")

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
        server = grpc.aio.server()
        reg_pb2_grpc.add_RegistrationServicer_to_server(self, server)
        dra_pb2_grpc.add_DRAPluginServicer_to_server(self, server)

        addresses = [self.socket_path]
        if self.registration_socket_path:
            addresses.append(self.registration_socket_path)

        clean_paths = []
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

            server.add_insecure_port(bind_address)
            self.logger.info(f"Server bound to address: {bind_address}")

        self.logger.info(f"Advertised Kubelet endpoint: {self.kubelet_socket_path}")
        await server.start()
        
        # Publish ResourceSlice to API server
        await asyncio.to_thread(self.publish_resource_slice)
        
        try:
            await server.wait_for_termination()
        finally:
            # Delete ResourceSlice from API server on shutdown
            await asyncio.to_thread(self.delete_resource_slice)
            await server.stop(0)
            for clean_path in clean_paths:
                if os.path.exists(clean_path):
                    try:
                        os.remove(clean_path)
                    except OSError:
                        pass
