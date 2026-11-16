"""Updater tests: safe apply order and the writable-install preflight.

Run with::

    python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from tgwsproxy.updater import (
    DEFAULT_REPO,
    UpdateError,
    UpdateInfo,
    _generate_apply_script,
    _probe_writable,
    prepare_update,
)


def _info() -> UpdateInfo:
    return UpdateInfo(
        current="1.6.0",
        latest="1.6.1",
        available=True,
        channel="release",
        tarball_url="https://example.invalid/tarball",
        published_at="",
        notes="",
    )


class UpdateRepoTest(unittest.TestCase):
    def test_default_repo_is_the_keenetic_fork(self):
        self.assertEqual(DEFAULT_REPO, "mxshooter/tg-ws-keenetic")


class ProbeWritableTest(unittest.TestCase):
    def test_true_for_writable_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(_probe_writable(Path(tmp)))
            self.assertEqual(
                [p for p in os.listdir(tmp) if p.startswith(".tgwsproxy")], []
            )

    def test_false_for_missing_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(_probe_writable(Path(tmp) / "missing"))

    @unittest.skipIf(
        getattr(os, "geteuid", lambda: 1)() == 0,
        "root bypasses directory permissions",
    )
    def test_false_for_read_only_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            path.chmod(stat.S_IRUSR | stat.S_IXUSR)
            try:
                self.assertFalse(_probe_writable(path))
            finally:
                path.chmod(stat.S_IRWXU)


class PrepareUpdateTest(unittest.TestCase):
    def test_refuses_missing_install_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "staging"
            with self.assertRaises(UpdateError) as ctx:
                prepare_update(
                    _info(),
                    install_root=str(Path(tmp) / "no-such-install"),
                    staging_root=str(staging),
                )
            self.assertIn("does not exist", str(ctx.exception))
            self.assertFalse(staging.exists())

    @unittest.skipIf(
        getattr(os, "geteuid", lambda: 1)() == 0,
        "root bypasses directory permissions",
    )
    def test_refuses_read_only_install_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = Path(tmp) / "install"
            install.mkdir()
            install.chmod(stat.S_IRUSR | stat.S_IXUSR)
            try:
                with self.assertRaises(UpdateError) as ctx:
                    prepare_update(
                        _info(),
                        install_root=str(install),
                        staging_root=str(Path(tmp) / "staging"),
                    )
                self.assertIn("no write access", str(ctx.exception))
            finally:
                install.chmod(stat.S_IRWXU)


class ApplyScriptTest(unittest.TestCase):
    def test_write_access_is_checked_before_stopping_the_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = _generate_apply_script(
                staging_root=Path(tmp),
                install_root=Path("/opt/share/tgwsproxy"),
                init_script="/opt/etc/init.d/S99tgwsproxy",
                log_file=Path(tmp) / "update.log",
            )
            text = script.read_text(encoding="utf-8")
            probe = text.index(".tgwsproxy-update-probe")
            stop = text.index('"$INIT" stop')
            self.assertLess(probe, stop)

    def test_failures_after_stop_restore_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = _generate_apply_script(
                staging_root=Path(tmp),
                install_root=Path("/opt/share/tgwsproxy"),
                init_script="/opt/etc/init.d/S99tgwsproxy",
                log_file=Path(tmp) / "update.log",
            )
            text = script.read_text(encoding="utf-8")
            self.assertIn("fail_after_stop()", text)
            self.assertIn(
                'mkdir -p "$BACKUP" || fail_after_stop', text
            )
            self.assertIn(
                '"$INIT" start >> "$LOG" 2>&1 || log "restart after rollback failed"',
                text,
            )


if __name__ == "__main__":
    unittest.main()
