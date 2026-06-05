import asyncio
import fcntl
import pytest
from unittest.mock import MagicMock, patch, mock_open

from pydra.core.server import DraNodeServer
from pydra.core.generated.dra import dra_pb2 as dra_pb2

class DummyDriver(DraNodeServer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.prepare_called = False
        self.unprepare_called = False
        self.prepare_sleep = 0
    
    def get_devices(self):
        return [{"name": "0", "attributes": {}}]

    async def prepare_hardware(self, claim_uid, namespace, name):
        self.prepare_called = True
        if self.prepare_sleep:
            await asyncio.sleep(self.prepare_sleep)
        return [{"cdi_id": "dummy.com/device=0", "metadata": {"ip": "1.2.3.4"}}]

    async def unprepare_hardware(self, claim_uid, namespace, name):
        self.unprepare_called = True

@pytest.fixture
def server():
    return DummyDriver("dummy.com/device", "/tmp/dummy.sock", enable_device_metadata=True, cdi_directory="/tmp/cdi")

def test_file_lock(server):
    async def run_test():
        with patch("pydra.core.server.os.makedirs"), \
             patch("pydra.core.server.open", mock_open()), \
             patch("pydra.core.server.fcntl.flock") as mock_flock:
            
            async with server._lock():
                # Inside the lock
                assert mock_flock.call_count == 1
                args, kwargs = mock_flock.call_args_list[0]
                assert args[1] == fcntl.LOCK_EX
                
            # Outside the lock
            assert mock_flock.call_count == 2
            args, kwargs = mock_flock.call_args_list[1]
            assert args[1] == fcntl.LOCK_UN

    asyncio.run(run_test())

def test_node_prepare_metadata_generation(server):
    async def run_test():
        request = MagicMock()
        claim = MagicMock()
        claim.uid = "claim-123"
        claim.namespace = "default"
        claim.name = "test-claim"
        request.claims = [claim]
        
        context = MagicMock()
        context.is_active.return_value = True
        
        with patch("pydra.core.server.os.makedirs"), \
             patch("pydra.core.server.open", mock_open()) as mock_file, \
             patch("pydra.core.server.fcntl.flock"):
            
            response = await server.NodePrepareResources(request, context)
            
            assert server.prepare_called
            assert len(response.claims) == 1
            assert "claim-123" in response.claims
            res = response.claims["claim-123"]
            assert len(res.devices) == 1
            assert res.devices[0].cdi_device_ids[0] == "dummy.com/device/metadata=claim-123-test-claim"

            # 1 file for lock, 1 file for metadata, 1 file for CDI spec = 3
            assert mock_file.call_count == 3
            
    asyncio.run(run_test())

def test_node_prepare_cancellation(server):
    async def run_test():
        server.prepare_sleep = 2 # simulate long running task
        
        request = MagicMock()
        claim = MagicMock()
        claim.uid = "claim-123"
        request.claims = [claim]
        
        context = MagicMock()
        context.is_active.return_value = False
        
        callback = None
        def mock_add_done_callback(cb):
            nonlocal callback
            callback = cb
        context.add_done_callback = mock_add_done_callback
        
        with patch("pydra.core.server.os.makedirs"), \
             patch("pydra.core.server.open", mock_open()), \
             patch("pydra.core.server.fcntl.flock"):
             
            prepare_task = asyncio.create_task(server.NodePrepareResources(request, context))
            await asyncio.sleep(0.1) 
            
            assert callback is not None
            callback()
            
            response = await prepare_task
            assert response.claims["claim-123"].error == "cancelled"

    asyncio.run(run_test())

def test_watch_reconciliation():
    server = DummyDriver("dummy.com/device", "/tmp/dummy.sock")
    
    with patch("kubernetes.client.ResourceV1Api"), \
         patch("kubernetes.watch.Watch") as mock_watch, \
         patch("kubernetes.config.load_incluster_config"):
        
        mock_watch_instance = mock_watch.return_value
        def fake_stream(*args, **kwargs):
            server._stop_event.set()
            return []
        mock_watch_instance.stream.side_effect = fake_stream
        
        server._watch_resource_slice()
        
        assert server.k8s_api is not None
        server.k8s_api.read_resource_slice.assert_called_once()
