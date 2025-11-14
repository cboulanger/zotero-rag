import asyncio
import json
import logging
import socket
from typing import Optional

import websockets

logger = logging.getLogger(__name__)

def find_free_tcp_port() -> int:
    """Finds a free TCP port to be used by the debugger."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]

class RemoteFirefox:
    def __init__(self):
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.root_actor: Optional[str] = None
        self.addon_actor: Optional[str] = None
        self.message_id = 0
        self.pending_requests = {}

    async def connect(self, port: int, timeout: int = 30):
        uri = f"ws://127.0.0.1:{port}"
        try:
            self.ws = await asyncio.wait_for(websockets.connect(uri), timeout=timeout)
            asyncio.create_task(self._listen())
            
            # The first message from the server contains the root actor
            initial_message = await self._wait_for_message_type('root')
            self.root_actor = initial_message.get('actor')
            if not self.root_actor:
                raise ConnectionError("Could not get root actor from RDP server.")
            
            # Get the addon actor
            response = await self._send_request({
                "to": self.root_actor,
                "type": "getAddons"
            })
            self.addon_actor = response.get('addonsActor')
            if not self.addon_actor:
                raise ConnectionError("Could not get addon actor from RDP server.")

        except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed) as e:
            raise ConnectionError(f"Failed to connect to Zotero RDP server at {uri}: {e}")

    async def _listen(self):
        """Listens for incoming messages and resolves pending futures."""
        async for message in self.ws:
            data = json.loads(message)
            from_actor = data.get('from')
            
            if from_actor in self.pending_requests:
                future = self.pending_requests.pop(from_actor, None)
                if future and not future.done():
                    future.set_result(data)
            else:
                # Handle unsolicited messages if needed, or just log them
                logger.debug(f"Received unsolicited message: {data}")

    async def _wait_for_message_type(self, msg_type: str, timeout: int = 10):
        """Waits for a specific type of message not tied to a request."""
        async for message in self.ws:
            data = json.loads(message)
            if data.get('type') == msg_type:
                return data
        raise asyncio.TimeoutError(f"Timed out waiting for message of type '{msg_type}'")

    async def _send_request(self, payload: dict) -> dict:
        """Sends a request and waits for a response."""
        self.message_id += 1
        payload['from'] = f"client{self.message_id}"
        
        future = asyncio.get_event_loop().create_future()
        self.pending_requests[payload['from']] = future
        
        await self.ws.send(json.dumps(payload))
        
        try:
            response = await asyncio.wait_for(future, timeout=10)
            if 'error' in response:
                raise RuntimeError(f"RDP Error: {response.get('message')}")
            return response
        except asyncio.TimeoutError:
            self.pending_requests.pop(payload['from'], None)
            raise TimeoutError(f"RDP request timed out: {payload}")

    async def install_temporary_addon(self, path: str) -> dict:
        """Installs a temporary addon from a given path."""
        if not self.addon_actor:
            raise ConnectionError("Not connected or addon actor not found.")
        
        response = await self._send_request({
            "to": self.addon_actor,
            "type": "installTemporaryAddon",
            "addonPath": path
        })
        return response

    async def reload_addon(self, addon_id: str):
        """Reloads an installed addon by its ID."""
        if not self.addon_actor:
            raise ConnectionError("Not connected or addon actor not found.")
            
        await self._send_request({
            "to": self.addon_actor,
            "type": "reload",
            "id": addon_id
        })