"""Git reference and abbreviation behavior, using only local fixture repositories."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

if sys.platform == "win32":
    from gitrepo import CommitError, Repository
    from winprocess import Job

    class LocalRepository(Repository):
        def git(self, *args, **kwargs):
            # Only tests use a named local Git remote; production transport is HTTPS.
            if args[0] in ("clone", "fetch"):
                kwargs["local"] = True
            return super().git(*args, **kwargs)


@unittest.skipUnless(
    sys.platform == "win32" and shutil.which("git.exe"), "requires Windows and Git"
)
class GitReferences(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.remote = self.root / "remote.git"
        self.cache = self.root / "cache.git"
        self.checkout = self.root / "checkout"
        self.source = self.root / "source"
        self.job = Job()
        self.addCleanup(self.job.close)
        (self.root / "empty-git-config").touch()
        self.env = {
            key: val
            for key, val in os.environ.items()
            if not key.upper().startswith("GIT_")
        }
        self.env.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=str(self.root / "empty-git-config"),
            GIT_AUTHOR_NAME="Fixture",
            GIT_AUTHOR_EMAIL="fixture@example.invalid",
            GIT_COMMITTER_NAME="Fixture",
            GIT_COMMITTER_EMAIL="fixture@example.invalid",
        )
        self.git("init", "--initial-branch=main", self.source)
        self.git("init", "--bare", "--initial-branch=main", self.remote)
        self.repo = LocalRepository(
            self.cache, self.checkout, self.remote.as_uri(), self.job, self.root
        )
        self.tree = self.git("-C", self.source, "mktree", input=b"")

    def git(self, *args, input=None):
        result = subprocess.run(
            ["git.exe", *map(str, args)],
            input=input,
            capture_output=True,
            env=self.env,
            cwd=self.root,
            timeout=20,
        )
        self.assertEqual(
            result.returncode, 0, result.stderr.decode("utf-8", errors="replace")
        )
        return result.stdout.strip().decode("ascii")

    def make_commit(self, message, parent=None):
        argv = ("-p", parent) if parent else ()
        return self.git(
            "-C", self.source, "commit-tree", self.tree, *argv, "-m", message
        )

    def push(self, *refspecs):
        self.git("-C", self.source, "push", "--force", self.remote, *refspecs)

    def test_create_uses_single_clone_snapshot_when_remote_renames_default(self):
        old = self.make_commit("original default branch")
        new = self.make_commit("advanced default branch", parent=old)
        self.push(f"{old}:refs/heads/main")
        git = self.repo.git

        def advance_after_clone(*args, **kwargs):
            result = git(*args, **kwargs)
            if args[:2] == ("clone", "--bare"):
                self.push(f"{new}:refs/heads/trunk")
                self.git("-C", self.remote, "symbolic-ref", "HEAD", "refs/heads/trunk")
                self.push(":refs/heads/main")
            return result

        with patch.object(self.repo, "git", side_effect=advance_after_clone) as call:
            self.repo.create()
            remote_commands = [
                args[0]
                for args, _ in call.call_args_list
                if self.repo.url in args and args[0] in ("clone", "fetch", "ls-remote")
            ]
        self.assertEqual(remote_commands, ["clone"])
        self.assertEqual(self.git("-C", self.remote, "rev-parse", "HEAD"), new)
        self.assertEqual(self.git("-C", self.cache, "rev-parse", "HEAD"), old)
        self.assertEqual(self.git("-C", self.checkout, "rev-parse", "HEAD"), old)
        self.assertEqual(
            self.git("-C", self.checkout, "symbolic-ref", "HEAD"), "refs/heads/main"
        )
        self.assertEqual(
            self.git("-C", self.checkout, "remote", "get-url", "origin"), self.repo.url
        )
        self.repo.fetch()
        self.assertEqual(self.repo.resolve(new), new)

    def test_create_keeps_tags_not_reachable_from_a_branch(self):
        main = self.make_commit("default branch")
        tagged = self.make_commit("reachable only from a tag")
        self.git("-C", self.source, "tag", "-a", "release", tagged, "-m", "release")
        self.push(f"{main}:refs/heads/main", "refs/tags/release")

        self.repo.create()

        self.assertEqual(self.repo.resolve(tagged), tagged)
        self.assertEqual(
            self.git("-C", self.checkout, "rev-parse", "refs/tags/release^{commit}"),
            tagged,
        )

    def test_create_accepts_empty_remote(self):
        self.repo.create()

        self.assertEqual(
            self.git("-C", self.checkout, "symbolic-ref", "HEAD"), "refs/heads/main"
        )
        self.assertEqual(self.git("-C", self.checkout, "for-each-ref"), "")
        self.assertEqual(
            self.git("-C", self.checkout, "remote", "get-url", "origin"), self.repo.url
        )

    def test_force_push_and_deleted_refs_leave_unusable_cached_objects(self):
        old = self.make_commit("old")
        new = self.make_commit("unrelated")
        self.git("-C", self.source, "tag", "-a", "kept", old, "-m", "annotated tag")
        self.push(f"{old}:refs/heads/main", f"{old}:refs/heads/extra", "refs/tags/kept")
        self.repo.create()
        self.assertEqual(self.repo.resolve(old.upper()), old)
        self.push(f"{new}:refs/heads/main", ":refs/heads/extra")
        self.repo.fetch()
        self.assertEqual(self.repo.resolve(new[:7]), new)
        self.assertEqual(
            self.repo.resolve(old), old, "the annotated tag still makes it reachable"
        )
        self.push(":refs/tags/kept")
        self.repo.fetch()
        self.assertEqual(self.git("-C", self.cache, "cat-file", "-t", old), "commit")
        with self.assertRaises(CommitError) as exc:
            self.repo.resolve(old)
        self.assertEqual(exc.exception.code, "commit_not_found")

    def test_prefix_below_git_minimum_can_be_unique_or_ambiguous(self):
        seen: dict[str, str] = {}
        for index in range(17):  # pigeonhole principle: at least two share a hex digit
            commit = self.make_commit(f"candidate {index}")
            prefix = commit[0]
            if prefix in seen:
                first = seen[prefix]
                break
            seen[prefix] = commit
        self.push(f"{first}:refs/heads/main")
        self.repo.create()
        self.assertEqual(self.repo.resolve(prefix), first)
        self.push(f"{commit}:refs/heads/extra")
        self.repo.fetch()
        with self.assertRaises(CommitError) as exc:
            self.repo.resolve(prefix)
        self.assertEqual(exc.exception.code, "commit_ambiguous")
        self.assertEqual(self.repo.resolve(first), first)
        self.push(":refs/heads/extra")
        self.repo.fetch()
        self.assertEqual(self.repo.resolve(prefix), first)
