"""
pydra/core/server.py

Async gRPC server base class implementing both:
  - Kubernetes PluginRegistration v1 (for kubelet plugin discovery)
  - Kubernetes DRA v1beta1 Node service (for resource preparation)

Both services are served over Unix Domain Sockets (UDS).
Subclasses override prepare_hardware() / unprepare_hardware() to provide
hardware-specific allocation logic.
"""

import abc
import asyncio
import logging
import os
import sys
from typing import List, Optional

import grpc
import grpc.aio

# ---------------------------------------------------------------------------
# Path bootstrap: add the generated/ sub-package to sys.path so that the
# auto-generated gRPC stubs can resolve their own internal imports.
# ---------------------------------------------------------------------------
_GENERATED_DIR = os.path.join(os.path.dirname(__file__), "generated")
if _GENERATED_DIR not in sys.path:
    sys.path.insert(0, _GENERATED_DIR)

from pluginregistration.v1 import api_pb2 as reg_pb2          # noqa: E402
from pluginregistration.v1 import api_pb2_grpc as reg_grpc    # noqa: E402
from dra.v1beta1 import api_pb2 as dra_pb2                    # noqa: E402
from dra.v1beta1 import api_pb2_grpc as dra_grpc              # noqa: E402

logger = logging.getLogger(__name__)


