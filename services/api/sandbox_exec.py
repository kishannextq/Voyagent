"""Start a tool server process under OS resource limits, then exec it.

    python sandbox_exec.py <command> [args...]

Applied on Linux/macOS (the containers); on Windows the command runs unchanged (dev only).
Limits (env-overridable): address space, open files, max file size written, process count,
no core dumps. Together with the minimal environment from mcp_client._server_env this bounds
what a misbehaving or compromised tool server can do to the host. It is not a full sandbox:
network and filesystem reads are not restricted here (see docs/SECURITY.md, tool isolation).
"""

import os
import sys

LIMITS = {
    # name: (env var, default)
    "RLIMIT_AS": ("SANDBOX_MAX_MEMORY_BYTES", 1024 * 1024 * 1024),  # 1 GiB virtual memory
    "RLIMIT_NOFILE": ("SANDBOX_MAX_OPEN_FILES", 256),
    "RLIMIT_FSIZE": ("SANDBOX_MAX_FILE_BYTES", 16 * 1024 * 1024),  # 16 MiB per written file
    "RLIMIT_NPROC": ("SANDBOX_MAX_PROCESSES", 512),
    "RLIMIT_CORE": ("SANDBOX_CORE_BYTES", 0),
}


def apply_limits() -> dict[str, int]:
    import resource

    applied = {}
    for name, (env, default) in LIMITS.items():
        res = getattr(resource, name, None)
        if res is None:
            continue
        want = int(os.environ.get(env, default))
        _soft, hard = resource.getrlimit(res)
        if hard != resource.RLIM_INFINITY:
            want = min(want, hard)  # can only lower limits
        resource.setrlimit(res, (want, want))
        applied[name] = want
    return applied


def main(argv: list[str]) -> None:
    if not argv:
        print("usage: sandbox_exec.py <command> [args...]", file=sys.stderr)
        sys.exit(2)
    if os.name == "posix":
        apply_limits()
        os.umask(0o077)
        os.execvp(argv[0], argv)  # noqa: S606 - argv is built by mcp_client, not user input
    else:  # Windows dev machines: no rlimits; run the command as a child and pass its exit code
        import subprocess

        sys.exit(subprocess.call(argv))  # noqa: S603 - argv is built by mcp_client, not user input


if __name__ == "__main__":
    main(sys.argv[1:])
