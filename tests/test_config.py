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


class MultiDomainTest(unittest.TestCase):
    def test_defaults_are_empty_lists(self):
        cfg = Config()
        self.assertEqual(cfg.cfproxy_user_domains, [])
        self.assertEqual(cfg.cfproxy_worker_domains, [])

    def test_legacy_singular_keys_are_migrated(self):
        cfg = _from_dict(
            {
                "cfproxy_user_domain": "my.example.com",
                "cfproxy_worker_domain": "my-worker.workers.dev",
            }
        )
        self.assertEqual(cfg.cfproxy_user_domains, ["my.example.com"])
        self.assertEqual(
            cfg.cfproxy_worker_domains, ["my-worker.workers.dev"]
        )

    def test_plural_value_wins_over_legacy_key(self):
        cfg = _from_dict(
            {
                "cfproxy_user_domain": "old.example.com",
                "cfproxy_user_domains": ["new.example.com"],
            }
        )
        self.assertEqual(cfg.cfproxy_user_domains, ["new.example.com"])

    def test_empty_plural_value_still_uses_legacy_key(self):
        cfg = _from_dict(
            {
                "cfproxy_user_domain": "old.example.com",
                "cfproxy_user_domains": [],
            }
        )
        self.assertEqual(cfg.cfproxy_user_domains, ["old.example.com"])

    def test_string_value_is_split_on_commas_and_whitespace(self):
        cfg = _from_dict(
            {
                "cfproxy_user_domains": (
                    "a.example.com, b.example.com;c.example.com d.example.com"
                )
            }
        )
        self.assertEqual(
            cfg.cfproxy_user_domains,
            [
                "a.example.com",
                "b.example.com",
                "c.example.com",
                "d.example.com",
            ],
        )

    def test_list_entries_may_contain_several_domains(self):
        cfg = _from_dict(
            {
                "cfproxy_worker_domains": [
                    "a.workers.dev, b.workers.dev",
                    "c.workers.dev",
                ]
            }
        )
        self.assertEqual(
            cfg.cfproxy_worker_domains,
            ["a.workers.dev", "b.workers.dev", "c.workers.dev"],
        )

    def test_duplicates_are_dropped_case_insensitively(self):
        cfg = _from_dict(
            {
                "cfproxy_user_domains": [
                    "A.example.com",
                    "a.example.com",
                    "b.example.com",
                ]
            }
        )
        self.assertEqual(
            cfg.cfproxy_user_domains, ["A.example.com", "b.example.com"]
        )

    def test_unusable_values_become_an_empty_list(self):
        cfg = _from_dict(
            {
                "cfproxy_user_domains": None,
                "cfproxy_worker_domains": 42,
            }
        )
        self.assertEqual(cfg.cfproxy_user_domains, [])
        self.assertEqual(cfg.cfproxy_worker_domains, [])

    def test_validate_rejects_invalid_domains(self):
        cfg = Config()
        cfg.cfproxy_user_domains = ["not a domain"]
        cfg.cfproxy_worker_domains = ["https://worker.example.com"]
        errors = cfg.validate()
        self.assertTrue(any("CF proxy domain" in e for e in errors))
        self.assertTrue(any("CF worker domain" in e for e in errors))

    def test_validate_accepts_several_valid_domains(self):
        cfg = Config()
        cfg.cfproxy_user_domains = ["a.example.com", "b.example.com"]
        cfg.cfproxy_worker_domains = ["my-worker.workers.dev"]
        self.assertEqual(cfg.validate(), [])

    def test_to_dict_uses_only_the_plural_keys(self):
        cfg = _from_dict({"cfproxy_user_domain": "legacy.example.com"})
        data = cfg.to_dict()
        self.assertEqual(data["cfproxy_user_domains"], ["legacy.example.com"])
        self.assertNotIn("cfproxy_user_domain", data)
        self.assertNotIn("cfproxy_worker_domain", data)


if __name__ == "__main__":
    unittest.main()
