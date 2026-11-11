import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from victus_fan import paths, updater

REPO = "owner/repo"
CFG = {"enabled": True, "check_only": False, "repo": REPO, "branch": ""}


def fake_get(sha="b" * 40, fail=None):
    def _get(url, accept="application/vnd.github+json"):
        if fail:
            raise updater.UpdateError(fail)
        if url.endswith(f"/repos/{REPO}"):
            return json.dumps({"default_branch": "main"}).encode()
        if "/commits/" in url:
            return json.dumps({"sha": sha, "commit": {"message": "Neue Funktion\n\nDetails",
                                                      "committer": {"date": "2026-11-11T20:00:00Z"}}}).encode()
        raise AssertionError(url)
    return _get


class UpdaterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        patches = [
            mock.patch.object(paths, "INSTALL_INFO", self.tmp / "installed.json"),
            mock.patch.object(paths, "UPDATE_STATUS", self.tmp / "update_status.json"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def installed(self, sha):
        paths.INSTALL_INFO.write_text(json.dumps({"sha": sha}), encoding="utf-8")

    def test_current(self):
        self.installed("a" * 40)
        with mock.patch.object(updater, "_get", fake_get(sha="a" * 40)):
            status = updater.check_and_update(CFG)
        self.assertEqual(status["state"], "current")
        self.assertEqual(status["branch"], "main")

    def test_available_check_only(self):
        self.installed("a" * 40)
        with mock.patch.object(updater, "_get", fake_get()):
            status = updater.check_and_update(CFG, check_only=True)
        self.assertEqual(status["state"], "available")
        self.assertEqual(status["latest_message"], "Neue Funktion")
        saved = json.loads(paths.UPDATE_STATUS.read_text(encoding="utf-8"))
        self.assertEqual(saved["state"], "available")

    def test_unknown_install_counts_as_outdated(self):
        with mock.patch.object(updater, "_get", fake_get()):
            status = updater.check_and_update(CFG, check_only=True)
        self.assertEqual(status["state"], "available")

    def test_network_error(self):
        with mock.patch.object(updater, "_get", fake_get(fail="Keine Verbindung")):
            status = updater.check_and_update(CFG)
        self.assertEqual(status["state"], "error")
        self.assertIn("Keine Verbindung", status["message"])

    def test_install_runs_installer(self):
        self.installed("a" * 40)
        src = self.tmp / "src"
        with mock.patch.object(updater, "_get", fake_get()), \
                mock.patch.object(updater.sys, "platform", "win32"), \
                mock.patch.object(updater, "download_source", return_value=src) as dl, \
                mock.patch.object(updater, "run_installer") as inst:
            status = updater.check_and_update(CFG)
        self.assertEqual(status["state"], "updated")
        dl.assert_called_once()
        inst.assert_called_once_with(src, "b" * 40, "main")

    def test_failed_install_reports_error(self):
        self.installed("a" * 40)
        with mock.patch.object(updater, "_get", fake_get()), \
                mock.patch.object(updater.sys, "platform", "win32"), \
                mock.patch.object(updater, "download_source", side_effect=updater.UpdateError("kaputt")):
            status = updater.check_and_update(CFG)
        self.assertEqual(status["state"], "error")


def make_zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in entries:
            zf.writestr(name, "x")
    return buf.getvalue()


class DownloadTest(unittest.TestCase):
    def test_extracts_root(self):
        data = make_zip(["repo-abc/install/windows/install.ps1", "repo-abc/victus_fan/__init__.py"])
        with tempfile.TemporaryDirectory() as d, mock.patch.object(updater, "_get", return_value=data):
            root = updater.download_source(REPO, "abc", Path(d))
            self.assertEqual(root.name, "repo-abc")

    def test_rejects_path_traversal(self):
        data = make_zip(["repo-abc/install/windows/install.ps1", "repo-abc/../../evil.txt"])
        with tempfile.TemporaryDirectory() as d, mock.patch.object(updater, "_get", return_value=data):
            with self.assertRaises(updater.UpdateError):
                updater.download_source(REPO, "abc", Path(d))

    def test_rejects_archive_without_installer(self):
        data = make_zip(["repo-abc/README.md"])
        with tempfile.TemporaryDirectory() as d, mock.patch.object(updater, "_get", return_value=data):
            with self.assertRaises(updater.UpdateError):
                updater.download_source(REPO, "abc", Path(d))


if __name__ == "__main__":
    unittest.main()