class DraNodeServer(
    reg_grpc.RegistrationServicer,
    dra_grpc.DRAPluginServicer,
    abc.ABC,
):
    """
    Base class for a Kubernetes DRA node plugin.

    It runs two gRPC servers over Unix Domain Sockets:

    1. **Registration server** (``registration_socket_path``):
       Serves the PluginRegistration API so kubelet can discover the driver.
       ``GetInfo`` returns the *endpoint* path that kubelet should use to
       connect to the DRA Node service (may differ from the bind path when
       running with bind-mounts, e.g. local dev against a kind cluster).

    2. **Node server** (``plugin_socket_path``):
       Serves the DRA Node API (NodePrepareResources / NodeUnprepareResources).

    Subclasses must implement :meth:`prepare_hardware` and
    :meth:`unprepare_hardware`.
    """

    def __init__(
        self,
        plugin_name: str,
        plugin_socket_path: str,
        registration_socket_path: str,
        plugin_endpoint: Optional[str] = None,
    ) -> None:
        """
        Args:
            plugin_name:               Unique driver name (e.g. ``tpu.google.com``).
            plugin_socket_path:        Absolute path where the Node gRPC server
                                       will bind (host-side path).
            registration_socket_path:  Absolute path where the Registration gRPC
                                       server will bind (host-side path, placed in
                                       ``plugins_registry/`` directory).
            plugin_endpoint:           The socket path *as the kubelet sees it*
                                       (i.e. the in-container path when running
                                       with bind-mounts).  Defaults to
                                       ``plugin_socket_path``.
        """
        self.plugin_name = plugin_name
        self.plugin_socket_path = plugin_socket_path
        self.registration_socket_path = registration_socket_path
        # When running locally with kind bind-mounts the kubelet sees the socket
        # at a different path than the host Python process binds to.
        self.plugin_endpoint = plugin_endpoint or plugin_socket_path

    # ------------------------------------------------------------------
    # PluginRegistration service implementation
    # ------------------------------------------------------------------

    async def GetInfo(self, request: reg_pb2.InfoRequest, context: grpc.aio.ServicerContext) -> reg_pb2.PluginInfo:
        """Return plugin metadata so the kubelet can connect to the DRA service."""
        logger.info(
            "GetInfo called — name=%s endpoint=%s",
            self.plugin_name,
            self.plugin_endpoint,
        )
        return reg_pb2.PluginInfo(
            type="DRAPlugin",
            name=self.plugin_name,
            endpoint=self.plugin_endpoint,
            supported_versions=["v1beta1"],
        )

    async def NotifyRegistrationStatus(
        self,
        request: reg_pb2.RegistrationStatus,
        context: grpc.aio.ServicerContext,
    ) -> reg_pb2.RegistrationStatusResponse:
        """Handle registration status notification from kubelet."""
        if request.plugin_registered:
            logger.info("Plugin '%s' successfully registered with kubelet.", self.plugin_name)
        else:
            logger.error(
                "Plugin '%s' registration FAILED: %s",
                self.plugin_name,
                request.error,
            )
        return reg_pb2.RegistrationStatusResponse()

    # ------------------------------------------------------------------
    # DRA Node service implementation
    # ------------------------------------------------------------------

    async def NodePrepareResources(
        self,
        request: dra_pb2.NodePrepareResourcesRequest,
        context: grpc.aio.ServicerContext,
    ) -> dra_pb2.NodePrepareResourcesResponse:
        """Prepare hardware resources for the requested claims."""
        claims_result: dict = {}
        loop = asyncio.get_event_loop()

        for claim in request.claims:
            logger.info(
                "Preparing claim uid=%s name=%s namespace=%s",
                claim.uid,
                claim.name,
                claim.namespace,
            )
            try:
                cdi_device_ids: List[str] = await loop.run_in_executor(
                    None,
                    self.prepare_hardware,
                    claim.uid,
                    claim.namespace,
                    claim.name,
                )
                # Build the Device list; each CDI device ID maps to a Device entry.
                devices = [
                    dra_pb2.Device(cdi_device_ids=[cdi_id])
                    for cdi_id in cdi_device_ids
                ]
                claims_result[claim.uid] = dra_pb2.NodePrepareResourceResponse(
                    devices=devices
                )
                logger.info(
                    "Claim %s prepared — CDI IDs: %s",
                    claim.uid,
                    cdi_device_ids,
                )
            except Exception as exc:  # pylint: disable=broad-except
                logger.error(
                    "Failed to prepare claim %s: %s",
                    claim.uid,
                    exc,
                    exc_info=True,
                )
                claims_result[claim.uid] = dra_pb2.NodePrepareResourceResponse(
                    error=str(exc)
                )

        return dra_pb2.NodePrepareResourcesResponse(claims=claims_result)

    async def NodeUnprepareResources(
        self,
        request: dra_pb2.NodeUnprepareResourcesRequest,
        context: grpc.aio.ServicerContext,
    ) -> dra_pb2.NodeUnprepareResourcesResponse:
        """Release hardware resources for the given claims."""
        claims_result: dict = {}
        loop = asyncio.get_event_loop()

        for claim in request.claims:
            logger.info(
                "Unpreparing claim uid=%s name=%s namespace=%s",
                claim.uid,
                claim.name,
                claim.namespace,
            )
            try:
                await loop.run_in_executor(
                    None,
                    self.unprepare_hardware,
                    claim.uid,
                    claim.namespace,
                    claim.name,
                )
                claims_result[claim.uid] = dra_pb2.NodeUnprepareResourceResponse()
                logger.info("Claim %s unprepared successfully.", claim.uid)
            except Exception as exc:  # pylint: disable=broad-except
                logger.error(
                    "Failed to unprepare claim %s: %s",
                    claim.uid,
                    exc,
                    exc_info=True,
                )
                claims_result[claim.uid] = dra_pb2.NodeUnprepareResourceResponse(
                    error=str(exc)
                )

        return dra_pb2.NodeUnprepareResourcesResponse(claims=claims_result)

    # ------------------------------------------------------------------
    # Abstract hardware methods — override in subclasses
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def prepare_hardware(
        self,
        claim_uid: str,
        namespace: str,
        name: str,
    ) -> List[str]:
        """
        Allocate hardware for the given ResourceClaim.

        Args:
            claim_uid:  UID of the ResourceClaim (used as a unique key).
            namespace:  Namespace of the ResourceClaim.
            name:       Name of the ResourceClaim.

        Returns:
            A list of CDI device ID strings, e.g. ``["vendor.com/type=id"]``.

        Raises:
            Exception: Any exception will be caught and surfaced as a claim error.
        """

    @abc.abstractmethod
    def unprepare_hardware(
        self,
        claim_uid: str,
        namespace: str,
        name: str,
    ) -> None:
        """
        Release hardware previously allocated for the given ResourceClaim.

        Args:
            claim_uid:  UID of the ResourceClaim.
            namespace:  Namespace of the ResourceClaim.
            name:       Name of the ResourceClaim.

        Raises:
            Exception: Any exception will be caught and surfaced as a claim error.
        """

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    def _bind_uds_server(self, server: grpc.aio.Server, socket_path: str) -> None:
        """Ensure the socket directory exists, remove stale socket, then bind."""
        socket_dir = os.path.dirname(socket_path)
        if socket_dir:
            os.makedirs(socket_dir, exist_ok=True)
        if os.path.exists(socket_path):
            logger.warning("Removing stale socket: %s", socket_path)
            os.remove(socket_path)
        server.add_insecure_port(f"unix://{socket_path}")

    async def serve(self) -> None:
        """
        Start both gRPC servers and block until the Node server terminates.

        Start order:
        1. Node (DRA) server on ``plugin_socket_path``
        2. Registration server on ``registration_socket_path``

        The registration socket is created *after* the Node socket is ready so
        that by the time kubelet discovers and calls ``GetInfo``, the DRA
        endpoint is already accepting connections.
        """
        # --- Node (DRA) server ---
        node_server = grpc.aio.server()
        dra_grpc.add_DRAPluginServicer_to_server(self, node_server)
        self._bind_uds_server(node_server, self.plugin_socket_path)
        await node_server.start()
        logger.info("DRA Node server listening on %s", self.plugin_socket_path)

        # --- Registration server ---
        reg_server = grpc.aio.server()
        reg_grpc.add_RegistrationServicer_to_server(self, reg_server)
        self._bind_uds_server(reg_server, self.registration_socket_path)
        await reg_server.start()
        logger.info(
            "Registration server listening on %s (endpoint=%s)",
            self.registration_socket_path,
            self.plugin_endpoint,
        )

        logger.info("Plugin '%s' is running. Press Ctrl+C to stop.", self.plugin_name)
        try:
            await node_server.wait_for_termination()
        finally:
            logger.info("Shutting down servers...")
            await reg_server.stop(grace=5)
            await node_server.stop(grace=5)
            # Clean up sockets on exit.
            for path in (self.plugin_socket_path, self.registration_socket_path):
                if os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
