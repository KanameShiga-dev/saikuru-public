"""Windows process-tree records for the cancel evaluation. Read-only except kill_recorded.

Each record keeps the PID and its creation time, so a PID reused by an unrelated process
is never treated as the recorded one (and is never stopped).
"""
import ctypes
import subprocess
from ctypes import wintypes

TH32CS_SNAPPROCESS = 0x2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD), ('th32ProcessID', wintypes.DWORD),
                ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', wintypes.DWORD),
                ('cntThreads', wintypes.DWORD), ('th32ParentProcessID', wintypes.DWORD),
                ('pcPriClassBase', ctypes.c_long), ('dwFlags', wintypes.DWORD), ('szExeFile', ctypes.c_wchar * 260)]


_k32 = ctypes.WinDLL('kernel32', use_last_error=True)
_k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
STILL_ACTIVE = 259


def processes():
    """Current processes as {pid: (parent_pid, exe_name)}."""
    snapshot = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot in (None, INVALID_HANDLE_VALUE):
        raise OSError('Process snapshot failed')
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        result = {}
        ok = _k32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            result[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
            ok = _k32.Process32NextW(snapshot, ctypes.byref(entry))
        return result
    finally:
        _k32.CloseHandle(snapshot)


def creation_time(pid, running_only=False):
    """Creation time as a FILETIME integer, or None. With running_only, an exited process (still
    reachable while another process holds its handle) also gives None."""
    handle = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        if running_only:
            code = wintypes.DWORD()
            if not _k32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != STILL_ACTIVE:
                return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not _k32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return None
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        _k32.CloseHandle(handle)


def descendants(root_pid):
    """Records of every process below root_pid (not root itself), with creation times."""
    table = processes()
    children = {}
    for pid, (parent, _) in table.items():
        children.setdefault(parent, []).append(pid)
    root_created = creation_time(root_pid)
    records, queue = [], list(children.get(root_pid, []))
    while queue:
        pid = queue.pop(0)
        created = creation_time(pid)
        # A child created before its recorded parent is a PID-reuse artefact, not a real descendant.
        if pid == root_pid or (created and root_created and created < root_created):
            continue
        records.append({'pid': pid, 'parent': table[pid][0], 'name': table[pid][1], 'created': created})
        queue.extend(children.get(pid, []))
    return records


def alive(record):
    created = creation_time(record['pid'], running_only=True)
    if record.get('created') is None or created is None:
        table = processes()
        return record['pid'] in table and table[record['pid']][1] == record['name'] and record.get('created') is None
    return created == record['created']


def survivors(records):
    return [r for r in records if alive(r)]


def kill_recorded(records):
    """Stop only recorded processes that are still the same process (PID and creation time)."""
    stopped = []
    for record in records:
        if record.get('created') is not None and alive(record):
            subprocess.run(['taskkill', '/PID', str(record['pid']), '/F'], capture_output=True,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=15)
            stopped.append(record['pid'])
    return stopped
