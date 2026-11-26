"""Tests for CF front domain dialing over TLS and plain HTTP.

Run with::

    python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from tgwsproxy import cf_websocket
from tgwsproxy.websocket import RawWebSocket, WsHandshakeError


class CfDomainTimeoutTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cf_websocket.reset_cf_timeouts()
        self.addCleanup(cf_websocket.reset_cf_timeouts)

    async def connect(self, *hosts, secure=True):
        return await cf_websocket.connect_cf_domains(
            ((host, host) for host in hosts),
            secure=secure,
            label="test",
        )

    async def test_clients_dial_in_parallel_and_http_responses_do_not_throttle(self):
        statuses = iter((101, 429, 101, 503, 429))
        started = asyncio.Queue()
        release = asyncio.Event()

        async def dial(host, *args, **kwargs):
            if host == "preferred":
                status = next(statuses)
                started.put_nowait(host)
                await release.wait()
                if status != 101:
                    raise WsHandshakeError(status, "unavailable")
            return object()

        with patch.object(RawWebSocket, "connect", side_effect=dial):
            clients = [
                asyncio.create_task(self.connect("preferred", "other"))
                for _ in range(5)
            ]
            try:
                for _ in clients:
                    await asyncio.wait_for(started.get(), 1)
                release.set()
                results = await asyncio.gather(*clients)
                self.assertCountEqual(
                    [name for _, name in results],
                    ["preferred"] * 2 + ["other"] * 3,
                )
            finally:
                for task in clients:
                    task.cancel()
                await asyncio.gather(*clients, return_exceptions=True)

        with patch.object(RawWebSocket, "connect", return_value=object()):
            self.assertEqual((await self.connect("preferred"))[1], "preferred")

    async def test_five_timeouts_cover_five_domains_and_cooldown_is_per_mode(self):
        hosts = ("a", "http-error", "b", "c", "d", "e", "f")
        responses = [
            asyncio.TimeoutError(),
            WsHandshakeError(503, "unavailable"),
            TimeoutError(),
            TimeoutError(),
            TimeoutError(),
            TimeoutError(),
            WsHandshakeError(429, "busy"),
            object(),
        ]
        with patch.object(
            RawWebSocket, "connect", side_effect=responses
        ) as dial:
            self.assertIsNone(await self.connect(*hosts))
            self.assertEqual((await self.connect(*hosts))[1], "b")
            self.assertEqual(
                [
                    (
                        call.args[0],
                        call.kwargs["secure"],
                        call.kwargs["timeout"],
                    )
                    for call in dial.await_args_list
                ],
                [
                    ("a", True, 3),
                    ("http-error", False, 3),
                    ("b", False, 3),
                    ("c", True, 5),
                    ("d", False, 5),
                    ("e", True, 10),
                    ("http-error", True, 3),
                    ("b", True, 3),
                ],
            )

    async def test_timeout_retries_same_domain_and_successes_set_first_mode(self):
        ws = object()
        with patch.object(
            RawWebSocket,
            "connect",
            side_effect=[TimeoutError(), ws, ws, TimeoutError(), ws],
        ) as dial:
            self.assertEqual(await self.connect("a"), (ws, "a"))
            self.assertEqual(await self.connect("b"), (ws, "b"))
            self.assertEqual(await self.connect("b"), (ws, "b"))
            self.assertEqual(
                [
                    (call.args[0], call.kwargs["secure"])
                    for call in dial.await_args_list
                ],
                [
                    ("a", True),
                    ("a", False),
                    ("b", False),
                    ("b", False),
                    ("b", True),
                ],
            )

    async def test_single_domain_tries_other_mode_on_timeout_or_cooldown(self):
        ws = object()
        responses = [
            WsHandshakeError(503, "unavailable"),
            TimeoutError(),
            WsHandshakeError(503, "unavailable"),
            ws,
        ]
        with patch.object(
            RawWebSocket, "connect", side_effect=responses
        ) as dial:
            self.assertIsNone(await self.connect("a"))
            self.assertIsNone(await self.connect("a"))
            self.assertEqual(await self.connect("a"), (ws, "a"))
            self.assertEqual(
                [call.kwargs["secure"] for call in dial.await_args_list],
                [True, True, False, False],
            )

    async def test_timeout_retry_skips_to_other_front_without_waiting(self):
        with patch.object(cf_websocket, "CF_TIMEOUT_COOLDOWN", 0), patch.object(
            RawWebSocket, "connect", side_effect=asyncio.TimeoutError()
        ):
            self.assertIsNone(await self.connect("a", secure=False))

        started, release = asyncio.Event(), asyncio.Event()

        async def dial(host, *args, **kwargs):
            if host == "a":
                started.set()
                await release.wait()
                raise WsHandshakeError(429, "busy")
            return object()

        with patch.object(
            RawWebSocket, "connect", side_effect=dial
        ) as connect:
            retry = asyncio.create_task(self.connect("a", secure=False))
            try:
                await asyncio.wait_for(started.wait(), 1)
                result = await asyncio.wait_for(
                    self.connect("a", "b", secure=False), 1
                )
                self.assertEqual(result[1], "b")
                self.assertEqual(
                    [
                        (call.args[0], call.kwargs["secure"])
                        for call in connect.await_args_list
                    ],
                    [("a", False), ("b", False)],
                )
            finally:
                release.set()
                await retry

        with patch.object(RawWebSocket, "connect", return_value=object()):
            self.assertEqual(
                (await self.connect("a", secure=False))[1], "a"
            )

    async def test_cancelling_timeout_retry_releases_it_for_next_client(self):
        with patch.object(cf_websocket, "CF_TIMEOUT_COOLDOWN", 0), patch.object(
            RawWebSocket, "connect", side_effect=asyncio.TimeoutError()
        ):
            await self.connect("a")

        started = asyncio.Event()

        async def dial(*args, **kwargs):
            started.set()
            await asyncio.Future()

        with patch.object(RawWebSocket, "connect", side_effect=dial):
            retry = asyncio.create_task(self.connect("a"))
            await asyncio.wait_for(started.wait(), 1)
            retry.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await retry

        with patch.object(
            RawWebSocket, "connect", return_value=object()
        ) as connect:
            self.assertEqual((await self.connect("a"))[1], "a")
            connect.assert_awaited_once_with(
                "a", "a", path="/apiws", secure=True, timeout=3.0
            )

    async def test_tls_cooldown_is_shared_and_mode_specific(self):
        with patch.object(
            RawWebSocket, "connect", side_effect=asyncio.TimeoutError()
        ):
            self.assertIsNone(await self.connect("a"))

        self.assertTrue(cf_websocket.cf_tls_in_cooldown("a"))
        self.assertFalse(cf_websocket.cf_tls_in_cooldown("b"))

        with patch("tgwsproxy.cf_websocket.time") as clock:
            clock.monotonic.return_value = (
                cf_websocket._cf_timeout_until[("a", True)] + 1
            )
            self.assertFalse(cf_websocket.cf_tls_in_cooldown("a"))

    async def test_reset_clears_mode_statistics(self):
        with patch.object(RawWebSocket, "connect", return_value=object()):
            await self.connect("a", secure=False)

        self.assertFalse(cf_websocket.cf_prefers_tls())
        cf_websocket.reset_cf_timeouts()
        self.assertTrue(cf_websocket.cf_prefers_tls())


if __name__ == "__main__":
    unittest.main()
