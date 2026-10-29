"""macOS process inspection using the public libproc/sysctl interfaces.

Layouts/constants follow the macOS SDK's sys/proc_info.h and sys/sysctl.h.
No command output or parent environment is used to infer child ownership.
"""

from __future__ import annotations

import ctypes as c
import errno
import os
import subprocess
from functools import lru_cache


class _BSDInfo(c.Structure):
    _fields_ = (
        [
            (name, c.c_uint32)
            for name in (
                "flags",
                "status",
                "xstatus",
                "pid",
                "ppid",
                "uid",
                "gid",
                "ruid",
                "rgid",
                "svuid",
                "svgid",
                "reserved",
            )
        ]
        + [("comm", c.c_char * 16), ("name", c.c_char * 32)]
        + [(name, c.c_uint32) for name in ("nfiles", "pgid", "jobc", "tdev", "tpgid")]
        + [("nice", c.c_int32), ("start_seconds", c.c_uint64), ("start_microseconds", c.c_uint64)]
    )


class _VnodeStat(c.Structure):
    _fields_ = (
        [
            ("dev", c.c_uint32),
            ("mode", c.c_uint16),
            ("nlink", c.c_uint16),
            ("ino", c.c_uint64),
            ("uid", c.c_uint32),
            ("gid", c.c_uint32),
        ]
        + [
            (name, c.c_int64)
            for name in (
                "atime",
                "atimensec",
                "mtime",
                "mtimensec",
                "ctime",
                "ctimensec",
                "birthtime",
                "birthtimensec",
                "size",
                "blocks",
            )
        ]
        + [
            ("blksize", c.c_int32),
            ("flags", c.c_uint32),
            ("gen", c.c_uint32),
            ("rdev", c.c_uint32),
            ("spare", c.c_int64 * 2),
        ]
    )


class _VnodePath(c.Structure):
    _fields_ = [
        ("stat", _VnodeStat),
        ("type", c.c_int),
        ("pad", c.c_int),
        ("fsid", c.c_int32 * 2),
        ("path", c.c_char * 1024),
    ]


class _VnodePaths(c.Structure):
    _fields_ = [("cwd", _VnodePath), ("root", _VnodePath)]


class _TaskInfo(c.Structure):
    _fields_ = [
        (name, c.c_uint64)
        for name in ("virtual", "resident", "user", "system", "threads_user", "threads_system")
    ] + [("counters", c.c_int32 * 12)]


@lru_cache(maxsize=1)
def _libc():
    lib = c.CDLL(None, use_errno=True)
    lib.proc_pidinfo.argtypes = [c.c_int, c.c_int, c.c_uint64, c.c_void_p, c.c_int]
    lib.proc_pidinfo.restype = c.c_int
    lib.proc_listpids.argtypes = [c.c_uint32, c.c_uint32, c.c_void_p, c.c_int]
    lib.proc_listpids.restype = c.c_int
    lib.sysctl.argtypes = [
        c.POINTER(c.c_int),
        c.c_uint,
        c.c_void_p,
        c.POINTER(c.c_size_t),
        c.c_void_p,
        c.c_size_t,
    ]
    lib.sysctl.restype = c.c_int
    return lib


def _pidinfo(pid, flavor, structure):
    value = structure()
    c.set_errno(0)
    size = _libc().proc_pidinfo(pid, flavor, 0, c.byref(value), c.sizeof(value))
    if size != c.sizeof(value):
        code = c.get_errno() or errno.EIO
        raise OSError(code, "macOS process inspection failed")
    return value


