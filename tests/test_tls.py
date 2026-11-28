"""Tests for shared TLS context creation.

Run with::

    python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import ssl
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tgwsproxy import websocket


class CreateContextTest(unittest.TestCase):
    def test_verified_context_checks_certificates(self):
        context = websocket.create_ssl_context(check_hostname=True, verify=True)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_default_context_stays_unverified(self):
        context = websocket.create_ssl_context()
        self.assertEqual(context.verify_mode, ssl.CERT_NONE)
        self.assertFalse(context.check_hostname)

    def test_broken_system_store_does_not_prevent_startup(self):
        with patch.object(
            websocket.ssl,
            "create_default_context",
            side_effect=OSError("no CA store"),
        ):
            verified = websocket.create_ssl_context(
                check_hostname=True, verify=True
            )
            plain = websocket.create_ssl_context()

        self.assertEqual(verified.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(verified.check_hostname)
        self.assertEqual(plain.verify_mode, ssl.CERT_NONE)
        self.assertFalse(plain.check_hostname)

    def test_certifi_bundle_is_added_to_the_context(self):
        context = Mock()
        certifi = SimpleNamespace(where=lambda: "/opt/share/cacert.pem")
        with patch.dict(sys.modules, {"certifi": certifi}):
            websocket._load_ca_bundle(context)

        context.load_verify_locations.assert_called_once_with(
            cafile="/opt/share/cacert.pem"
        )

    def test_missing_certifi_is_ignored(self):
        context = Mock()
        with patch.dict(sys.modules, {"certifi": None}):
            websocket._load_ca_bundle(context)

        context.load_verify_locations.assert_not_called()

    def test_broken_certifi_bundle_is_ignored(self):
        context = Mock()
        context.load_verify_locations.side_effect = FileNotFoundError("gone")
        certifi = SimpleNamespace(where=lambda: "/missing/cacert.pem")
        with patch.dict(sys.modules, {"certifi": certifi}):
            websocket._load_ca_bundle(context)

        context.load_verify_locations.assert_called_once()


if __name__ == "__main__":
    unittest.main()
