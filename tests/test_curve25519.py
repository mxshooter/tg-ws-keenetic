"""Tests for the pure-Python X25519 fallback and the forged key share.

Run with::

    python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import os
import unittest

from tgwsproxy import fake_tls
from tgwsproxy._curve25519 import (
    P,
    is_valid_point,
    x25519,
    x25519_public_key,
)

# RFC 7748, section 6.1 (Alice and Bob).
ALICE_PRIVATE = bytes.fromhex(
    "77076d0a7318a57d3c16c17251b26645"
    "df4c2f87ebc0992ab177fba51db92c2a"
)
ALICE_PUBLIC = bytes.fromhex(
    "8520f0098930a754748b7ddcb43ef75a"
    "0dbf3a0d26381af4eba4a98eaa9b4e6a"
)
BOB_PRIVATE = bytes.fromhex(
    "5dab087e624a8a4b79e17f8b83800ee6"
    "6f3bb1292618b6fd1c2f8b27ff88e0eb"
)
BOB_PUBLIC = bytes.fromhex(
    "de9edb7d7b7dc1b4d35b61c2ece43537"
    "3f8343c85b78674dadfc7e146f882b4f"
)
SHARED = bytes.fromhex(
    "4a5d9d5ba4ce2de1728e3bf480350f25"
    "e07e21c947d19e3376f09b3c1e161742"
)

# RFC 7748, section 5.2 (single iteration).
LADDER_SCALAR = bytes.fromhex(
    "09000000000000000000000000000000"
    "00000000000000000000000000000000"
)
LADDER_POINT = LADDER_SCALAR
LADDER_RESULT = bytes.fromhex(
    "422c8e7a6227d7bca1350b3e2bb7279f"
    "7897b87bb6854b783c60e80311ae3079"
)


class RfcVectorsTest(unittest.TestCase):
    def test_alice_and_bob_public_keys(self):
        self.assertEqual(x25519_public_key(ALICE_PRIVATE), ALICE_PUBLIC)
        self.assertEqual(x25519_public_key(BOB_PRIVATE), BOB_PUBLIC)

    def test_shared_secret_is_symmetric(self):
        self.assertEqual(x25519(ALICE_PRIVATE, BOB_PUBLIC), SHARED)
        self.assertEqual(x25519(BOB_PRIVATE, ALICE_PUBLIC), SHARED)

    def test_single_ladder_iteration(self):
        self.assertEqual(x25519(LADDER_SCALAR, LADDER_POINT), LADDER_RESULT)

    def test_high_bit_of_point_is_ignored(self):
        masked = bytearray(BOB_PUBLIC)
        masked[31] |= 0x80
        self.assertEqual(x25519(ALICE_PRIVATE, bytes(masked)), SHARED)


class GeneratedKeyTest(unittest.TestCase):
    def test_random_public_keys_are_valid_curve_points(self):
        for _ in range(25):
            self.assertTrue(is_valid_point(x25519_public_key(os.urandom(32))))

    def test_public_keys_differ_between_calls(self):
        keys = {x25519_public_key(os.urandom(32)) for _ in range(10)}
        self.assertEqual(len(keys), 10)

    def test_curve_equation_rejects_random_bytes(self):
        # roughly half of random 32-byte strings are not x-coordinates
        invalid = 0
        for _ in range(20):
            if not is_valid_point(os.urandom(32)):
                invalid += 1
        self.assertGreater(invalid, 0)

    def test_prime_matches_curve25519(self):
        self.assertEqual(P, 2**255 - 19)


class FakeTlsKeyShareTest(unittest.TestCase):
    def setUp(self):
        self._saved = fake_tls.X25519PrivateKey

    def tearDown(self):
        fake_tls.X25519PrivateKey = self._saved

    def test_fallback_without_cryptography_is_a_real_point(self):
        fake_tls.X25519PrivateKey = None
        for _ in range(5):
            self.assertTrue(is_valid_point(fake_tls._x25519_public_key()))

    def test_reports_the_backend_in_use(self):
        fake_tls.X25519PrivateKey = None
        self.assertEqual(fake_tls.x25519_backend(), "pure-python")

    @unittest.skipUnless(
        fake_tls.X25519PrivateKey is not None, "needs cryptography"
    )
    def test_cryptography_path_matches_pure_python(self):
        private_key = os.urandom(32)
        expected = (
            fake_tls.X25519PrivateKey.from_private_bytes(private_key)
            .public_key()
            .public_bytes_raw()
        )
        self.assertEqual(x25519_public_key(private_key), expected)


if __name__ == "__main__":
    unittest.main()
