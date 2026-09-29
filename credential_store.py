"""Store QQ IMAP authorization codes in the current Windows user's Credential Manager."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import re


PREFIX = 'Codex.QQInvoiceCollector.imap.qq.com/'
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168


class FILETIME(ctypes.Structure):
    _fields_ = [('low', wintypes.DWORD), ('high', wintypes.DWORD)]


class CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ('Flags', wintypes.DWORD),
        ('Type', wintypes.DWORD),
        ('TargetName', wintypes.LPWSTR),
        ('Comment', wintypes.LPWSTR),
        ('LastWritten', FILETIME),
        ('CredentialBlobSize', wintypes.DWORD),
        ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)),
        ('Persist', wintypes.DWORD),
        ('AttributeCount', wintypes.DWORD),
        ('Attributes', ctypes.c_void_p),
        ('TargetAlias', wintypes.LPWSTR),
        ('UserName', wintypes.LPWSTR),
    ]


PCREDENTIALW = ctypes.POINTER(CREDENTIALW)


def _api():
    if not hasattr(ctypes, 'WinDLL'):
        raise OSError('保存授权码需要 Windows 凭据管理器')
    api = ctypes.WinDLL('Advapi32.dll', use_last_error=True)
    api.CredWriteW.argtypes = [PCREDENTIALW, wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(PCREDENTIALW)]
    api.CredReadW.restype = wintypes.BOOL
    api.CredEnumerateW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.POINTER(wintypes.DWORD),
                                   ctypes.POINTER(ctypes.POINTER(PCREDENTIALW))]
    api.CredEnumerateW.restype = wintypes.BOOL
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredDeleteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    return api


def _target(address: str) -> str:
    normalized = address.strip().lower()
    if not re.fullmatch(r'[^\s@]+@qq\.com', normalized):
        raise ValueError('请输入有效的 @qq.com 邮箱地址')
    return PREFIX + normalized


def _raise_last_error(action: str) -> None:
    raise OSError(ctypes.get_last_error(), f'Windows 凭据管理器{action}失败')


def save_credential(address: str, authorization_code: str) -> None:
    if not authorization_code:
        raise ValueError('授权码不能为空')
    encoded = authorization_code.encode('utf-8')
    if len(encoded) > 2560:
        raise ValueError('授权码过长')
    blob = (ctypes.c_ubyte * len(encoded)).from_buffer_copy(encoded)
    credential = CREDENTIALW()
    credential.Type = CRED_TYPE_GENERIC
    credential.TargetName = _target(address)
    credential.Comment = 'QQ 邮箱发票归集 IMAP 授权码'
    credential.CredentialBlobSize = len(encoded)
    credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
    credential.Persist = CRED_PERSIST_LOCAL_MACHINE
    credential.UserName = address.strip().lower()
    if not _api().CredWriteW(ctypes.byref(credential), 0):
        _raise_last_error('写入')


def load_credential(address: str) -> str | None:
    api = _api()
    credential = PCREDENTIALW()
    if not api.CredReadW(_target(address), CRED_TYPE_GENERIC, 0,
                         ctypes.byref(credential)):
        if ctypes.get_last_error() == ERROR_NOT_FOUND:
            return None
        _raise_last_error('读取')
    try:
        return ctypes.string_at(credential.contents.CredentialBlob,
                                credential.contents.CredentialBlobSize).decode('utf-8')
    finally:
        api.CredFree(ctypes.cast(credential, ctypes.c_void_p))


def list_accounts() -> list[str]:
    api = _api()
    count = wintypes.DWORD()
    credentials = ctypes.POINTER(PCREDENTIALW)()
    if not api.CredEnumerateW(PREFIX + '*', 0, ctypes.byref(count),
                              ctypes.byref(credentials)):
        if ctypes.get_last_error() == ERROR_NOT_FOUND:
            return []
        _raise_last_error('列出账户')
    try:
        return sorted({credentials[index].contents.TargetName[len(PREFIX):]
                       for index in range(count.value)})
    finally:
        api.CredFree(ctypes.cast(credentials, ctypes.c_void_p))


def delete_credential(address: str) -> bool:
    if _api().CredDeleteW(_target(address), CRED_TYPE_GENERIC, 0):
        return True
    if ctypes.get_last_error() == ERROR_NOT_FOUND:
        return False
    _raise_last_error('删除')
