"""Windows 10+ no-reparse handles, private ACLs and job-bound commands.

Only imported on Windows. Uses documented Win32 APIs through the standard library.
"""

from contextlib import contextmanager
import ctypes as c
from ctypes import wintypes as w
from functools import lru_cache
import msvcrt
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time


kernel = c.WinDLL("kernel32", use_last_error=True)
security = c.WinDLL("advapi32", use_last_error=True)
SIZE = c.c_size_t
PTR = c.c_void_p


def api(library, name, result, *arguments):
    function = getattr(library, name)
    function.restype, function.argtypes = result, list(arguments)
    return function


close_handle = api(kernel, "CloseHandle", w.BOOL, w.HANDLE)
local_free = api(kernel, "LocalFree", PTR, PTR)
create_file = api(kernel, "CreateFileW", w.HANDLE, w.LPCWSTR, w.DWORD, w.DWORD,
                  PTR, w.DWORD, w.DWORD, w.HANDLE)
get_file_info = api(kernel, "GetFileInformationByHandle", w.BOOL, w.HANDLE, PTR)
get_final_path = api(kernel, "GetFinalPathNameByHandleW", w.DWORD, w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD)
get_long_path = api(kernel, "GetLongPathNameW", w.DWORD, w.LPCWSTR, w.LPWSTR, w.DWORD)


def long_path(path):
    """Expand DOS names without resolving away symlinks/reparse components."""
    path, tail = Path(path), []
    while True:
        buffer = c.create_unicode_buffer(32768)
        length = get_long_path(str(path), buffer, len(buffer))
        if length:
            if length >= len(buffer):
                raise ValueError('Windows path exceeds the supported buffer')
            return Path(buffer.value).joinpath(*reversed(tail))
        error = c.get_last_error()
        if error not in (2, 3) or path.parent == path:
            raise c.WinError(error)
        tail.append(path.name)
        path = path.parent


def checked(result):
    if not result:
        raise c.WinError(c.get_last_error())
    return result


class FileInfo(c.Structure):
    _fields_ = [("attributes", w.DWORD), ("creation", w.FILETIME), ("access", w.FILETIME),
                ("write", w.FILETIME), ("volume", w.DWORD), ("size_high", w.DWORD),
                ("size_low", w.DWORD), ("links", w.DWORD), ("index_high", w.DWORD), ("index_low", w.DWORD)]


class SecurityAttributes(c.Structure):
    _fields_ = [("length", w.DWORD), ("descriptor", PTR), ("inherit", w.BOOL)]


@lru_cache(maxsize=1)
def user_sid():
    token, needed = w.HANDLE(), w.DWORD()
    current = api(kernel, "GetCurrentProcess", w.HANDLE)
    open_token = api(security, "OpenProcessToken", w.BOOL, w.HANDLE, w.DWORD, c.POINTER(w.HANDLE))
    token_info = api(security, "GetTokenInformation", w.BOOL, w.HANDLE, w.DWORD, PTR, w.DWORD, c.POINTER(w.DWORD))
    sid_string = api(security, "ConvertSidToStringSidW", w.BOOL, PTR, c.POINTER(w.LPWSTR))
    checked(open_token(current(), 8, c.byref(token)))
    try:
        token_info(token, 1, None, 0, c.byref(needed))
        buffer = c.create_string_buffer(needed.value)
        checked(token_info(token, 1, buffer, needed, c.byref(needed)))
        sid = c.cast(buffer, c.POINTER(PTR))[0]
        text = w.LPWSTR()
        checked(sid_string(sid, c.byref(text)))
        try:
            return text.value
        finally:
            local_free(c.cast(text, PTR))
    finally:
        close_handle(token)


@contextmanager
def private_attributes(directory=False):
    descriptor = PTR()
    convert = api(security, "ConvertStringSecurityDescriptorToSecurityDescriptorW", w.BOOL,
                  w.LPCWSTR, w.DWORD, c.POINTER(PTR), c.POINTER(w.DWORD))
    sid = user_sid()
    flags = "OICI" if directory else ""
    checked(convert(f"O:{sid}D:P(A;{flags};FA;;;{sid})", 1, c.byref(descriptor), None))
    try:
        yield SecurityAttributes(c.sizeof(SecurityAttributes), descriptor, False)
    finally:
        local_free(descriptor)


