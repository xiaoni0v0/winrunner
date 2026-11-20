"""Git transport, reachability checks, and clean worktree materialization."""

import os
from pathlib import Path

import config


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
        env = {k: v for k, v in os.environ.items()
               if k.upper() != "RUNNER_TOKEN" and
               (not k.upper().startswith("GIT_") or k.upper() in tls_settings)}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=str(self.base / "empty-git-config"),
                   GIT_TERMINAL_PROMPT="0", GIT_ALLOW_PROTOCOL="file" if local else "https",
                   GIT_LFS_SKIP_SMUDGE="1", GIT_OPTIONAL_LOCKS="0", LC_ALL="C")
        argv = [config.GIT, "-c", "credential.helper=", "-c", "core.protectNTFS=true",
                "-c", "core.symlinks=false", "-c", "http.sslVerify=true",
                "-c", "http.followRedirects=false", "-c", "gc.auto=0"]
        if path is not None:
            argv.extend(("-C", str(path)))
        argv.extend(str(arg) for arg in args)
        stdout, stderr = bytearray(), bytearray()
        try:
            code = self.job.run(argv, cwd=self.base, env=env, executable=config.GIT,
                                stdout=consumer if consumer is not None else stdout.extend,
                                stderr=stderr.extend)
        except OSError as exc:
            raise GitFailure(f"Could not start Git: {exc}", bytes(stdout), bytes(stderr)) from exc
        if code and check:
            raise GitFailure(f"git {args[0]} exited with code {code}", bytes(stdout), bytes(stderr))
        return code, bytes(stdout)

    def create(self):
        self.git("clone", "--bare", "--no-tags", "--no-recurse-submodules", "--template=",
                 "--", self.url, self.cache)
        self.fetch()
        code, head = self.git("rev-parse", "--verify", "HEAD^{commit}", path=self.cache, check=False)
        if code == 0:
            _, ref = self.git("symbolic-ref", "-q", "HEAD", path=self.cache, check=False)
            branch = ref.strip().decode("utf-8", errors="surrogateescape")
            self.materialize(head.strip().decode("ascii"), attached=branch.startswith("refs/heads/"))
        else:
            _, heads = self.git("for-each-ref", "--count=1", "--format=%(objectname)",
                                "refs/heads", path=self.cache)
            if heads.strip():
                raise GitFailure("The remote default branch does not identify a commit")
            self.git("clone", "--shared", "--no-checkout", "--template=", "--",
                     self.cache, self.worktree, local=True)
        # Keep the public origin in the exposed .git/config, not the cache path.
        self.git("remote", "set-url", "origin", self.url, path=self.worktree)

    def fetch(self):
        self.git("fetch", "--atomic", "--prune", "--no-tags", "--no-recurse-submodules",
                 "--", self.url, "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*",
                 path=self.cache)

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
                _, refs = self.git("for-each-ref", f"--contains={sha}", "--count=1",
                                   "--format=%(refname)", "refs/heads", "refs/tags", path=self.cache)
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
            raise CommitError("commit_not_found", "No commit reachable from a current remote branch or tag matches the hash")
        if len(matches) != 1:
            raise CommitError("commit_ambiguous", "More than one reachable commit matches the hash prefix")
        return matches[0]

    def materialize(self, commit, *, attached=False):
        self.git("clone", "--shared", "--no-checkout", "--template=", "--",
                 self.cache, self.worktree, local=True)
        if attached:
            self.git("reset", "--hard", commit, path=self.worktree)
        else:
            self.git("checkout", "--detach", "--force", commit, path=self.worktree)
            self.git("remote", "set-url", "origin", self.url, path=self.worktree)
