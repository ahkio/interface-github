# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
import base64
import os
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from odoo.exceptions import UserError

from .common import TestGithubConnectorCommon

BRANCH_MODULE = "odoo.addons.github_connector.models.github_repository_branch"
TOKEN = "secret-token"
CREDENTIALS = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
CLONE_URL = "https://github.com/OCA/OCB.git"
TOKEN_URL = f"https://{TOKEN}@github.com/OCA/OCB.git"


class TestGithubRepositoryBranchDownload(TestGithubConnectorCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_str("github.access_token", TOKEN)
        cls.branch = cls.repository_ocb_13

    def setUp(self):
        super().setUp()
        source_path = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, source_path, True)
        branch_class = type(self.branch)
        for patcher in (
            patch.object(branch_class, "_get_source_path", return_value=source_path),
            patch.object(
                type(self.env["github.repository"]),
                "find_related_github_object",
                return_value=SimpleNamespace(clone_url=CLONE_URL),
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.branch.invalidate_recordset(["local_path"])
        self.local_path = self.branch.local_path
        self.git_calls = []

    def _fake_git(self, handlers):
        """Patch subprocess.run for git: handlers maps a git subcommand to a
        function returning (returncode, stdout, stderr); subcommands without
        a handler run the real git."""
        real_run = subprocess.run

        def fake_run(command, **kwargs):
            self.git_calls.append((command, kwargs))
            handler = handlers.get(command[1])
            if handler is None:
                return real_run(command, **kwargs)
            returncode, stdout, stderr = handler(command)
            return subprocess.CompletedProcess(command, returncode, stdout, stderr)

        return patch(f"{BRANCH_MODULE}.subprocess.run", side_effect=fake_run)

    def _make_repository(self, remote_url):
        os.makedirs(self.local_path)
        subprocess.run(["git", "init", "-q", self.local_path], check=True)
        subprocess.run(
            ["git", "-C", self.local_path, "remote", "add", "origin", remote_url],
            check=True,
        )

    def _remote_url(self):
        return subprocess.run(
            ["git", "-C", self.local_path, "remote", "get-url", "origin"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _calls_of(self, subcommand):
        return [call for call in self.git_calls if call[0][1] == subcommand]

    def _assert_auth_header(self, kwargs):
        env = kwargs["env"]
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(
            env["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraHeader"
        )
        self.assertEqual(
            env["GIT_CONFIG_VALUE_0"], f"Authorization: Basic {CREDENTIALS}"
        )

    def test_clone_sends_token_as_header(self):
        with self._fake_git({"clone": lambda command: (0, "", "")}):
            self.branch._download_code()
        ((command, kwargs),) = self._calls_of("clone")
        self.assertEqual(
            command,
            [
                "git",
                "clone",
                "-b",
                self.branch.name,
                "--single-branch",
                CLONE_URL,
                self.local_path,
            ],
        )
        self._assert_auth_header(kwargs)
        self.assertEqual(self.branch.state, "to_analyze")

    def test_clone_without_token_sends_no_header(self):
        with (
            patch.object(type(self.branch), "_get_github_token", return_value=""),
            self._fake_git({"clone": lambda command: (0, "", "")}),
        ):
            self.branch._download_code()
        ((_command, kwargs),) = self._calls_of("clone")
        self.assertNotIn("GIT_CONFIG_VALUE_0", kwargs["env"])

    def test_clone_failure_removes_folder_and_masks_token(self):
        def failing_clone(command):
            # git leaves the target folder behind when it existed beforehand
            os.makedirs(command[-1])
            return (
                128,
                "",
                f"fatal: unable to access '{TOKEN_URL}': The requested URL "
                f"returned error: 401 (Authorization: Basic {CREDENTIALS})",
            )

        with (
            self._fake_git({"clone": failing_clone}),
            self.assertLogs(BRANCH_MODULE, "ERROR") as logs,
            self.assertRaises(UserError) as error,
        ):
            self.branch._download_code()
        message = str(error.exception)
        self.assertIn("returned error: 401", message)
        self.assertIn("https://***@github.com/OCA/OCB.git", message)
        log_output = "\n".join(logs.output)
        self.assertIn("returned error: 401", log_output)
        for text in (message, log_output):
            self.assertNotIn(TOKEN, text)
            self.assertNotIn(CREDENTIALS, text)
        self.assertFalse(os.path.exists(self.local_path))
        self.assertEqual(self.branch.state, "to_download")

    def test_clone_failure_raises_when_folder_cannot_be_removed(self):
        def failing_clone(command):
            os.makedirs(command[-1])
            return 128, "", "fatal: The requested URL returned error: 401"

        with (
            self._fake_git({"clone": failing_clone}),
            patch(
                f"{BRANCH_MODULE}.shutil.rmtree",
                side_effect=PermissionError("Permission denied"),
            ),
            self.assertLogs(BRANCH_MODULE, "ERROR"),
            self.assertRaises(PermissionError),
        ):
            self.branch._download_code()

    def test_pull_removes_token_from_remote_url(self):
        self._make_repository(TOKEN_URL)
        self.branch.state = "analyzed"
        with self._fake_git({"pull": lambda command: (0, "Already up to date.\n", "")}):
            self.branch._download_code()
        self.assertEqual(self._remote_url(), CLONE_URL)
        ((command, kwargs),) = self._calls_of("pull")
        self.assertEqual(command, ["git", "pull", "origin", self.branch.name])
        self.assertEqual(kwargs["cwd"], self.local_path)
        self._assert_auth_header(kwargs)
        self.assertEqual(self.branch.state, "analyzed")

    def test_pull_with_changes_marks_branch_to_analyze(self):
        self._make_repository(CLONE_URL)
        self.branch.state = "analyzed"
        with self._fake_git({"pull": lambda command: (0, "Fast-forward\n", "")}):
            self.branch._download_code()
        self.assertEqual(self.branch.state, "to_analyze")

    def test_pull_failure_reclones_and_masks_token(self):
        self._make_repository(CLONE_URL)
        with (
            self._fake_git(
                {
                    "pull": lambda command: (
                        1,
                        "",
                        f"fatal: Authentication failed for '{TOKEN_URL}'",
                    ),
                    "clone": lambda command: (0, "", ""),
                }
            ),
            self.assertLogs(BRANCH_MODULE, "WARNING") as logs,
        ):
            self.branch._download_code()
        log_output = "\n".join(logs.output)
        self.assertIn("Authentication failed for 'https://***@github.com", log_output)
        self.assertNotIn(TOKEN, log_output)
        self.assertEqual(len(self._calls_of("clone")), 1)
        # The fake clone creates nothing: the old checkout has been removed
        self.assertFalse(os.path.exists(self.local_path))
        self.assertEqual(self.branch.state, "to_analyze")

    def test_folder_without_repository_is_recloned(self):
        os.makedirs(self.local_path)
        with (
            self._fake_git({"clone": lambda command: (0, "", "")}),
            self.assertLogs(BRANCH_MODULE, "WARNING"),
        ):
            self.branch._download_code()
        self.assertFalse(self._calls_of("remote"))
        self.assertFalse(self._calls_of("pull"))
        self.assertEqual(len(self._calls_of("clone")), 1)