def _arguments(pid):
    capacity = os.sysconf("SC_ARG_MAX")
    if not 0 < capacity <= 16 * 1024 * 1024:
        raise ValueError("Invalid macOS argument buffer size")
    data = c.create_string_buffer(capacity)
    size = c.c_size_t(capacity)
    mib = (c.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
    if _libc().sysctl(mib, 3, data, c.byref(size), None, 0) != 0:
        raise OSError(c.get_errno(), "Cannot inspect macOS process arguments")
    return _decode_arguments(data.raw[: size.value])


def _decode_arguments(data):
    integer_size = c.sizeof(c.c_int)
    if len(data) < integer_size:
        raise ValueError("Truncated macOS process arguments")
    argc = c.c_int.from_buffer_copy(data[:integer_size]).value
    if not 0 < argc <= len(data):
        raise ValueError("Invalid macOS argument count")
    offset = data.index(b"\0", integer_size) + 1  # executable path, then padding
    while offset < len(data) and data[offset] == 0:
        offset += 1
    args = []
    for _ in range(argc):
        end = data.index(b"\0", offset)
        args.append(data[offset:end])
        offset = end + 1
    environment = {}
    for item in data[offset:].split(b"\0"):
        if not item:
            break
        if b"=" in item:
            key, value = item.split(b"=", 1)
            environment[key] = value
    return b"\0".join(args).decode(errors="replace") + "\0", environment


def _start(info):
    return f"{info.start_seconds}:{info.start_microseconds}"


def process_start(pid):
    """Stable identity for admission leases; unreadable live processes fail closed."""
    try:
        info = _pidinfo(pid, 3, _BSDInfo)
        return None if info.status == 5 else _start(info)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return None
        raise


def process_identity(pid):
    try:
        before = _pidinfo(pid, 3, _BSDInfo)  # PROC_PIDTBSDINFO
        if before.pid != pid or before.status == 5:  # SZOMB
            return None
        sid = os.getsid(pid)
        cwd = os.fsdecode(_pidinfo(pid, 9, _VnodePaths).cwd.path)  # PROC_PIDVNODEPATHINFO
        cmdline, environment = _arguments(pid)
        after = _pidinfo(pid, 3, _BSDInfo)
        if not cwd or (_start(before), before.pgid, sid) != (_start(after), after.pgid, os.getsid(pid)):
            return None
        if after.status == 5:
            return None
        return {
            "pid": pid,
            "start": _start(before),
            "pgid": before.pgid,
            "sid": sid,
            "cwd": cwd,
            "cmdline": cmdline,
            "token": environment.get(b"TEA_DEV_TOKEN", b"").decode(),
            "owner": environment.get(b"TEA_DEV_ROOT", b"").decode(),
        }
    except (OSError, ValueError, IndexError):
        return None


def group_pids(pgid):
    lib = _libc()
    size = lib.proc_listpids(2, pgid, None, 0)  # PROC_PGRP_ONLY; returns bytes
    if size <= 0:
        return []
    for _ in range(3):
        buffer = (c.c_int * (size // c.sizeof(c.c_int) + 32))()
        count = lib.proc_listpids(2, pgid, buffer, c.sizeof(buffer))
        if count < 0:
            return []
        if count < c.sizeof(buffer):
            return [pid for pid in buffer[: count // c.sizeof(c.c_int)] if pid > 1]
        size = c.sizeof(buffer) * 2
    return []  # An incomplete scan cannot prove ownership.


def resident_bytes(pid):
    """Resident bytes, sampled by the parent of an isolated parser process."""
    return _pidinfo(pid, 4, _TaskInfo).resident  # PROC_PIDTASKINFO


def process_arguments(pid):
    """Private inspection for ownership checks; callers must not log the environment."""
    return _arguments(pid)


def all_pids():
    lib = _libc()
    size = lib.proc_listpids(1, 0, None, 0)  # PROC_ALL_PIDS
    for _ in range(3):
        buffer = (c.c_int * (max(size, 0) // c.sizeof(c.c_int) + 64))()
        count = lib.proc_listpids(1, 0, buffer, c.sizeof(buffer))
        if count < 0:
            raise OSError("Cannot enumerate macOS processes")
        if count < c.sizeof(buffer):
            return [pid for pid in buffer[: count // c.sizeof(c.c_int)] if pid > 0]
        size = c.sizeof(buffer) * 2
    raise OSError("macOS process enumeration exceeded its buffer")


def _lsof(*arguments):
    result = subprocess.run(
        ["/usr/sbin/lsof", "-nP", *arguments], capture_output=True, timeout=10, check=False
    )
    if result.returncode not in (0, 1) or result.stderr or len(result.stdout) > 8 * 1024 * 1024:
        raise OSError("Cannot inspect macOS file ownership")
    return [field.lstrip(b"\n") for field in result.stdout.split(b"\0") if field.strip(b"\n")]


def open_files():
    pid = None
    for field in _lsof("-u", str(os.getuid()), "-F0pn"):
        if field.startswith(b"p"):
            pid = int(field[1:])
        elif field.startswith(b"n") and pid and field[1:2] == b"/":
            yield pid, os.fsdecode(field[1:])
