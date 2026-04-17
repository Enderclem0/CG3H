"""
Resolve Hades2.pdb symbols via the DIA SDK (no registration needed).

Usage:
    python pdb_lookup.py <symbol> [<symbol> ...]
    python pdb_lookup.py --disasm <symbol> [--size N]
"""

import os
import sys
import ctypes
from ctypes import c_void_p, c_ulong, c_wchar_p, POINTER, byref, HRESULT
from comtypes import GUID

# Paths can be overridden via env vars so the tool works outside the default
# Steam / Visual Studio install layout.  Priority: env var > hardcoded default.
PDB_PATH = os.environ.get('HADES2_PDB_PATH',
    r'C:\Program Files (x86)\Steam\steamapps\common\Hades II\Ship\Hades2.pdb')
EXE_PATH = os.environ.get('HADES2_EXE_PATH',
    r'C:\Program Files (x86)\Steam\steamapps\common\Hades II\Ship\Hades2.exe')
# DIA SDK ships with Visual Studio.  Try Community → Professional → Enterprise
# under VS 2022 as fallback; allow env override for non-default installs or
# older/newer VS versions.
_DIA_CANDIDATES = [
    os.environ.get('DIA_SDK_DLL'),  # explicit override (may be None)
    r'C:\Program Files\Microsoft Visual Studio\2022\Community\DIA SDK\bin\amd64\msdia140.dll',
    r'C:\Program Files\Microsoft Visual Studio\2022\Professional\DIA SDK\bin\amd64\msdia140.dll',
    r'C:\Program Files\Microsoft Visual Studio\2022\Enterprise\DIA SDK\bin\amd64\msdia140.dll',
    r'C:\Program Files (x86)\Microsoft Visual Studio\2019\Community\DIA SDK\bin\amd64\msdia140.dll',
]
DIA_DLL = next((p for p in _DIA_CANDIDATES if p and os.path.isfile(p)),
               _DIA_CANDIDATES[1])  # default to VS2022 Community path if none exist

CLSID_DiaSource    = GUID('{E6756135-1E65-4D17-8576-610761398C3C}')
IID_IClassFactory  = GUID('{00000001-0000-0000-C000-000000000046}')
IID_IDiaDataSource = GUID('{79F1BB5F-B66E-48E5-B6A9-1545C323CA3D}')

# IDiaDataSource vtbl
DS_LOAD_PDB     = 4   # loadDataFromPdb(LPCOLESTR)
DS_OPEN_SESSION = 8   # openSession(IDiaSession**)
# IDiaSession vtbl
SESS_GET_GLOBAL = 5   # get_globalScope(IDiaSymbol**)
SESS_FIND_CHILD = 8   # findChildren(IDiaSymbol*, enum, name, flags, IDiaEnumSymbols**)
# IDiaEnumSymbols vtbl
ENUM_NEXT = 6
# IDiaSymbol vtbl
SYM_GET_RVA    = 13  # get_relativeVirtualAddress(DWORD*)
SYM_GET_LENGTH = 17  # get_length(ULONGLONG*)
SYM_GET_NAME   = 5   # get_name(BSTR*)

SymTagFunction    = 5
SymTagData        = 7
SymTagPublicSymbol = 10


def call(iface, idx, argtypes, restype, *args):
    vtbl = ctypes.cast(iface, POINTER(POINTER(c_void_p)))[0]
    fn = ctypes.cast(vtbl[idx], ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes))
    return fn(iface, *args)


def open_pdb():
    dll = ctypes.WinDLL(DIA_DLL)
    DllGetClassObject = dll.DllGetClassObject
    DllGetClassObject.restype = HRESULT

    cf = c_void_p()
    hr = DllGetClassObject(byref(CLSID_DiaSource), byref(IID_IClassFactory), byref(cf))
    if hr: raise OSError(f"DllGetClassObject hr=0x{hr:x}")

    ds = c_void_p()
    hr = call(cf, 3, [c_void_p, POINTER(GUID), POINTER(c_void_p)], HRESULT,
              None, byref(IID_IDiaDataSource), byref(ds))
    if hr: raise OSError(f"CreateInstance hr=0x{hr:x}")

    hr = call(ds, DS_LOAD_PDB, [c_wchar_p], HRESULT, PDB_PATH)
    if hr: raise OSError(f"loadDataFromPdb hr=0x{hr:x}")

    sess = c_void_p()
    hr = call(ds, DS_OPEN_SESSION, [POINTER(c_void_p)], HRESULT, byref(sess))
    if hr: raise OSError(f"openSession hr=0x{hr:x}")

    scope = c_void_p()
    hr = call(sess, SESS_GET_GLOBAL, [POINTER(c_void_p)], HRESULT, byref(scope))
    if hr: raise OSError(f"get_globalScope hr=0x{hr:x}")

    return sess, scope


