"""Fixed host-supplied AppContainer launcher; no application/project imports."""
import ctypes
from ctypes import wintypes as W
import json
import subprocess
import sys

LAUNCH_NONCE = "unbound"
LAUNCH_PHASE = "before"


def main():
    global LAUNCH_NONCE, LAUNCH_PHASE
    config = json.loads(sys.stdin.buffer.read(262145))
    if not isinstance(config, dict) or len(json.dumps(config)) > 262144:
        raise ValueError("Invalid launch description")
    LAUNCH_NONCE = config["nonce"]
    api = WindowsSecurity()  # prepended from windows_acl.py by the host
    token, process, thread = W.HANDLE(), None, None
    allocated, desktop, descriptor, null, startup = [], None, ctypes.c_void_p(), None, None
    try:
        api.require(api.advapi.OpenProcessToken(api.kernel.GetCurrentProcess(), 8, ctypes.byref(token)))
        if ctypes.cast(api.token_information(token, 20), ctypes.POINTER(W.DWORD)).contents.value:
            raise OSError("Elevated command host refused")
        class Attributes(ctypes.Structure):
            _fields_ = [("length", W.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", W.BOOL)]
        class Startup(ctypes.Structure):
            _fields_ = [("cb", W.DWORD), ("reserved", W.LPWSTR), ("desktop", W.LPWSTR), ("title", W.LPWSTR),
                ("x", W.DWORD), ("y", W.DWORD), ("cx", W.DWORD), ("cy", W.DWORD), ("xchars", W.DWORD),
                ("ychars", W.DWORD), ("fill", W.DWORD), ("flags", W.DWORD), ("show", W.WORD),
                ("reserved_size", W.WORD), ("reserved_data", ctypes.c_void_p),
                ("input", W.HANDLE), ("output", W.HANDLE), ("error", W.HANDLE)]
        class StartupEx(ctypes.Structure):
            _fields_ = [("startup", Startup), ("attributes", ctypes.c_void_p)]
        class Process(ctypes.Structure):
            _fields_ = [("process", W.HANDLE), ("thread", W.HANDLE), ("pid", W.DWORD), ("tid", W.DWORD)]
        class Capabilities(ctypes.Structure):
            _fields_ = [("sid", ctypes.c_void_p), ("capabilities", ctypes.c_void_p), ("count", W.DWORD), ("reserved", W.DWORD)]
        sid = api.sid(config["sid"])
        allocated.append(sid)
        read_sid = api.sid(config["read_sid"])
        allocated.append(read_sid)
        capability_values = [SidAttributes(read_sid.value, 4)]
        # CLR, Python and native runtimes query machine runtime configuration.
        # Read-only registry capability does not grant host registry mutation.
        runtime_capabilities = ["registryRead"]
        if config.get("powershell_runtime") is True:
            runtime_capabilities += ["lpacInstrumentation", "lpacCom"]
        for name in runtime_capabilities:
            capability = api.sid(api.capability_sid(name))
            allocated.append(capability)
            capability_values.append(SidAttributes(capability.value, 4))
        if config.get("network") is True:
            internet = api.sid("S-1-15-3-1")
            allocated.append(internet)
            capability_values.append(SidAttributes(internet.value, 4))
        enabled = (SidAttributes * len(capability_values))(*capability_values)
        capabilities = Capabilities(sid.value, ctypes.addressof(enabled), len(capability_values), 0)
        user_data = api.token_information(token, 1)
        user = api.sid_text(ctypes.cast(user_data, ctypes.POINTER(SidAttributes)).contents.sid)
        logon = api.logon_sid()
        api.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [W.LPCWSTR, W.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        api.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = W.BOOL
        sddl = f"D:(A;;GA;;;{user})(A;;GA;;;{config['sid']})(A;;GA;;;{logon})"
        api.require(api.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None))
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.CreateDesktopW.argtypes = [W.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, W.DWORD, W.DWORD, ctypes.POINTER(Attributes)]
        user32.CreateDesktopW.restype = W.HANDLE
        user32.CloseDesktop.argtypes, user32.CloseDesktop.restype = [W.HANDLE], W.BOOL
        attributes = Attributes(ctypes.sizeof(Attributes), descriptor.value, False)
        desktop = user32.CreateDesktopW(config["desktop"], None, None, 0, 0x10000000, ctypes.byref(attributes))
        api.require(desktop)
        api.kernel.GetStdHandle.argtypes, api.kernel.GetStdHandle.restype = [W.DWORD], W.HANDLE
        api.kernel.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, ctypes.POINTER(Attributes), W.DWORD, W.DWORD, W.HANDLE]
        api.kernel.CreateFileW.restype = W.HANDLE
        null_attributes = Attributes(ctypes.sizeof(Attributes), None, True)
        null = api.kernel.CreateFileW("NUL", 0x80000000, 3, ctypes.byref(null_attributes), 3, 0, None)
        if null == ctypes.c_void_p(-1).value:
            raise OSError("Command stdin unavailable")
        startup = StartupEx()
        startup.startup.cb = ctypes.sizeof(startup)
        startup.startup.desktop = "winsta0\\" + config["desktop"]
        startup.startup.flags = 0x100
        startup.startup.input, startup.startup.output, startup.startup.error = null, api.kernel.GetStdHandle(-11), api.kernel.GetStdHandle(-12)
        handles = (W.HANDLE * 3)(null, startup.startup.output, startup.startup.error)
        length = ctypes.c_size_t()
        api.kernel.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, W.DWORD, W.DWORD, ctypes.POINTER(ctypes.c_size_t)]
        api.kernel.InitializeProcThreadAttributeList.restype = W.BOOL
        api.kernel.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, W.DWORD, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
        api.kernel.UpdateProcThreadAttribute.restype = W.BOOL
        api.kernel.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
        api.kernel.InitializeProcThreadAttributeList(None, 3, 0, ctypes.byref(length))
        attribute_data = ctypes.create_string_buffer(length.value)
        api.require(api.kernel.InitializeProcThreadAttributeList(attribute_data, 3, 0, ctypes.byref(length)))
        startup.attributes = ctypes.addressof(attribute_data)
        api.require(api.kernel.UpdateProcThreadAttribute(startup.attributes, 0, 0x20002, handles, ctypes.sizeof(handles), None, None))
        api.require(api.kernel.UpdateProcThreadAttribute(startup.attributes, 0, 0x20009, ctypes.byref(capabilities), ctypes.sizeof(capabilities), None, None))
        all_packages_policy = W.DWORD(1)  # PROCESS_CREATION_ALL_APPLICATION_PACKAGES_OPT_OUT
        api.require(api.kernel.UpdateProcThreadAttribute(startup.attributes, 0, 0x2000F,
            ctypes.byref(all_packages_policy), ctypes.sizeof(all_packages_policy), None, None))
        api.kernel.CreateProcessW.argtypes = [W.LPCWSTR, W.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, W.BOOL,
            W.DWORD, ctypes.c_void_p, W.LPCWSTR, ctypes.POINTER(StartupEx), ctypes.POINTER(Process)]
        api.kernel.CreateProcessW.restype = W.BOOL
        block = ctypes.create_unicode_buffer("\0".join(key + "=" + value for key, value in sorted(config["environment"].items())) + "\0\0")
        info = Process()
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline(config["argv"]))
        api.require(api.kernel.CreateProcessW(config["argv"][0], command, None, None, True,
            0x08000000 | 0x4 | 0x400 | 0x80000, block, config["root"], ctypes.byref(startup), ctypes.byref(info)))
        process, thread = info.process, info.thread
        child_token = W.HANDLE()
        api.require(api.advapi.OpenProcessToken(process, 8, ctypes.byref(child_token)))
        try:
            if not ctypes.cast(api.token_information(child_token, 29), ctypes.POINTER(W.DWORD)).contents.value:
                raise OSError("AppContainer token was not applied")
            # LPAC is requested through the documented creation attribute above;
            # unsupported attributes fail CreateProcess before target execution.
            # Class 46 is not a supported user-mode query on all Windows builds.
            package_data = api.token_information(child_token, 31)
            package = ctypes.cast(package_data, ctypes.POINTER(ctypes.c_void_p)).contents.value
            if api.sid_text(package) != config["sid"]:
                raise OSError("Unexpected command package identity")
        finally:
            api.kernel.CloseHandle(child_token)
        api.kernel.ResumeThread.argtypes, api.kernel.ResumeThread.restype = [W.HANDLE], W.DWORD
        LAUNCH_PHASE = "execution"
        api.require(api.kernel.ResumeThread(thread) == 1)
        api.kernel.WaitForSingleObject.argtypes, api.kernel.WaitForSingleObject.restype = [W.HANDLE, W.DWORD], W.DWORD
        api.kernel.GetExitCodeProcess.argtypes, api.kernel.GetExitCodeProcess.restype = [W.HANDLE, ctypes.POINTER(W.DWORD)], W.BOOL
        if api.kernel.WaitForSingleObject(process, 0xFFFFFFFF) != 0:
            raise OSError("Unable to wait for isolated command")
        code = W.DWORD()
        api.require(api.kernel.GetExitCodeProcess(process, ctypes.byref(code)))
        return code.value
    finally:
        for handle in (thread, process, token, null):
            if handle:
                api.kernel.CloseHandle(handle)
        if startup is not None and startup.attributes:
            api.kernel.DeleteProcThreadAttributeList(startup.attributes)
        if desktop:
            user32.CloseDesktop(desktop)
        for pointer in (descriptor, *allocated):
            if pointer.value:
                api.kernel.LocalFree(pointer)


if __name__ == "__main__":
    try:
        status = main()
    except BaseException as error:
        trace = error.__traceback__
        while trace is not None and trace.tb_next is not None:
            trace = trace.tb_next
        print("DIANSTAR_ISOLATION_ERROR:" + LAUNCH_NONCE + ":" + LAUNCH_PHASE + ":" + str(getattr(error, "winerror", None) or type(error).__name__)
            + ":" + str(trace.tb_lineno if trace else 0), flush=True)
        status = 125
    sys.exit(status)