def validate_component(name):
    device = name.split(".", 1)[0].upper()
    if (not name or name.endswith((".", " ")) or any(ch in name for ch in ':<>"|?*\\')
            or any(ord(ch) < 32 for ch in name)
            or device in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
            or re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", device)):
        raise ValueError("invalid Windows path component (device, stream or ambiguous name)")


def open_file(path, *, directory=False, create=False, attributes=None, exclusive=False, allow_git_metadata=False):
    if allow_git_metadata and (create or exclusive):
        raise ValueError("Git metadata capability is read-only")
    # FILE_LIST_DIRECTORY counts as read access for share-delete enforcement;
    # attribute-only handles do not pin directories against renaming.
    access = 0x20000 | (0x81 if directory else (0x40000000 if create else 0x80000000))
    if exclusive:
        access |= 0xC0000000
    # Deny deletion/renaming while handles are open. Parents stay pinned during approval.
    handle = create_file(str(path), access, 0 if exclusive else 3, c.byref(attributes) if attributes else None,
                         4 if exclusive else (1 if create else 3), 0x00200000 | (0x02000000 if directory else 0), None)
    if handle == c.c_void_p(-1).value:
        raise c.WinError(c.get_last_error())
    try:
        info = FileInfo()
        checked(get_file_info(handle, c.byref(info)))
        if info.attributes & 0x400 or bool(info.attributes & 0x10) != directory:
            raise ValueError("only ordinary files/directories are supported; reparse points are rejected")
        if exclusive and info.links != 1:
            raise ValueError("lock files must not have hard links")
        length = get_final_path(handle, None, 0, 0)
        checked(length)
        final = c.create_unicode_buffer(length + 1)
        checked(get_final_path(handle, final, len(final), 0))
        # Long names expose protected files addressed through DOS 8.3 aliases.
        for part in Path(final.value).parts[1:]:
            lowered = part.lower()
            if (lowered == ".git" and not allow_git_metadata) or lowered == ".env" or lowered.startswith(".env."):
                raise ValueError("path names a protected component")
        return handle
    except BaseException:
        close_handle(handle)
        raise


@contextmanager
def existing_attributes(path):
    """Preserve a replaced file's owner and DACL, including inheritance protection."""
    handle, descriptor = open_file(path), PTR()
    get_security = api(security, "GetSecurityInfo", w.DWORD, w.HANDLE, w.DWORD, w.DWORD,
                       PTR, PTR, PTR, PTR, c.POINTER(PTR))
    try:
        # Include the owner: elevated runners otherwise assign the default
        # Administrators owner to the replacement even with the correct DACL.
        code = get_security(handle, 1, 5, None, None, None, None, c.byref(descriptor))
        if code:
            raise c.WinError(code)
        yield SecurityAttributes(c.sizeof(SecurityAttributes), descriptor, False)
    finally:
        if descriptor:
            local_free(descriptor)
        close_handle(handle)


def file_descriptor(path, *, create=False, private=False, permissions_from=None, exclusive=False, allow_git_metadata=False):
    if allow_git_metadata and (create or exclusive or permissions_from is not None):
        raise ValueError("Git metadata capability is read-only")
    if create and private:
        with private_attributes() as attributes:
            handle = open_file(path, create=True, attributes=attributes, exclusive=exclusive)
    elif create and permissions_from is not None:
        with existing_attributes(permissions_from) as attributes:
            handle = open_file(path, create=True, attributes=attributes)
    else:
        handle = open_file(path, create=create, exclusive=exclusive, allow_git_metadata=allow_git_metadata)
    try:
        if private:
            check_private(handle)
        return msvcrt.open_osfhandle(handle, os.O_BINARY | (os.O_RDWR if exclusive else (os.O_WRONLY if create else os.O_RDONLY)))
    except BaseException:
        close_handle(handle)
        raise


def private_directory(path):
    create_directory = api(kernel, "CreateDirectoryW", w.BOOL, w.LPCWSTR, PTR)
    with private_attributes(directory=True) as attributes:
        checked(create_directory(str(path), c.byref(attributes)))


def seal_editor_result(directory):
    """Tighten only an owned temporary, single-link draft after atomic editor save."""
    directory.ensure_open()
    directory.check_private()
    if not re.fullmatch(r"sprig-editor-[0-9a-f]{32}",directory.path.name):
        raise ValueError("editor ACL sealing requires its private temporary directory")
    handle=create_file(str(directory.path/'draft.md'),0x80000000 | 0xE0000,0,None,3,0x00200000,None)
    if handle==c.c_void_p(-1).value:
        raise c.WinError(c.get_last_error())
    try:
        info=FileInfo()
        checked(get_file_info(handle,c.byref(info)))
        if info.attributes & (0x400 | 0x10) or info.links!=1 or (info.size_high<<32 | info.size_low)>2*1024*1024:
            raise ValueError("editor result must be an independent bounded regular file; no ACL was changed")
        owner,dacl=PTR(),PTR()
        present,defaulted=w.BOOL(),w.BOOL()
        get_owner=api(security,"GetSecurityDescriptorOwner",w.BOOL,PTR,c.POINTER(PTR),c.POINTER(w.BOOL))
        get_dacl=api(security,"GetSecurityDescriptorDacl",w.BOOL,PTR,c.POINTER(w.BOOL),c.POINTER(PTR),c.POINTER(w.BOOL))
        set_security=api(security,"SetSecurityInfo",w.DWORD,w.HANDLE,w.DWORD,w.DWORD,PTR,PTR,PTR,PTR)
        with private_attributes() as attributes:
            checked(get_owner(attributes.descriptor,c.byref(owner),c.byref(defaulted)))
            checked(get_dacl(attributes.descriptor,c.byref(present),c.byref(dacl),c.byref(defaulted)))
            if not present.value or not dacl or not owner:
                raise ValueError("private editor security descriptor is unavailable")
            code=set_security(handle,1,0x80000005,owner,None,dacl,None)
            if code:
                raise c.WinError(code)
        check_private(handle)  # Keep the ordinary strict reader unchanged.
    finally:
        close_handle(handle)


def check_private(handle):
    owner, dacl, descriptor, sid = PTR(), PTR(), PTR(), PTR()
    get_security = api(security, "GetSecurityInfo", w.DWORD, w.HANDLE, w.DWORD, w.DWORD,
                       c.POINTER(PTR), PTR, c.POINTER(PTR), PTR, c.POINTER(PTR))
    code = get_security(handle, 1, 5, c.byref(owner), None, c.byref(dacl), None, c.byref(descriptor))
    if code:
        raise c.WinError(code)
    convert_sid = api(security, "ConvertStringSidToSidW", w.BOOL, w.LPCWSTR, c.POINTER(PTR))
    equal = api(security, "EqualSid", w.BOOL, PTR, PTR)
    get_ace = api(security, "GetAce", w.BOOL, PTR, w.DWORD, c.POINTER(PTR))
    get_control = api(security, "GetSecurityDescriptorControl", w.BOOL, PTR, c.POINTER(w.WORD), c.POINTER(w.DWORD))
    try:
        checked(convert_sid(user_sid(), c.byref(sid)))
        control, revision = w.WORD(), w.DWORD()
        checked(get_control(descriptor, c.byref(control), c.byref(revision)))
        if not owner or not equal(owner, sid) or not dacl or not control.value & 0x1000:
            raise ValueError("session requires a protected owner-only Windows ACL")
        count = c.cast(dacl.value + 4, c.POINTER(w.WORD))[0]
        allowed = False
        for index in range(count):
            ace = PTR()
            checked(get_ace(dacl, index, c.byref(ace)))
            kind = c.cast(ace, c.POINTER(c.c_ubyte))[0]
            if kind == 1:  # Deny ACEs cannot grant another user access.
                continue
            if kind != 0 or not equal(ace.value + 8, sid):
                raise ValueError("session ACL grants access outside the current user")
            allowed = True
        if not allowed:
            raise ValueError("session ACL does not grant the current user access")
    finally:
        if sid:
            local_free(sid)
        local_free(descriptor)


class BasicLimits(c.Structure):
    _fields_ = [("process_time", c.c_longlong), ("job_time", c.c_longlong), ("flags", w.DWORD),
                ("min_working_set", SIZE), ("max_working_set", SIZE), ("active", w.DWORD),
                ("affinity", SIZE), ("priority", w.DWORD), ("scheduling", w.DWORD)]


class ExtendedLimits(c.Structure):
    _fields_ = [("basic", BasicLimits), ("io", c.c_ulonglong * 6), ("process_memory", SIZE),
                ("job_memory", SIZE), ("peak_process_memory", SIZE), ("peak_job_memory", SIZE)]


class StartupInfo(c.Structure):
    _fields_ = [("size", w.DWORD), ("reserved", w.LPWSTR), ("desktop", w.LPWSTR), ("title", w.LPWSTR),
                ("x", w.DWORD), ("y", w.DWORD), ("width", w.DWORD), ("height", w.DWORD),
                ("xchars", w.DWORD), ("ychars", w.DWORD), ("fill", w.DWORD), ("flags", w.DWORD),
                ("show", w.WORD), ("reserved_size", w.WORD), ("reserved_data", PTR),
                ("stdin", w.HANDLE), ("stdout", w.HANDLE), ("stderr", w.HANDLE)]


class StartupEx(c.Structure):
    _fields_ = [("info", StartupInfo), ("attributes", PTR)]


class ProcessInfo(c.Structure):
    _fields_ = [("process", w.HANDLE), ("thread", w.HANDLE), ("pid", w.DWORD), ("tid", w.DWORD)]


def stop_job(job):
    """Termination is asynchronous: wait for every descendant to release resources."""
    class Accounting(c.Structure):
        _fields_ = [("times", c.c_longlong * 4), ("faults", w.DWORD), ("total", w.DWORD),
                    ("active", w.DWORD), ("terminated", w.DWORD)]

    terminate = api(kernel, "TerminateJobObject", w.BOOL, w.HANDLE, w.UINT)
    query = api(kernel, "QueryInformationJobObject", w.BOOL, w.HANDLE, c.c_int, PTR, w.DWORD, PTR)
    open_process = api(kernel, "OpenProcess", w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
    wait = api(kernel, "WaitForSingleObject", w.DWORD, w.HANDLE, w.DWORD)
    sleep = api(kernel, "Sleep", None, w.DWORD)
    handles = []
    try:
        # ActiveProcesses can reach zero before final resource teardown. Retain
        # process handles so we can also wait for the processes to signal exit.
        for capacity in (64, 4096, 65536):
            class ProcessIds(c.Structure):
                _fields_ = [("assigned", w.DWORD), ("count", w.DWORD), ("ids", SIZE * capacity)]
            ids = ProcessIds()
            if query(job, 3, c.byref(ids), c.sizeof(ids), None):
                break
            if c.get_last_error() != 234 or capacity == 65536:
                raise c.WinError(c.get_last_error())
        for pid in ids.ids[:ids.count]:
            handle = open_process(0x100000, False, pid)  # SYNCHRONIZE
            if handle:
                handles.append(handle)
            elif c.get_last_error() != 87:  # Process already exited.
                raise c.WinError(c.get_last_error())
        checked(terminate(job, 1))
        deadline = time.monotonic() + 5
        for handle in handles:
            if wait(handle, max(0, int((deadline - time.monotonic()) * 1000))) != 0:
                raise OSError("Windows command cleanup did not finish within five seconds")
        while True:
            accounting = Accounting()
            checked(query(job, 1, c.byref(accounting), c.sizeof(accounting), None))
            if accounting.active == 0:
                return
            if time.monotonic() >= deadline:
                raise OSError("Windows command cleanup did not finish within five seconds")
            sleep(10)
    finally:
        for handle in handles:
            close_handle(handle)


def check_command_launcher(executable):
    """App-execution brokers can launch processes outside the caller's Job."""
    def reject_alias(path):
        try:
            tag = os.lstat(path).st_reparse_tag
        except FileNotFoundError:
            return
        if tag == stat.IO_REPARSE_TAG_APPEXECLINK:
            raise ValueError("Windows app execution aliases cannot guarantee process cleanup; "
                             "use uv sync --managed-python --python 3.13")

    executable = Path(executable).absolute()
    reject_alias(executable)
    if not re.fullmatch(r"python(?:w|[0-9.]*)\.exe", executable.name, re.IGNORECASE):
        return
    # CPython's venv redirector resolves its interpreter from pyvenv.cfg's home.
    # uv can otherwise select a Microsoft Store alias as that home.
    for parent in (executable.parent, executable.parent.parent):
        try:
            with (parent / "pyvenv.cfg").open("rb") as config:
                data = config.read(65537)
        except FileNotFoundError:
            continue
        if len(data) > 65536:
            raise ValueError("Python virtual environment configuration is too large")
        for line in data.decode("utf-8").splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip().lower() == "home":
                home = Path(value.strip())
                reject_alias(home / executable.name)
                reject_alias(home / "python.exe")
        break


class StdioProcess:
    """Launch into a kill-on-close job before any child instruction executes."""
    def __init__(self, argv, directory, env, *, console=False):
        self.job, self.process = None, None
        self.stdin = self.stdout = self.stderr = None
        program = argv[0]
        if os.path.dirname(program) and not os.path.isabs(program):
            program = str(Path(directory) / program)
        executable = shutil.which(program, path=env.get("PATH"))
        if executable is None:
            raise FileNotFoundError("command executable was not found")
        if Path(executable).suffix.lower() in {".bat", ".cmd"}:
            raise ValueError("batch launchers require explicit cmd.exe /c in the approved argv")
        check_command_launcher(executable)
        command = subprocess.list2cmdline(argv)
        if len(command) >= 32767:
            raise ValueError("Windows command line exceeds 32766 characters")
        create_job = api(kernel, "CreateJobObjectW", w.HANDLE, PTR, w.LPCWSTR)
        set_job = api(kernel, "SetInformationJobObject", w.BOOL, w.HANDLE, c.c_int, PTR, w.DWORD)
        init = api(kernel, "InitializeProcThreadAttributeList", w.BOOL, PTR, w.DWORD, w.DWORD, c.POINTER(SIZE))
        update = api(kernel, "UpdateProcThreadAttribute", w.BOOL, PTR, w.DWORD, SIZE, PTR, SIZE, PTR, PTR)
        delete = api(kernel, "DeleteProcThreadAttributeList", None, PTR)
        duplicate = api(kernel, "DuplicateHandle", w.BOOL, w.HANDLE, w.HANDLE, w.HANDLE, c.POINTER(w.HANDLE), w.DWORD, w.BOOL, w.DWORD)
        create = api(kernel, "CreateProcessW", w.BOOL, w.LPCWSTR, w.LPWSTR, PTR, PTR, w.BOOL, w.DWORD, PTR, w.LPCWSTR, PTR, c.POINTER(ProcessInfo))
        set_mode = api(kernel, "SetNamedPipeHandleState", w.BOOL, w.HANDLE, c.POINTER(w.DWORD), PTR, PTR)
        current = api(kernel, "GetCurrentProcess", w.HANDLE)()
        inherited, child_fds, attributes, info = [], [], None, ProcessInfo()
        attributes_initialized = False
        try:
            self.job = checked(create_job(None, None))
            limits = ExtendedLimits()
            limits.basic.flags = 0x2000
            checked(set_job(self.job, 9, c.byref(limits), c.sizeof(limits)))
            for descriptor, name in enumerate(("stdin", "stdout", "stderr")):
                if console:
                    child = descriptor
                else:
                    read_fd, write_fd = os.pipe()
                    parent, child = (write_fd, read_fd) if name == "stdin" else (read_fd, write_fd)
                    setattr(self, name, parent)
                    child_fds.append(child)
                handle = w.HANDLE()
                checked(duplicate(current, msvcrt.get_osfhandle(child), current, c.byref(handle), 0, True, 2))
                inherited.append(handle.value)
                if not console:
                    mode = w.DWORD(1)  # PIPE_NOWAIT, including Python 3.11.
                    checked(set_mode(msvcrt.get_osfhandle(parent), c.byref(mode), None, None))
            size = SIZE()
            init(None, 2, 0, c.byref(size))
            attributes = c.create_string_buffer(size.value)
            checked(init(attributes, 2, 0, c.byref(size)))
            attributes_initialized = True
            jobs, handles = (w.HANDLE * 1)(self.job), (w.HANDLE * 3)(*inherited)
            checked(update(attributes, 0, 0x2000D, jobs, c.sizeof(jobs), None, None))
            checked(update(attributes, 0, 0x20002, handles, c.sizeof(handles), None, None))
            startup = StartupEx()
            startup.info.size, startup.info.flags = c.sizeof(startup), 0x100 if console else 0x101
            startup.info.stdin, startup.info.stdout, startup.info.stderr = inherited
            startup.attributes = c.cast(attributes, PTR)
            environment = c.create_unicode_buffer("\0".join(f"{key}={value}" for key, value in sorted(env.items(), key=lambda item: item[0].upper())) + "\0\0")
            checked(create(executable, c.create_unicode_buffer(command), None, None, True,
                           (0 if console else 0x08000000) | 0x00080000 | 0x400, environment, str(directory), c.byref(startup), c.byref(info)))
            self.process, info.process = info.process, None
        except BaseException:
            self.close()
            raise
        finally:
            if info.thread:
                close_handle(info.thread)
            if attributes_initialized:
                delete(attributes)
            for handle in inherited:
                close_handle(handle)
            for fd in child_fds:
                os.close(fd)

    def poll(self):
        if self.process is None:
            return 0
        wait = api(kernel, "WaitForSingleObject", w.DWORD, w.HANDLE, w.DWORD)
        if wait(self.process, 0) == 0:
            code = w.DWORD()
            checked(api(kernel, "GetExitCodeProcess", w.BOOL, w.HANDLE, c.POINTER(w.DWORD))(self.process, c.byref(code)))
            return code.value
        return None

    def read(self, channel):
        data, count = c.create_string_buffer(4096), w.DWORD()
        read = api(kernel, "ReadFile", w.BOOL, w.HANDLE, PTR, w.DWORD, c.POINTER(w.DWORD), PTR)
        if not read(msvcrt.get_osfhandle(getattr(self, channel)), data, 4096, c.byref(count), None):
            code = c.get_last_error()
            if code == 232:  # PIPE_NOWAIT has no data; CRT errno differs on Python 3.11.
                return None
            if code in (109, 233):
                return b""
            raise c.WinError(code)
        return data.raw[:count.value]

    def write(self, raw):
        count = w.DWORD()
        data = c.create_string_buffer(raw)
        write = api(kernel, "WriteFile", w.BOOL, w.HANDLE, PTR, w.DWORD, c.POINTER(w.DWORD), PTR)
        if not write(msvcrt.get_osfhandle(self.stdin), data, len(raw), c.byref(count), None):
            code = c.get_last_error()
            if code == 232:
                return 0
            raise c.WinError(code)
        return count.value

    def close(self):
        if self.stdin is not None:
            os.close(self.stdin)
            self.stdin = None
        until = time.monotonic() + .2
        while self.process is not None and self.poll() is None and time.monotonic() < until:
            time.sleep(.01)
        try:
            if self.job:
                stop_job(self.job)
        finally:
            if self.job:
                close_handle(self.job)
                self.job = None
            if self.process:
                api(kernel, "WaitForSingleObject", w.DWORD, w.HANDLE, w.DWORD)(self.process, 5000)
                close_handle(self.process)
                self.process = None
            for name in ("stdout", "stderr"):
                fd = getattr(self, name)
                if fd is not None:
                    os.close(fd)
                    setattr(self, name, None)


def run_command(argv, directory, env, seconds, cap, cancel_event=None, on_output=None):
    """Create directly in a kill-on-close Job Object; bound output without threads."""
    program = argv[0]
    if os.path.dirname(program) and not os.path.isabs(program):
        program = str(Path(directory) / program)
    executable = shutil.which(program, path=env.get("PATH"))
    if executable is None:
        raise FileNotFoundError("command executable was not found")
    if Path(executable).suffix.lower() in {".bat", ".cmd"}:
        raise ValueError("batch files require explicit cmd.exe /c approval")
    check_command_launcher(executable)
    command = subprocess.list2cmdline(argv)
    if len(command) >= 32767:
        raise ValueError("Windows command line exceeds 32766 characters")
    create_job = api(kernel, "CreateJobObjectW", w.HANDLE, PTR, w.LPCWSTR)
    set_job = api(kernel, "SetInformationJobObject", w.BOOL, w.HANDLE, c.c_int, PTR, w.DWORD)
    init_attributes = api(kernel, "InitializeProcThreadAttributeList", w.BOOL, PTR, w.DWORD, w.DWORD, c.POINTER(SIZE))
    update_attributes = api(kernel, "UpdateProcThreadAttribute", w.BOOL, PTR, w.DWORD, SIZE, PTR, SIZE, PTR, PTR)
    delete_attributes = api(kernel, "DeleteProcThreadAttributeList", None, PTR)
    create_process = api(kernel, "CreateProcessW", w.BOOL, w.LPCWSTR, w.LPWSTR, PTR, PTR, w.BOOL,
                         w.DWORD, PTR, w.LPCWSTR, PTR, c.POINTER(ProcessInfo))
    wait = api(kernel, "WaitForSingleObject", w.DWORD, w.HANDLE, w.DWORD)
    exit_code = api(kernel, "GetExitCodeProcess", w.BOOL, w.HANDLE, c.POINTER(w.DWORD))
    peek = api(kernel, "PeekNamedPipe", w.BOOL, w.HANDLE, PTR, w.DWORD, PTR, c.POINTER(w.DWORD), PTR)
    duplicate = api(kernel, "DuplicateHandle", w.BOOL, w.HANDLE, w.HANDLE, w.HANDLE, c.POINTER(w.HANDLE), w.DWORD, w.BOOL, w.DWORD)
    current = api(kernel, "GetCurrentProcess", w.HANDLE)()
    job = checked(create_job(None, None))
    read_fd = write_fd = None
    inherited, attributes, process = [], None, ProcessInfo()
    try:
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        checked(set_job(job, 9, c.byref(limits), c.sizeof(limits)))
        read_fd, write_fd = os.pipe()
        with open(os.devnull, "rb") as null:
            for handle in (msvcrt.get_osfhandle(write_fd), msvcrt.get_osfhandle(null.fileno())):
                child = w.HANDLE()
                checked(duplicate(current, handle, current, c.byref(child), 0, True, 2))
                inherited.append(child.value)
        size = SIZE()
        init_attributes(None, 2, 0, c.byref(size))
        attributes = c.create_string_buffer(size.value)
        checked(init_attributes(attributes, 2, 0, c.byref(size)))
        jobs = (w.HANDLE * 1)(job)
        handles = (w.HANDLE * 2)(*inherited)
        checked(update_attributes(attributes, 0, 0x2000D, jobs, c.sizeof(jobs), None, None))
        checked(update_attributes(attributes, 0, 0x20002, handles, c.sizeof(handles), None, None))
        startup = StartupEx()
        startup.info.size = c.sizeof(startup)
        startup.info.flags = 0x101  # USESTDHANDLES | USESHOWWINDOW (hidden)
        startup.info.stdin = inherited[1]
        startup.info.stdout = startup.info.stderr = inherited[0]
        startup.attributes = c.cast(attributes, PTR)
        environment = c.create_unicode_buffer("\0".join(f"{key}={value}" for key, value in sorted(env.items(), key=lambda item: item[0].upper())) + "\0\0")
        checked(create_process(executable, c.create_unicode_buffer(command), None, None, True,
                               0x08000000 | 0x00080000 | 0x400, environment, str(directory),
                               c.byref(startup), c.byref(process)))
        close_handle(process.thread)
        process.thread = None
        for handle in inherited:
            close_handle(handle)
        inherited.clear()
        os.close(write_fd)
        write_fd = None
        output, reason, deadline = bytearray(), None, time.monotonic() + seconds
        pipe = msvcrt.get_osfhandle(read_fd)
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise KeyboardInterrupt
            if time.monotonic() >= deadline:
                reason = "timeout"
                break
            available = w.DWORD()
            if not peek(pipe, None, 0, None, c.byref(available), None):
                code = c.get_last_error()
                if code not in {109, 233}:  # Broken pipe / no data.
                    raise c.WinError(code)
                if wait(process.process, 0) == 0:
                    break
            elif available.value:
                chunk = os.read(read_fd, min(available.value, cap - len(output) + 1, 65536))
                space = cap - len(output)
                output.extend(chunk[:space])
                if on_output:
                    on_output(chunk[:space])
                if len(chunk) > space:
                    reason = "output_limit"
                    break
                continue
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        stop_job(job)  # Also await descendants holding the output pipe or cwd.
        close_handle(job)
        job = None
        checked(wait(process.process, 5000) == 0)
        code = w.DWORD()
        checked(exit_code(process.process, c.byref(code)))
        return {"approved": True, "returncode": code.value, "output": output.decode("utf-8", errors="replace"),
                "timed_out": reason == "timeout", "output_limited": reason == "output_limit"}
    finally:
        try:
            if job:
                stop_job(job)
        finally:
            if job:
                close_handle(job)
            if process.process:
                wait(process.process, 5000)
                close_handle(process.process)
            if process.thread:
                close_handle(process.thread)
            if attributes:
                delete_attributes(attributes)
            for handle in inherited:
                close_handle(handle)
            for fd in (read_fd, write_fd):
                if fd is not None:
                    os.close(fd)