def find_symbol(sess, scope, name, tag=SymTagFunction):
    enum = c_void_p()
    hr = call(sess, SESS_FIND_CHILD, [c_void_p, c_ulong, c_wchar_p, c_ulong, POINTER(c_void_p)],
              HRESULT, scope, tag, name, 0, byref(enum))
    if hr or not enum.value:
        return None
    sym = c_void_p()
    fetched = c_ulong(0)
    call(enum, ENUM_NEXT, [c_ulong, POINTER(c_void_p), POINTER(c_ulong)], HRESULT,
         1, byref(sym), byref(fetched))
    if fetched.value == 0:
        return None
    rva = c_ulong(0)
    call(sym, SYM_GET_RVA, [POINTER(c_ulong)], HRESULT, byref(rva))
    length = ctypes.c_ulonglong(0)
    call(sym, SYM_GET_LENGTH, [POINTER(ctypes.c_ulonglong)], HRESULT, byref(length))
    return rva.value, length.value


def lookup_any(sess, scope, name):
    for tag in (SymTagFunction, SymTagPublicSymbol, SymTagData):
        r = find_symbol(sess, scope, name, tag)
        if r: return r, tag
    return None, None


def regex_search(sess, scope, pattern, tag=SymTagFunction, limit=50):
    """Regex-search children by name. DIA nsfRegularExpression=8."""
    enum = c_void_p()
    hr = call(sess, SESS_FIND_CHILD, [c_void_p, c_ulong, c_wchar_p, c_ulong, POINTER(c_void_p)],
              HRESULT, scope, tag, pattern, 8, byref(enum))
    if hr or not enum.value:
        return []
    results = []
    while len(results) < limit:
        sym = c_void_p()
        fetched = c_ulong(0)
        call(enum, ENUM_NEXT, [c_ulong, POINTER(c_void_p), POINTER(c_ulong)], HRESULT,
             1, byref(sym), byref(fetched))
        if fetched.value == 0:
            break
        rva = c_ulong(0)
        call(sym, SYM_GET_RVA, [POINTER(c_ulong)], HRESULT, byref(rva))
        bstr = c_void_p()
        call(sym, SYM_GET_NAME, [POINTER(c_void_p)], HRESULT, byref(bstr))
        name_str = ctypes.wstring_at(bstr.value) if bstr.value else '?'
        if bstr.value:
            ctypes.windll.oleaut32.SysFreeString(bstr)
        results.append((rva.value, name_str))
    return results


def name_at_rva(sess, rva):
    """Return symbol name that covers this RVA, or None."""
    # Try findSymbolByRVA at several vtbl indices until one succeeds.
    for idx in (14, 15, 16, 17, 18):
        sym = c_void_p()
        try:
            hr = call(sess, idx, [c_ulong, c_ulong, POINTER(c_void_p)], HRESULT,
                      rva, 5, byref(sym))  # SymTagFunction=5
            if hr == 0 and sym.value:
                break
        except Exception:
            pass
    else:
        return None
    from ctypes import c_wchar_p as _wp
    bstr = c_void_p()
    # get_name returns BSTR
    try:
        hr = call(sym, SYM_GET_NAME, [POINTER(c_void_p)], HRESULT, byref(bstr))
    except Exception:
        return None
    if hr or not bstr.value:
        return None
    s = ctypes.wstring_at(bstr.value)
    # free BSTR
    ctypes.windll.oleaut32.SysFreeString(bstr)
    return s


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    args = sys.argv[1:]
    want_disasm = False
    size = 0x400
    names = []
    rvas = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == '--disasm':
            want_disasm = True
        elif a == '--size':
            size = int(args[i+1], 0)
            i += 1
        elif a == '--rva':
            rvas.append(int(args[i+1], 0))
            i += 1
        elif a == '--va':
            rvas.append(int(args[i+1], 0) - 0x140000000)
            i += 1
        else:
            names.append(a)
        i += 1

    sess, scope = open_pdb()
    for rva in rvas:
        n = name_at_rva(sess, rva)
        print(f"VA=0x{rva + 0x140000000:x} RVA=0x{rva:x} -> {n}")

    for name in names:
        if name.startswith('~') or name.startswith('@'):
            # regex search
            pattern = name[1:]
            print(f"Regex search: {pattern}")
            for tag, tagname in [(SymTagFunction, 'func'), (SymTagData, 'data'), (SymTagPublicSymbol, 'pub')]:
                results = regex_search(sess, scope, pattern, tag=tag, limit=25)
                for rva, n in results:
                    va = 0x140000000 + rva
                    print(f"  [{tagname}] VA=0x{va:x} {n}")
            continue
        result, tag = lookup_any(sess, scope, name)
        if not result:
            print(f"{name}: NOT FOUND")
            continue
        rva, length = result
        va = 0x140000000 + rva
        tagname = {SymTagFunction: 'func', SymTagPublicSymbol: 'pub', SymTagData: 'data'}.get(tag, '?')
        print(f"{name}: RVA=0x{rva:x} VA=0x{va:x} len=0x{length:x} ({tagname})")
        if want_disasm and length > 0:
            import pefile, capstone
            pe = pefile.PE(EXE_PATH, fast_load=True)
            pe.parse_data_directories()
            data = pe.get_memory_mapped_image()[rva:rva+min(length, size)]
            cs = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
            cs.detail = True
            for ins in cs.disasm(bytes(data), va):
                print(f"  0x{ins.address:x}: {ins.mnemonic:8s} {ins.op_str}")


if __name__ == '__main__':
    main()
