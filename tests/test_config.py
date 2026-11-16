"""Config tests: defaults, tolerant parsing and the update_repo migration.

Run with::

    python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import unittest

from tgwsproxy.config import (
    DEFAULT_UPDATE_REPO,
    Config,
    _from_dict,
)


class UpdateRepoTest(unittest.TestCase):
    def test_default_points_at_the_keenetic_fork(self):
        self.assertEqual(DEFAULT_UPDATE_REPO, "mxshooter/tg-ws-keenetic")
        self.assertEqual(Config().update_repo, DEFAULT_UPDATE_REPO)

    def test_legacy_parent_repo_is_migrated(self):
        # The parent project publishes Rust releases without a `tgwsproxy/`
        # payload, so an old default must not survive a config load.
        cfg = _from_dict({"update_repo": "Omn1z/tg-ws-keenetic"})
        self.assertEqual(cfg.update_repo, DEFAULT_UPDATE_REPO)

    def test_legacy_repo_match_is_case_insensitive(self):
        cfg = _from_dict({"update_repo": "omn1z/TG-WS-KEENETIC"})
        self.assertEqual(cfg.update_repo, DEFAULT_UPDATE_REPO)

    def test_custom_repo_is_preserved(self):
        cfg = _from_dict({"update_repo": "someone/else"})
        self.assertEqual(cfg.update_repo, "someone/else")

    def test_missing_repo_uses_default(self):
        self.assertEqual(_from_dict({}).update_repo, DEFAULT_UPDATE_REPO)

    def test_blank_repo_uses_default(self):
        self.assertEqual(
            _from_dict({"update_repo": "  "}).update_repo, DEFAULT_UPDATE_REPO
        )


class ConfigParsingTest(unittest.TestCase):
    def test_missing_keys_do_not_break_loading(self):
        cfg = _from_dict({"port": 1443})
        self.assertEqual(cfg.port, 1443)
        self.assertEqual(cfg.web_port, Config().web_port)
        self.assertTrue(cfg.secret)
        self.assertEqual(len(cfg.secret), 32)

    def test_dc_redirects_accept_string_keys(self):
        cfg = _from_dict({"dc_redirects": {"2": "149.154.167.220"}})
        self.assertEqual(cfg.dc_redirects, {2: "149.154.167.220"})


if __name__ == "__main__":
    unittest.main()
