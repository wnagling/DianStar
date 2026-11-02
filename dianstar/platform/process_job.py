"""Own the lifetime of a Windows command tree, including orphaned descendants.

Create suspended, assign to a kill-on-close job, then resume the primary thread.
This manages process lifetime, not filesystem or network isolation.
"""
import ctypes
from ctypes import wintypes


class WindowsJob:
    def __init__(self):
        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("min_working", ctypes.c_size_t), ("max_working", ctypes.c_size_t),
                        ("active", wintypes.DWORD), ("affinity", ctypes.c_size_t),
                        ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ("read_ops", "write_ops", "other_ops", "read", "write", "other")]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "OpenThread": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
            "CreateToolhelp32Snapshot": ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("Unable to create command process job")
        information = Extended()
        information.basic.flags = 0x2000 | 0x8 | 0x200  # kill-on-close, process count, job memory
        information.basic.active = 64
        information.job_memory = 4 * 1024**3
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(information), ctypes.sizeof(information)):
            self.close()
            raise OSError("Unable to configure command process job")

    def attach_and_resume(self, pid):
        process = self.kernel.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
        if not process:
            raise OSError("Unable to open suspended command")
        try:
            if not self.kernel.AssignProcessToJobObject(self.handle, process):
                raise OSError("Unable to own command process tree")
        finally:
            self.kernel.CloseHandle(process)
        class ThreadEntry(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("identifier", wintypes.DWORD),
                        ("owner", wintypes.DWORD), ("priority", wintypes.LONG), ("delta", wintypes.LONG), ("flags", wintypes.DWORD)]
        for name in ("Thread32First", "Thread32Next"):
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)], wintypes.BOOL
        snapshot = self.kernel.CreateToolhelp32Snapshot(0x4, 0)  # SNAPTHREAD
        if snapshot in (None, ctypes.c_void_p(-1).value):
            raise OSError("Unable to find suspended command thread")
        try:
            entry = ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            available = self.kernel.Thread32First(snapshot, ctypes.byref(entry))
            while available:
                if entry.owner == pid:
                    thread = self.kernel.OpenThread(0x2, False, entry.identifier)
                    if not thread:
                        raise OSError("Unable to open command thread")
                    try:
                        if self.kernel.ResumeThread(thread) != 1:
                            raise OSError("Unable to resume suspended command")
                        return
                    finally:
                        self.kernel.CloseHandle(thread)
                entry.size = ctypes.sizeof(entry)
                available = self.kernel.Thread32Next(snapshot, ctypes.byref(entry))
            raise OSError("Suspended command thread unavailable")
        finally:
            self.kernel.CloseHandle(snapshot)

    def stop(self):
        if self.handle and not self.kernel.TerminateJobObject(self.handle, 1):
            raise OSError("Unable to stop command process tree")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
