"""Pinned directories and no-follow files for POSIX and native Windows."""

import os
from contextlib import contextmanager
from pathlib import Path
import stat


class Directory:
    def __init__(self, path, *, parent=None, name=None):
        if parent is not None:
            parent.ensure_open()
        self.path = Path(path)
        self.fd = None
        if os.name == "nt":
            from . import windows
            self.fd = windows.open_file(self.path, directory=True)
        else:
            self.fd = os.open(name if parent else self.path,
                              os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=parent.fd if parent else None)

    def __enter__(self):
        self.ensure_open()
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.fd is not None:
            if os.name == "nt":
                from .windows import close_handle
                close_handle(self.fd)
            else:
                os.close(self.fd)
            self.fd = None

    def child(self, name):
        self.ensure_open()
        return Directory(self.path / name, parent=self, name=name)

    def ensure_open(self):
        if self.fd is None:
            raise OSError("directory handle is closed")

    def identity(self):
        self.ensure_open()
        if os.name == "nt":
            from .windows import file_identity
            return file_identity(self.fd)
        info = os.fstat(self.fd)
        return info.st_dev, info.st_ino

    def scandir(self):
        self.ensure_open()
        return os.scandir(self.path if os.name == "nt" else self.fd)

    def stat(self, name):
        self.ensure_open()
        if os.name == "nt":
            return os.stat(self.path / name, follow_symlinks=False)
        return os.stat(name, dir_fd=self.fd, follow_symlinks=False)

    @contextmanager
    def open_read(self, name, *, private=False):
        self.ensure_open()
        if os.name == "nt":
            from .windows import file_descriptor
            fd = file_descriptor(self.path / name, private=private)
        else:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("only regular files are supported")
            if private and os.name != "nt" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
                raise ValueError("unsafe session file")
            yield handle

    def read(self, name, limit, *, private=False):
        with self.open_read(name, private=private) as handle:
            return handle.read(limit + 1)

    def write_new(self, name, data, mode=0o600, *, private=False, permissions_from=None):
        self.ensure_open()
        if os.name == "nt":
            from .windows import file_descriptor
            fd = file_descriptor(self.path / name, create=True, private=private,
                                 permissions_from=self.path / permissions_from if permissions_from else None)
        else:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=self.fd)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            if os.name != "nt":
                os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())

    def replace(self, source, target):
        self.ensure_open()
        if os.name == "nt":
            os.replace(self.path / source, self.path / target)
        else:
            os.replace(source, target, src_dir_fd=self.fd, dst_dir_fd=self.fd)

    def unlink(self, name):
        self.ensure_open()
        if os.name == "nt":
            os.unlink(self.path / name)
        else:
            os.unlink(name, dir_fd=self.fd)

    def mkdir(self, name):
        self.ensure_open()
        if os.name == "nt":
            from .windows import private_directory
            private_directory(self.path / name)
        else:
            os.mkdir(name, mode=0o700, dir_fd=self.fd)

    def check_private(self, name=None):
        self.ensure_open()
        if os.name == "nt":
            from .windows import check_private, open_file, close_handle
            if name is None:
                check_private(self.fd)
            else:
                handle = open_file(self.path / name)
                try:
                    check_private(handle)
                finally:
                    close_handle(handle)
        else:
            info = self.stat(name) if name is not None else os.fstat(self.fd)
            mode = 0o600 if name is not None else 0o700
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != mode:
                raise ValueError(f"session {'file' if name else 'directory'} must be owned by you with mode {mode:04o}")

    def sync(self):
        self.ensure_open()
        if os.name != "nt":
            os.fsync(self.fd)

    def lock(self, name):
        """Hold a private, stable lock inode/handle until close or process exit."""
        self.ensure_open()
        if os.name == "nt":
            from .windows import file_descriptor
            fd = file_descriptor(self.path / name, create=True, private=True, exclusive=True)
        else:
            import fcntl
            fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600, dir_fd=self.fd)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
                    raise ValueError("lock file must be a private regular file without hard links")
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BaseException:
                os.close(fd)
                raise
        return os.fdopen(fd, "r+b", buffering=0)


def is_link(entry):
    try:
        return entry.is_symlink() or bool(getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
                                         & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except OSError:
        return True  # A disappeared or unreadable entry is never traversed.
