import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from victus_fan import paths, updater

CFG = {"enabled": True, "check_only": False}


def fake_get(tag="v2.3.0", sha="b" * 40, fail=None, release=True, tags=None, branch_sha="c" * 40):
    """Nachgebaute GitHub-API: letztes Release, Tag-Liste und Commits."""
    def _get(url, accept="application/vnd.github+json"):
        if fail:
            raise updater.UpdateError(fail)
        if url.endswith("/releases/latest"):
            if not release:
                raise updater.UpdateError(f"Nicht gefunden: {url}")
            return json.dumps({"tag_name": tag, "name": f"Version {tag[1:]}",
                               "published_at": "2026-11-12T09:00:00Z"}).encode()
        if "/tags" in url:
            return json.dumps([{"name": t} for t in (tags if tags is not None else [tag])]).encode()
        if "/commits/" in url:
            ref = url.rsplit("/", 1)[1]
            commit_sha = branch_sha if ref == "main" else sha
            return json.dumps({"sha": commit_sha, "commit": {"message": "Neue Funktion\n\nDetails",
                                                             "committer": {"date": "2026-11-12T08:00:00Z"}}}).encode()
        raise AssertionError(url)
    return _get


class UpdaterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        patches = [
            mock.patch.object(paths, "INSTALL_INFO", self.tmp / "installed.json"),
            mock.patch.object(paths, "UPDATE_STATUS", self.tmp / "update_status.json"),
            mock.patch.object(paths, "UPDATE_BRANCH_FILE", self.tmp / "update-branch.txt"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def installed(self, version, sha="a" * 40):
        paths.INSTALL_INFO.write_text(json.dumps({"version": version, "sha": sha}), encoding="utf-8")

    def check(self, cfg=CFG, branch="", **get_args):
        with mock.patch.object(updater, "_get", fake_get(**get_args)):
            return updater.check_and_update(cfg, check_only=True, branch=branch)

    def test_newer_tag_is_offered(self):
        self.installed("2.2.0")
        status = self.check()
        self.assertEqual(status["state"], "available")
        self.assertEqual(status["latest_version"], "2.3.0")
        self.assertEqual(status["branch"], "v2.3.0")

    def test_same_version_is_current(self):
        self.installed("2.3.0", sha="b" * 40)
        self.assertEqual(self.check()["state"], "current")

    def test_never_downgrades(self):
        self.installed("2.4.0")                      # z. B. von Hand neuer installiert
        self.assertEqual(self.check()["state"], "current")

    def test_untagged_commits_on_main_are_ignored(self):
        self.installed("2.3.0", sha="b" * 40)
        self.assertEqual(self.check(branch_sha="d" * 40)["state"], "current")

    def test_falls_back_to_highest_tag(self):
        self.installed("2.2.0")
        status = self.check(release=False, tags=["v2.10.0", "v2.9.1", "test", "v2.3.0"])
        self.assertEqual(status["latest_version"], "2.10.0")   # numerisch, nicht alphabetisch

    def test_no_tags_yet(self):
        self.installed("2.2.0")
        status = self.check(release=False, tags=[])
        self.assertEqual(status["state"], "error")
        self.assertIn("Noch keine freigegebene Version", status["message"])

    def test_branch_mode_follows_commits(self):
        self.installed("2.3.0", sha="b" * 40)
        status = self.check(branch="main")
        self.assertEqual(status["state"], "available")
        self.assertEqual(status["latest_sha"], "c" * 40)

    def test_source_never_comes_from_config(self):
        # Die config.toml dürfen normale Benutzer bearbeiten - der als SYSTEM laufende
        # Updater darf daraus weder Repository noch Branch übernehmen.
        self.installed("2.3.0", sha="b" * 40)
        urls = []
        get = fake_get()

        def spy(url, accept="application/vnd.github+json"):
            urls.append(url)
            return get(url, accept)
        with mock.patch.object(updater, "_get", spy):
            status = updater.check_and_update(dict(CFG, repo="evil/repo", branch="main"), check_only=True)
        self.assertEqual(status["state"], "current")
        self.assertTrue(urls and all(updater.REPO in u for u in urls))

    def test_test_branch_file(self):
        self.assertEqual(updater.test_branch(), "")
        updater.set_test_branch("claude/test-1")
        self.assertEqual(updater.test_branch(), "claude/test-1")
        paths.UPDATE_BRANCH_FILE.write_text("../../x", encoding="utf-8")
        self.assertEqual(updater.test_branch(), "")      # kein gültiger Branch-Name
        updater.set_test_branch("")
        self.assertFalse(paths.UPDATE_BRANCH_FILE.exists())

    def test_network_error(self):
        status = self.check(fail="Keine Verbindung")
        self.assertEqual(status["state"], "error")
        self.assertIn("Keine Verbindung", status["message"])

    def test_install_runs_installer_with_tag(self):
        self.installed("2.2.0")
        src = self.tmp / "src"
        with mock.patch.object(updater, "_get", fake_get()), \
                mock.patch.object(updater.sys, "platform", "win32"), \
                mock.patch.object(updater, "download_source", return_value=src) as dl, \
                mock.patch.object(updater, "run_installer") as inst:
            status = updater.check_and_update(CFG)
        self.assertEqual(status["state"], "updated")
        dl.assert_called_once_with(updater.REPO, "b" * 40, mock.ANY)
        inst.assert_called_once_with(src, "b" * 40, "v2.3.0")

    def test_failed_install_reports_error(self):
        self.installed("2.2.0")
        with mock.patch.object(updater, "_get", fake_get()), \
                mock.patch.object(updater.sys, "platform", "win32"), \
                mock.patch.object(updater, "download_source", side_effect=updater.UpdateError("kaputt")):
            status = updater.check_and_update(CFG)
        self.assertEqual(status["state"], "error")

    def test_interrupted_install_is_recognised(self):
        self.installed("2.3.0", sha="b" * 40)
        paths.UPDATE_STATUS.write_text(json.dumps({"state": "installing", "latest_sha": "b" * 40,
                                                   "latest_message": "Version 2.3.0"}), encoding="utf-8")
        status = self.check()
        self.assertEqual(status["state"], "current")
        self.assertIsNotNone(status.get("updated_at"))

    def test_parse_version(self):
        self.assertEqual(updater.parse_version("v2.10.1"), (2, 10, 1))
        self.assertIsNone(updater.parse_version("v2.3"))
        self.assertIsNone(updater.parse_version("latest"))


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
            root = updater.download_source("owner/repo", "abc", Path(d))
            self.assertEqual(root.name, "repo-abc")

    def test_rejects_path_traversal(self):
        data = make_zip(["repo-abc/install/windows/install.ps1", "repo-abc/../../evil.txt"])
        with tempfile.TemporaryDirectory() as d, mock.patch.object(updater, "_get", return_value=data):
            with self.assertRaises(updater.UpdateError):
                updater.download_source("owner/repo", "abc", Path(d))

    def test_rejects_archive_without_installer(self):
        data = make_zip(["repo-abc/README.md"])
        with tempfile.TemporaryDirectory() as d, mock.patch.object(updater, "_get", return_value=data):
            with self.assertRaises(updater.UpdateError):
                updater.download_source("owner/repo", "abc", Path(d))


if __name__ == "__main__":
    unittest.main()
