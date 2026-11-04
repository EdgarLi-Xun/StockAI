"""Per-Windows-user login autofill; secrets live only in Credential Manager."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import os


class _Credential(ctypes.Structure):
    _fields_=[('Flags',wintypes.DWORD),('Type',wintypes.DWORD),
              ('TargetName',wintypes.LPWSTR),('Comment',wintypes.LPWSTR),
              ('LastWritten',wintypes.FILETIME),('CredentialBlobSize',wintypes.DWORD),
              ('CredentialBlob',ctypes.POINTER(ctypes.c_ubyte)),('Persist',wintypes.DWORD),
              ('AttributeCount',wintypes.DWORD),('Attributes',ctypes.c_void_p),
              ('TargetAlias',wintypes.LPWSTR),('UserName',wintypes.LPWSTR)]


class LoginVault:
    def __init__(self,scope):
        if not scope:raise ValueError('缺少登录记录范围')
        self.target='StockAI/Login/'+hashlib.sha256(str(scope).encode()).hexdigest()

    def _api(self):
        if os.name!='nt':raise OSError('仅支持 Windows 凭据管理器')
        api=ctypes.WinDLL('advapi32',use_last_error=True)
        api.CredWriteW.argtypes=[ctypes.POINTER(_Credential),wintypes.DWORD]
        api.CredWriteW.restype=wintypes.BOOL
        api.CredReadW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.POINTER(ctypes.POINTER(_Credential))]
        api.CredReadW.restype=wintypes.BOOL
        api.CredDeleteW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD]
        api.CredDeleteW.restype=wintypes.BOOL
        api.CredFree.argtypes=[ctypes.c_void_p];api.CredFree.restype=None
        return api

    def save(self,email,password):
        email=str(email).strip().lower()
        if not email or len(email)>254 or '\0' in email or not isinstance(password,str) or not 12<=len(password)<=200:
            raise ValueError('账号或密码格式不正确')
        api=self._api();raw=password.encode('utf-16-le')
        blob=(ctypes.c_ubyte*len(raw)).from_buffer_copy(raw)
        value=_Credential(Type=1,TargetName=self.target,Comment='StockAI 登录自动填充',
                          CredentialBlobSize=len(raw),CredentialBlob=blob,Persist=2,UserName=email)
        if not api.CredWriteW(ctypes.byref(value),0):raise ctypes.WinError(ctypes.get_last_error())

    def read(self):
        api=self._api();pointer=ctypes.POINTER(_Credential)()
        if not api.CredReadW(self.target,1,0,ctypes.byref(pointer)):
            code=ctypes.get_last_error()
            if code==1168:return None
            raise ctypes.WinError(code)
        try:
            row=pointer.contents
            if row.CredentialBlobSize>2560:raise ValueError('登录记录格式不正确')
            password=ctypes.string_at(row.CredentialBlob,row.CredentialBlobSize).decode('utf-16-le')
            return {'email':row.UserName or '', 'password':password}
        finally:api.CredFree(pointer)

    def clear(self):
        api=self._api()
        if not api.CredDeleteW(self.target,1,0) and ctypes.get_last_error()!=1168:
            raise ctypes.WinError(ctypes.get_last_error())
