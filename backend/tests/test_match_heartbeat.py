"""El dispatcher no debe perder una conexión mientras Groq espera cuota."""

import asyncio
import unittest
from unittest.mock import patch

from fastapi import WebSocketDisconnect

from app.api.v1.endpoints.matches import match_socket


class FakeSocket:
    headers = {'authorization': 'Bearer synthetic'}

    def __init__(self):
        self.sent = []
        self.reads = 0

    async def accept(self):
        pass

    async def send_json(self, message):
        self.sent.append(message)

    async def receive_json(self):
        self.reads += 1
        if self.reads == 1:
            return {'type': 'match.resume', 'request_id': 'synthetic-request'}
        if len(self.sent) >= 2:
            raise WebSocketDisconnect()
        await asyncio.sleep(10)


class MatchHeartbeatTests(unittest.TestCase):
    def test_processing_job_sends_heartbeat_before_dispatcher_timeout(self):
        socket = FakeSocket()
        loop = asyncio.SelectorEventLoop()
        try:
            with patch('app.api.v1.endpoints.matches.is_valid_service_token', return_value=True), \
                 patch('app.api.v1.endpoints.matches.MatchJobStore') as store, \
                 patch('app.api.v1.endpoints.matches.MATCH_WEBSOCKET_HEARTBEAT_SECONDS', .01):
                store.return_value.get.return_value = {'status': 'processing'}
                loop.run_until_complete(asyncio.wait_for(match_socket(socket), timeout=2))
        finally:
            loop.close()
        self.assertEqual([message['type'] for message in socket.sent], ['match.processing', 'match.heartbeat'])
