"""Narrow Windows ACL grants for one synthetic, non-account command SID."""
import ctypes
from ctypes import wintypes as W


class SidAttributes(ctypes.Structure):
    _fields_ = [("sid", ctypes.c_void_p), ("attributes", W.DWORD)]


class Trustee(ctypes.Structure):
    _fields_ = [("multiple", ctypes.c_void_p), ("operation", W.DWORD), ("form", W.DWORD),
                ("kind", W.DWORD), ("name", ctypes.c_void_p)]


class ExplicitAccess(ctypes.Structure):
    _fields_ = [("permissions", W.DWORD), ("mode", W.DWORD), ("inheritance", W.DWORD), ("trustee", Trustee)]


class WindowsSecurity:
    def __init__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        for dll, name, args, result in (
            (self.kernel, "LocalFree", [ctypes.c_void_p], ctypes.c_void_p),
            (self.kernel, "CloseHandle", [W.HANDLE], W.BOOL),
            (self.kernel, "GetCurrentProcess", [], W.HANDLE),
            (self.advapi, "ConvertStringSidToSidW", [W.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)], W.BOOL),
            (self.advapi, "ConvertSidToStringSidW", [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], W.BOOL),
            (self.advapi, "GetNamedSecurityInfoW", [W.LPWSTR, W.DWORD, W.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], W.DWORD),
            (self.advapi, "SetNamedSecurityInfoW", [W.LPWSTR, W.DWORD, W.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                ctypes.c_void_p, ctypes.c_void_p], W.DWORD),
            (self.advapi, "SetEntriesInAclW", [W.ULONG, ctypes.POINTER(ExplicitAccess), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], W.DWORD),
            (self.advapi, "OpenProcessToken", [W.HANDLE, W.DWORD, ctypes.POINTER(W.HANDLE)], W.BOOL),
            (self.advapi, "GetTokenInformation", [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD, ctypes.POINTER(W.DWORD)], W.BOOL),
        ):
            function = getattr(dll, name)
            function.argtypes, function.restype = args, result

    @staticmethod
    def require(ok):
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    def sid(self, text):
        pointer = ctypes.c_void_p()
        self.require(self.advapi.ConvertStringSidToSidW(text, ctypes.byref(pointer)))
        return pointer

    def sid_text(self, pointer):
        result = ctypes.c_void_p()
        self.require(self.advapi.ConvertSidToStringSidW(pointer, ctypes.byref(result)))
        try:
            return ctypes.wstring_at(result)
        finally:
            self.kernel.LocalFree(result)

    def token_information(self, token, kind):
        size = W.DWORD()
        self.advapi.GetTokenInformation(token, kind, None, 0, ctypes.byref(size))
        if not size.value:
            raise OSError("Token information unavailable")
        data = ctypes.create_string_buffer(size.value)
        self.require(self.advapi.GetTokenInformation(token, kind, data, size, ctypes.byref(size)))
        return data

    def token_sids(self, token, kind=2):
        data = self.token_information(token, kind)
        class Groups(ctypes.Structure):
            _fields_ = [("count", W.DWORD), ("entries", SidAttributes * 1)]
        count = ctypes.cast(data, ctypes.POINTER(W.DWORD)).contents.value
        array = (SidAttributes * count).from_buffer(data, Groups.entries.offset)
        return [(self.sid_text(item.sid), item.attributes) for item in array]

    def edit_acl(self, path, sid_text, *, mode=1, permissions=0x1301BF, inherit=True):
        """Change only this SID's ACEs; never replace another user's ACL."""
        sid, descriptor, old, new = self.sid(sid_text), ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        try:
            code = self.advapi.GetNamedSecurityInfoW(str(path), 1, 4, None, None, ctypes.byref(old), None, ctypes.byref(descriptor))
            if code:
                raise ctypes.WinError(code)
            if not old.value:
                raise OSError("Null DACL is not supported")
            entry = ExplicitAccess(permissions, mode, 3 if inherit else 0, Trustee(None, 0, 0, 0, sid.value))
            code = self.advapi.SetEntriesInAclW(1, ctypes.byref(entry), old, ctypes.byref(new))
            if code:
                raise ctypes.WinError(code)
            code = self.advapi.SetNamedSecurityInfoW(str(path), 1, 4, None, None, new, None)
            if code:
                raise ctypes.WinError(code)
        finally:
            for pointer in (sid, descriptor, new):
                if pointer.value:
                    self.kernel.LocalFree(pointer)

    def acl_entries(self, path):
        descriptor, acl = ctypes.c_void_p(), ctypes.c_void_p()
        try:
            code = self.advapi.GetNamedSecurityInfoW(str(path), 1, 4, None, None, ctypes.byref(acl), None, ctypes.byref(descriptor))
            if code or not acl.value:
                raise OSError("ACL unavailable")
            count = ctypes.c_ushort.from_address(acl.value + 4).value
            entries, offset = [], 8
            for _ in range(count):
                size = ctypes.c_ushort.from_address(acl.value + offset + 2).value
                if size < 4:
                    raise OSError("Invalid ACL entry")
                entries.append(ctypes.string_at(acl.value + offset, size))
                offset += size
            return entries
        finally:
            if descriptor.value:
                self.kernel.LocalFree(descriptor)

    def explicit_sid_entry(self, entry, sid):
        if entry[0] not in {0, 1} or entry[1] & 0x10 or len(entry) < 16:
            return False
        data = ctypes.create_string_buffer(entry)
        return self.sid_text(ctypes.addressof(data) + 8) == sid

    def capture_sid(self, path, sid):
        return [entry.hex() for entry in self.acl_entries(path) if self.explicit_sid_entry(entry, sid)]

    def restore_sid(self, path, sid, before):
        entries = [entry for entry in self.acl_entries(path) if not self.explicit_sid_entry(entry, sid)]
        entries += [bytes.fromhex(item) for item in before]
        entries.sort(key=lambda entry: (bool(entry[1] & 0x10), entry[0] != 1))
        self.set_acl(path, entries)

    def set_acl(self, path, entries, *, protected=None):
        data = ctypes.create_string_buffer(8 + sum(map(len, entries)))
        self.advapi.InitializeAcl.argtypes = [ctypes.c_void_p, W.DWORD, W.DWORD]
        self.advapi.InitializeAcl.restype = W.BOOL
        self.advapi.AddAce.argtypes = [ctypes.c_void_p, W.DWORD, W.DWORD, ctypes.c_void_p, W.DWORD]
        self.advapi.AddAce.restype = W.BOOL
        self.require(self.advapi.InitializeAcl(data, ctypes.sizeof(data), 4))
        for entry in entries:
            raw = ctypes.create_string_buffer(entry)
            self.require(self.advapi.AddAce(data, 4, 0xFFFFFFFF, raw, len(entry)))
        information = 4 | (0x80000000 if protected is True else 0x20000000 if protected is False else 0)
        code = self.advapi.SetNamedSecurityInfoW(str(path), 1, information, None, None, data, None)
        if code:
            raise ctypes.WinError(code)

    def acl_snapshot(self, path):
        descriptor, acl = ctypes.c_void_p(), ctypes.c_void_p()
        try:
            code = self.advapi.GetNamedSecurityInfoW(str(path), 1, 4, None, None, ctypes.byref(acl), None, ctypes.byref(descriptor))
            if code:
                raise ctypes.WinError(code)
            self.advapi.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, ctypes.POINTER(W.WORD), ctypes.POINTER(W.DWORD)]
            self.advapi.GetSecurityDescriptorControl.restype = W.BOOL
            control, revision = W.WORD(), W.DWORD()
            self.require(self.advapi.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)))
            return {"entries": [entry.hex() for entry in self.acl_entries(path)], "protected": bool(control.value & 0x1000)}
        finally:
            if descriptor.value:
                self.kernel.LocalFree(descriptor)

    def allow_ace(self, sid, mask, flags=0):
        pointer = self.sid(sid)
        try:
            size = 8 + 4 * ctypes.c_ubyte.from_address(pointer.value + 1).value
            raw = ctypes.string_at(pointer.value, size)
            return bytes([0, flags]) + (8 + size).to_bytes(2, "little") + mask.to_bytes(4, "little") + raw
        finally:
            self.kernel.LocalFree(pointer)

    def private_acl(self, before, *, git_sid=None):
        entries = []
        for item in before["entries"]:
            raw = bytes.fromhex(item)
            if raw[0] not in {0, 1}:
                raise OSError("Unsupported conditional/object ACL on protected path")
            data = ctypes.create_string_buffer(raw)
            sid = self.sid_text(ctypes.addressof(data) + 8)
            if sid.startswith("S-1-15-"):
                continue
            # Preserve normal users' effective rights while suspending new
            # parent package grants. Original inheritance is journaled.
            entries.append(bytes([raw[0], raw[1] & ~0x10]) + raw[2:])
        if git_sid:
            entries.append(self.allow_ace(git_sid, 0x1200A9, 3))
        entries.sort(key=lambda entry: entry[0] != 1)
        return {"entries": [entry.hex() for entry in entries], "protected": True}

    def logon_sid(self):
        token = W.HANDLE()
        self.require(self.advapi.OpenProcessToken(self.kernel.GetCurrentProcess(), 8, ctypes.byref(token)))
        try:
            identifiers = [sid for sid, attributes in self.token_sids(token) if attributes & 0xC0000000 == 0xC0000000]
            if len(identifiers) != 1:
                raise OSError("Logon identity unavailable")
            return identifiers[0]
        finally:
            self.kernel.CloseHandle(token)

    def is_elevated(self):
        token = W.HANDLE()
        self.require(self.advapi.OpenProcessToken(self.kernel.GetCurrentProcess(), 8, ctypes.byref(token)))
        try:
            return bool(ctypes.cast(self.token_information(token, 20), ctypes.POINTER(W.DWORD)).contents.value)
        finally:
            self.kernel.CloseHandle(token)

    def appcontainer_sid(self, name):
        userenv = ctypes.WinDLL("userenv", use_last_error=True)
        userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [W.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
        userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
        pointer = ctypes.c_void_p()
        if userenv.DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(pointer)) != 0:
            raise OSError("AppContainer identity unavailable")
        self.advapi.FreeSid.argtypes, self.advapi.FreeSid.restype = [ctypes.c_void_p], ctypes.c_void_p
        try:
            return self.sid_text(pointer)
        finally:
            self.advapi.FreeSid(pointer)

    def capability_sid(self, name):
        library = ctypes.WinDLL("kernelbase", use_last_error=True)
        function = library.DeriveCapabilitySidsFromName
        function.argtypes = [W.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(W.DWORD),
                             ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(W.DWORD)]
        function.restype = W.BOOL
        groups, capabilities, group_count, count = ctypes.c_void_p(), ctypes.c_void_p(), W.DWORD(), W.DWORD()
        self.require(function(name, ctypes.byref(groups), ctypes.byref(group_count), ctypes.byref(capabilities), ctypes.byref(count)))
        try:
            if count.value != 1 or group_count.value > 16:
                raise OSError("Unexpected capability identity")
            return self.sid_text(ctypes.cast(capabilities, ctypes.POINTER(ctypes.c_void_p))[0])
        finally:
            for array, length in ((groups, group_count.value), (capabilities, count.value)):
                if array.value:
                    for index in range(length):
                        self.kernel.LocalFree(ctypes.cast(array, ctypes.POINTER(ctypes.c_void_p))[index])
                    self.kernel.LocalFree(array)

    def create_profile(self, name):
        userenv = ctypes.WinDLL("userenv", use_last_error=True)
        userenv.CreateAppContainerProfile.argtypes = [W.LPCWSTR, W.LPCWSTR, W.LPCWSTR, ctypes.c_void_p, W.DWORD, ctypes.POINTER(ctypes.c_void_p)]
        userenv.CreateAppContainerProfile.restype = ctypes.c_long
        pointer = ctypes.c_void_p()
        result = userenv.CreateAppContainerProfile(name, "DianStar command", "Isolated development command", None, 0, ctypes.byref(pointer))
        if result not in {0, -2147024713}:
            raise OSError("Unable to create command isolation profile: " + hex(result & 0xFFFFFFFF))
        if pointer.value:
            self.advapi.FreeSid.argtypes, self.advapi.FreeSid.restype = [ctypes.c_void_p], ctypes.c_void_p
            self.advapi.FreeSid(pointer)

    def can_edit(self, path):
        self.kernel.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, ctypes.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
        self.kernel.CreateFileW.restype = W.HANDLE
        handle = self.kernel.CreateFileW(str(path), 0x40000, 7, None, 3, 0x02000000 | 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            return False
        self.kernel.CloseHandle(handle)
        return True

    def delete_profile(self, name):
        userenv = ctypes.WinDLL("userenv", use_last_error=True)
        userenv.DeleteAppContainerProfile.argtypes, userenv.DeleteAppContainerProfile.restype = [W.LPCWSTR], ctypes.c_long
        result = userenv.DeleteAppContainerProfile(name)
        if result not in {0, -2147024894}:
            raise OSError("Unable to remove command isolation profile")
