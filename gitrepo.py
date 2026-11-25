"""Git transport, reachability checks, and clean worktree materialization."""

import os
from pathlib import Path
import re
import shutil
import tempfile

import config

_REF_PATH_CONFLICT = re.compile(
    rb"cannot lock ref 'refs/(?:heads|tags)/[^\r\n]+': "
    rb"Unable to create '[^\r\n]+\.lock': File exists"
    rb"|cannot process '(refs/(?:heads|tags)/[^\r\n]+)' and '\1/[^\r\n]+' "
    rb"at the same time"
)


class GitFailure(Exception):
    def __init__(self, message, stdout=b"", stderr=b""):
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr or message.encode("utf-8", errors="replace")


class CommitError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Repository:
    def __init__(self, cache, worktree, url, job, base):
        self.cache = cache
        self.worktree = worktree
        self.url = url
        self.job = job
        self.base = base

    def git(self, *args, path=None, local=False, check=True, consumer=None):
        if not config.GIT:
            raise GitFailure("Git for Windows was not found on PATH")
        tls_settings = {"GIT_SSL_CAINFO", "GIT_SSL_CAPATH", "GIT_PROXY_SSL_CAINFO"}
        env = {
            k: v
            for k, v in os.environ.items()
            if k.upper() != "RUNNER_TOKEN"
            and (not k.upper().startswith("GIT_") or k.upper() in tls_settings)
        }
        env.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=str(self.base / "empty-git-config"),
            GIT_TERMINAL_PROMPT="0",
            GIT_ALLOW_PROTOCOL="file" if local else "https",
            GIT_LFS_SKIP_SMUDGE="1",
            GIT_NO_REPLACE_OBJECTS="1",
            GIT_OPTIONAL_LOCKS="0",
            LC_ALL="C",
        )
        argv = [
            config.GIT,
            "-c",
            "credential.helper=",
            "-c",
            "core.protectNTFS=true",
            "-c",
            "core.symlinks=false",
            "-c",
            "http.sslVerify=true",
            "-c",
            "http.followRedirects=false",
            "-c",
            "gc.auto=0",
        ]
        if path is not None:
            argv.extend(("-C", str(path)))
        argv.extend(str(arg) for arg in args)
        stdout, stderr = bytearray(), bytearray()
        try:
            code = self.job.run(
                argv,
                cwd=self.base,
                env=env,
                executable=config.GIT,
                stdout=consumer if consumer is not None else stdout.extend,
                stderr=stderr.extend,
            )
        except OSError as exc:
            raise GitFailure(
                f"Could not start Git: {exc}", bytes(stdout), bytes(stderr)
            ) from exc
        if code and check:
            raise GitFailure(
                f"git {args[0]} exited with code {code}", bytes(stdout), bytes(stderr)
            )
        return code, bytes(stdout)

    def create(self):
        # Keep HEAD, branches, and tags from the same remote snapshot.
        self.git(
            "clone",
            "--bare",
            "--no-recurse-submodules",
            "--template=",
            "--",
            self.url,
            self.cache,
        )
        code, head = self.git(
            "rev-parse", "--verify", "HEAD^{commit}", path=self.cache, check=False
        )
        if code == 0:
            _, ref = self.git(
                "symbolic-ref", "-q", "HEAD", path=self.cache, check=False
            )
            branch = ref.strip().decode("utf-8", errors="surrogateescape")
            self.materialize(
                head.strip().decode("ascii"), attached=branch.startswith("refs/heads/")
            )
        else:
            _, heads = self.git(
                "for-each-ref",
                "--count=1",
                "--format=%(objectname)",
                "refs/heads",
                path=self.cache,
            )
            if heads.strip():
                raise GitFailure("The remote default branch does not identify a commit")
            self.git(
                "clone",
                "--shared",
                "--no-checkout",
                "--template=",
                "--",
                self.cache,
                self.worktree,
                local=True,
            )
        # Keep the public origin in the exposed .git/config, not the cache path.
        self.git("remote", "set-url", "origin", self.url, path=self.worktree)

    def fetch(self):
        try:
            self.git(
                "fetch",
                "--atomic",
                "--prune",
                "--no-tags",
                "--no-recurse-submodules",
                "--",
                self.url,
                "+refs/heads/*:refs/heads/*",
                "+refs/tags/*:refs/tags/*",
                path=self.cache,
            )
        except GitFailure as exc:
            # Older Git only reports the conflicting .lock file. An orphaned
            # lock can cause this too; retry at most once, in a fresh cache.
            if not _REF_PATH_CONFLICT.search(exc.stderr):
                raise
            self._refresh_packed_cache()
            return

        # A successful fetch can put one spelling in a loose ref, shadowing a
        # packed ref with a different case. Use FETCH_HEAD's names from this
        # snapshot, since the filesystem may already have aliased the refs.
        seen: set[tuple[bytes, str]] = set()
        directories: set[tuple[bytes, str]] = set()
        for line in (self.cache / "FETCH_HEAD").read_bytes().split(b"\n"):
            fields = line.split(b"\t", 2)
            if len(fields) != 3:
                continue
            kind, _, description = fields[2].partition(b" '")
            if kind not in (b"branch", b"tag"):
                continue
            # Git ref names cannot contain spaces, but can contain quotes.
            name = description.split(b"' of ", 1)[0]
            key = kind, name.decode("utf-8", errors="surrogateescape").upper()
            parents = set()
            parent = key[1]
            while "/" in parent:
                parent = parent.rsplit("/", 1)[0]
                parents.add((kind, parent))
            if key in seen or key in directories or parents & seen:
                self._refresh_packed_cache()
                return
            seen.add(key)
            directories.update(parents)

    def _refresh_packed_cache(self):
        # Bare clone writes packed refs without taking a lock at each ref path.
        # Keep its normal heads/tags mapping: a mirror could import refs/replace.
        # The previous worktree was reset before fetch. Reference the old object
        # store for negotiation, then dissociate before swapping caches.
        scratch = Path(tempfile.mkdtemp(prefix=".fetch-", dir=self.cache.parent))
        fresh, old = scratch / "fresh", scratch / "old"
        preserve_backup = False
        try:
            self.git(
                "clone",
                "--bare",
                "--reference",
                self.cache,
                "--dissociate",
                "--no-recurse-submodules",
                "--template=",
                "--",
                self.url,
                fresh,
            )
            self.cache.rename(old)
            try:
                fresh.rename(self.cache)
            except OSError as swap_error:
                try:
                    old.rename(self.cache)
                except OSError as restore_error:
                    preserve_backup = True
                    raise GitFailure(
                        f"Could not replace cache ({swap_error}) or restore it "
                        f"({restore_error}); previous cache is at {old}"
                    ) from restore_error
                raise
        except OSError as exc:
            raise GitFailure(f"Could not replace cache: {exc}") from exc
        finally:
            if not preserve_backup:
                shutil.rmtree(scratch, ignore_errors=True)

    def resolve(self, prefix):
        prefix = prefix.lower()
        if len(prefix) >= 4:
            _, raw = self.git(f"rev-parse", f"--disambiguate={prefix}", path=self.cache)
            candidates = raw.decode("ascii").splitlines()
            matches = []
            for sha in candidates:
                _, kind = self.git("cat-file", "-t", sha, path=self.cache)
                if kind.strip() != b"commit":
                    continue
                _, refs = self.git(
                    "for-each-ref",
                    f"--contains={sha}",
                    "--count=1",
                    "--format=%(refname)",
                    "refs/heads",
                    "refs/tags",
                    path=self.cache,
                )
                if refs.strip():
                    matches.append(sha)
                    if len(matches) == 2:
                        break
        else:
            # Git's --disambiguate requires at least four hex digits. Scan only
            # reachable commits for the shorter prefixes, with constant memory.
            wanted = prefix.encode("ascii")
            tail = bytearray()
            matches = []

            def scan(chunk):
                if len(matches) >= 2:
                    return
                tail.extend(chunk)
                lines = tail.split(b"\n")
                tail[:] = lines.pop()
                for line in lines:
                    if line.startswith(wanted):
                        matches.append(line.decode("ascii"))
                        if len(matches) >= 2:
                            break

            self.git("rev-list", "--branches", "--tags", path=self.cache, consumer=scan)
        if not matches:
            raise CommitError(
                "commit_not_found",
                "No commit reachable from a current remote branch or tag matches the hash",
            )
        if len(matches) != 1:
            raise CommitError(
                "commit_ambiguous",
                "More than one reachable commit matches the hash prefix",
            )
        return matches[0]

    def materialize(self, commit, *, attached=False):
        self.git(
            "clone",
            "--shared",
            "--no-checkout",
            "--template=",
            "--",
            self.cache,
            self.worktree,
            local=True,
        )
        if attached:
            self.git("reset", "--hard", commit, path=self.worktree)
        else:
            self.git("checkout", "--detach", "--force", commit, path=self.worktree)
            self.git("remote", "set-url", "origin", self.url, path=self.worktree)
