"""Opt-in Linux isolation for approved commands."""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import platform
import re
import stat
import struct
import sys
import tempfile

from .filesystem import Directory


LAUNCHER = Path("/usr/bin/bwrap")
UNSHARE = Path("/usr/bin/unshare")
SYSTEM_ROOTS = tuple(map(Path, ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")))
PRIVATE_ROOTS = tuple(map(Path, ("/etc", "/dev", "/proc", "/sys", "/run", "/var/lib")))
MOUNT_ESCAPE = re.compile(rb"\\(040|011|012|134)")
MAX_ENTRIES = 200_000
MAX_MASKS = 96
MAX_DEPTH = 64


@dataclass(frozen=True)
class Launch:
    argv: list
    cwd: Path
    env: dict
    pass_fds: tuple


def identity(info):
    return info.st_dev, info.st_ino


def _overlap(left, right):
    return left.is_relative_to(right) or right.is_relative_to(left)


def _mount_points():
    with open("/proc/self/mountinfo", "rb") as handle:
        raw = handle.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("sandbox mount inventory exceeds 1 MiB")
    return {
        Path(os.fsdecode(MOUNT_ESCAPE.sub(lambda match: bytes([int(match[1], 8)]), row.split()[4])))
        for row in raw.splitlines()
    }


def _check_mount(path, mounts):
    if any(candidate != path and candidate.is_relative_to(path) for candidate in mounts):
        raise ValueError("sandbox cannot expose a directory containing another mount")
    if path in mounts:
        raise ValueError("sandbox needs a specific directory below the filesystem mount point")


def _trusted_executable(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o6022
            or not info.st_mode & 0o100):
        raise ValueError("sandbox launchers must be executable, root-owned non-setuid system files")
    return identity(info)


def _check_host_runtime(workspace):
    # The PTY bootstrap runs before isolation. A previous sandboxed command
    # must not be able to replace the host interpreter or its import roots.
    for entry in (sys.executable, __file__, *sys.path):
        if isinstance(entry, str):
            path = Path(os.path.abspath(entry))
            if _overlap(path, workspace) or _overlap(path.resolve(), workspace):
                raise ValueError("run Sprig from an installation and Python import path outside the sandbox workspace")


def _filter():
    # Classic BPF over Linux struct seccomp_data. Refuse alternate ABIs, including
    # x32, so compat socketcall and syscall-number aliases cannot skip the rules.
    machine = platform.machine()
    if sys.byteorder != "little" or machine not in ("x86_64", "aarch64") or struct.calcsize("P") != 8:
        raise ValueError("bubblewrap command sandbox requires 64-bit x86 or ARM Linux")
    arch, socket, socketpair = (0xC000003E, 41, 53) if machine == "x86_64" else (0xC00000B7, 198, 199)
    load, equal, at_least, bit_and, ret = 0x20, 0x15, 0x35, 0x54, 0x06
    allow, refuse, kill = 0x7FFF0000, 0x00050001, 0x80000000  # ALLOW, EPERM, KILL_PROCESS
    instructions = [
        (load, 0, 0, 4),                  # seccomp_data.arch
        (equal, 1, 0, arch),
        (ret, 0, 0, kill),
        (load, 0, 0, 0),                  # seccomp_data.nr
        (at_least, 0, 1, 0x40000000),
        (ret, 0, 0, refuse),
        (equal, 0, 1, socket),            # No pathname UNIX, VSOCK, or other host endpoints.
        (ret, 0, 0, refuse),
        (equal, 0, 1, 425),               # io_uring_setup can bypass syscall filtering.
        (ret, 0, 0, refuse),
        (equal, 1, 0, socketpair),        # Permit only private AF_UNIX SOCK_STREAM pairs.
        (ret, 0, 0, allow),
        (load, 0, 0, 16),                 # args[0], low word (domain)
        (equal, 0, 3, 1),                 # AF_UNIX
        (load, 0, 0, 24),                 # args[1], low word (type and flags)
        (bit_and, 0, 0, 0xF),            # Ignore SOCK_CLOEXEC and SOCK_NONBLOCK.
        (equal, 0, 1, 1),                 # SOCK_STREAM
        (ret, 0, 0, allow),
        (ret, 0, 0, refuse),
    ]
    return b"".join(struct.pack("=HBBI", *entry) for entry in instructions)


class Sandbox:
    def __init__(self, config, root, cwd, policy, check):
        if sys.platform != "linux":
            raise ValueError("bubblewrap command sandbox is available only on Linux; command refused")
        self.filter = _filter()
        self.config, self.root, self.cwd, self.policy, self.check = config, root, cwd.path, policy, check
        self.cwd_identity = identity(os.fstat(cwd.fd))
        self.snapshot = self._inspect()

    def _inspect(self):
        self.check()
        try:
            launchers = tuple(_trusted_executable(path) for path in (LAUNCHER, UNSHARE))
        except FileNotFoundError:
            raise ValueError("command sandbox needs system bubblewrap and util-linux unshare") from None
        if not Path("/proc/sys/fs/binfmt_misc").is_dir():
            raise ValueError("command sandbox needs a kernel with user-namespace binfmt_misc support")
        if identity(self.root.path.stat()) != identity(os.fstat(self.root.fd)):
            raise ValueError("sandbox workspace was replaced; reopen this workspace")
        mounts = _mount_points()
        home = Path.home().resolve()
        workspace = self.root.path
        _check_host_runtime(workspace)
        if home.is_relative_to(workspace) or workspace == Path("/tmp") or any(
                _overlap(workspace, root) for root in (*SYSTEM_ROOTS, *PRIVATE_ROOTS)):
            raise ValueError("sandbox workspace must be a dedicated directory outside system roots")
        _check_mount(workspace, mounts)

        private = {Path(path).resolve() for path in (self.config.config_path, *self.config.config_paths,
                   self.config.session_dir, self.config.change_history_dir, self.config.command_log_dir)
                   if path is not None}
        private.update(path.resolve() for path in (home / ".sprig", home / ".local/state/sprig-agent"))
        private_ids = {identity(path.stat()) for path in private if path.exists()}
        if any(workspace.is_relative_to(path) for path in private):
            raise ValueError("sandbox workspace overlaps private Sprig storage")
        system, aliases, masks = [], [], []
        for path in SYSTEM_ROOTS:
            if path.is_symlink():
                target = path.resolve(strict=True)
                if target.is_relative_to(Path("/usr")):
                    aliases.append((str(target).lstrip("/"), path))
                    continue
            if path.exists():
                system.append((path, identity(path.stat())))
                # WSL supplies kernel and host integration mounts below /usr.
                # Cover system submounts before the approved program can start.
                nested = {mount for mount in mounts if mount != path and mount.is_relative_to(path)}
                interop = Path("/usr/lib/wsl")
                if interop.is_relative_to(path) and interop.is_dir():
                    nested.add(interop)
                for mount in sorted(nested):
                    if any(parent in nested for parent in mount.parents):
                        continue
                    info = mount.lstat()
                    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                        raise ValueError("sandbox cannot mask an unexpected system submount")
                    masks.append((mount, stat.S_ISDIR(info.st_mode), identity(info)))
        extra = []
        claimed = [workspace, *SYSTEM_ROOTS, *PRIVATE_ROOTS, *private]
        for entry in self.config.sandbox_read_dirs:
            destination = Path(os.path.abspath(entry))
            path = entry.resolve(strict=True)
            if not path.is_dir():
                raise ValueError("sandbox read grant must name a directory")
            names = tuple(dict.fromkeys((path, destination)))
            if any(home.is_relative_to(name) or name == Path("/tmp")
                   or any(_overlap(name, other) for other in claimed) for name in names):
                raise ValueError("sandbox read directory overlaps the workspace, a system root or private Sprig storage")
            if destination != path:
                if _overlap(destination, path):
                    raise ValueError("sandbox runtime alias overlaps its resolved directory")
                aliases.append((str(path), destination))
            claimed.extend(names)
            _check_mount(path, mounts)
            extra.append((path, identity(path.stat())))

        entries = 0
        self.policy.reload()

        def visit(directory, prefix):
            nonlocal entries
            if len(prefix) >= MAX_DEPTH:
                raise ValueError("sandbox workspace exceeds 64 directory levels")
            with directory.scandir() as items:
                for entry in items:
                    self.check()
                    entries += 1
                    if entries > MAX_ENTRIES:
                        raise ValueError("sandbox workspace exceeds 200,000 visible entries")
                    parts = (*prefix, entry.name)
                    path = directory.path / entry.name
                    info = entry.stat(follow_symlinks=False)
                    is_dir = stat.S_ISDIR(info.st_mode)
                    regular = stat.S_ISREG(info.st_mode)
                    excluded = (self.policy.excluded(parts, ignore_case=True) or identity(info) in private_ids
                                or path in private or len(parts) == 1 and entry.name.lower() == ".sprigignore")
                    if excluded:
                        if not is_dir and not regular:
                            raise ValueError("sandbox cannot mask a protected link or special file")
                        masks.append((path, is_dir, identity(info)))
                        if len(masks) > MAX_MASKS:
                            raise ValueError("sandbox workspace exceeds 96 protected paths")
                    elif is_dir:
                        with directory.child(entry.name) as child:
                            visit(child, parts)
                    elif regular and info.st_nlink != 1:
                        raise ValueError("sandbox workspace contains a multiply linked file; use copied dependencies")
                    elif not regular and not stat.S_ISLNK(info.st_mode):
                        raise ValueError("sandbox workspace contains a special file")

        visit(self.root, ())
        if len(masks) > MAX_MASKS:
            raise ValueError("sandbox exceeds 96 protected paths")
        return (launchers, tuple(system), tuple(aliases), tuple(extra), tuple(sorted(masks)))

    def description(self):
        read = [str(path) for path, _ in (*self.snapshot[1], *self.snapshot[3])]
        read.extend(str(path) for target, path in self.snapshot[2] if target.startswith("/"))
        return ("RUN (BUBBLEWRAP; network blocked; host environment excluded)\n"
                + "workspace: " + json.dumps(str(self.root.path)) + " (read/write, protected paths masked)\n"
                + "read-only runtime directories: " + json.dumps(read) + "\n"
                + "private temporary files are discarded; setup failure stops execution")

    @contextmanager
    def launch(self, argv):
        if self._inspect() != self.snapshot or identity(self.cwd.lstat()) != self.cwd_identity:
            raise ValueError("sandbox inputs changed during approval; request fresh approval")
        _, system, aliases, extra, masks = self.snapshot
        # A user namespace without its own binfmt_misc table inherits handlers
        # from its parent. WSL's handler pins the host /init interpreter.
        options = [str(UNSHARE), "--map-current-user", "--pid", "--fork", "--kill-child", "--mount-binfmt",
                   "--propagation", "private", "--", str(LAUNCHER),
                   "--unshare-all", "--unshare-user", "--disable-userns",
                   "--die-with-parent", "--cap-drop", "ALL", "--clearenv",
                   "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--dir", "/run/sprig-home"]
        fds = []
        with ExitStack() as stack:
            def mount(fd, path, option):
                fds.append(fd)
                options.extend((option, str(fd), str(path)))

            for path, expected in (*system, *extra):
                directory = stack.enter_context(Directory(path))
                if identity(os.fstat(directory.fd)) != expected:
                    raise ValueError("sandbox runtime directory changed during approval")
                mount(directory.fd, path, "--ro-bind-fd")
            for target, path in aliases:
                options.extend(("--symlink", target, str(path)))
            mount(self.root.fd, self.root.path, "--bind-fd")
            for path, is_dir, _ in masks:
                if is_dir:
                    options.extend(("--size", "4096", "--tmpfs", str(path), "--remount-ro", str(path)))
                else:
                    fd = os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC)
                    stack.callback(os.close, fd)
                    mount(fd, path, "--ro-bind-data")
            policy = stack.enter_context(tempfile.TemporaryFile(dir="/tmp"))
            policy.write(self.filter)
            policy.seek(0)
            fds.append(policy.fileno())
            options.extend(("--seccomp", str(policy.fileno())))
            for key, value in (("PATH", "/usr/local/bin:/usr/bin:/bin"), ("HOME", "/run/sprig-home"),
                               ("TMPDIR", "/tmp"), ("LANG", "C.UTF-8"), ("TERM", "xterm-256color")):
                options.extend(("--setenv", key, value))
            options.extend(("--chdir", str(self.cwd), "--", *argv))
            self.check()
            yield Launch(options, Path("/"), {}, tuple(fds))
