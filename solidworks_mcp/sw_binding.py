# Copyright 2026 JIALE LIU
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""How this server binds to SOLIDWORKS COM objects: late or early.

Late binding is pywin32's dynamic dispatch: every member is resolved by name
through ``GetIDsOfNames`` on the object's default interface.  It is what every
release this server was measured on works with (2016 SP3, 2026 SP3.2), and it
is the default.

SOLIDWORKS Design 2026 (3DEXPERIENCE R2026x) hands ``GetActiveObject`` a
dispatch without usable type information (#26).  Name lookups then fail
(``<unknown>.SketchManager``), the method flagging in ``sw_core`` cannot do its
work, and a sketch creator invoked without its arguments takes SOLIDWORKS down.
Early binding goes around the names entirely: makepy generates wrapper classes
from the installed ``sldworks.tlb`` (one per interface, members called by
dispid through ``InvokeTypes``), and :class:`EarlyBound` routes every member
access through the interface that declares it.

``SW_MCP_BINDING`` selects the mode.  ``late`` is the default and leaves every
release this server was measured on exactly as it was; ``early`` turns the
makepy path on, for R2026x.  Early binding falls back to late binding, with one
warning in the log, when the type library cannot be loaded, so a
misconfiguration never costs the connection.  The generated wrappers land in
pywin32's ``gen_py`` cache (``%TEMP%\\gen_py\\<python version>``); the first
early-bound attach generates them, which takes a few seconds, later attaches
import them.

The mode is explicit rather than auto-detected because the cheap signal for
"late binding will break", a dispatch without type information, does not hold:
``IDispatch.GetTypeInfo`` fails on the application dispatch of SOLIDWORKS 2016
SP3 as well (measured 2026-10-29), where late binding has always worked.  What
differs on R2026x is name resolution on the document's sub-objects, which
cannot be probed at attach time without risking the very crash this guards
against.  So the reporter of a broken release sets ``early`` rather than the
server guessing.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

import pythoncom
import win32com.client
import winerror
from win32com.client import DispatchBaseClass, gencache

logger = logging.getLogger("solidworks-mcp")

# sldworks.tlb.  Its major version is the major of the release's revision number
# (2016 = 24, 2026 = 34), which the running application reports late-bound.
TYPELIB_GUID = "{83A33D31-27C5-11CE-BFD4-00400513BB57}"
TYPELIB_LCID = 0

MODES = ("late", "early")
DEFAULT_MODE = "late"
ENV_VAR = "SW_MCP_BINDING"

_PyIDispatchType = pythoncom.TypeIIDs[pythoncom.IID_IDispatch]


def mode() -> str:
    """The configured binding mode; an unknown value counts as the default and is logged."""
    raw = (os.environ.get(ENV_VAR) or DEFAULT_MODE).strip().lower()
    if raw in MODES:
        return raw
    logger.warning("%s=%r is not one of %s; using %r.", ENV_VAR, raw, ", ".join(MODES), DEFAULT_MODE)
    return DEFAULT_MODE


def _members(cls: type) -> set[str]:
    """The member names a generated interface class declares: methods and properties."""
    names = {name for name, member in vars(cls).items() if not name.startswith("_") and callable(member)}
    names.update(cls._prop_map_get_)
    names.update(cls._prop_map_put_)
    return names


def _declares(cls: type, name: str, put: bool) -> bool:
    if put:
        return name in cls._prop_map_put_
    return name in cls._prop_map_get_ or callable(vars(cls).get(name))


class Library:
    """The generated wrapper module for one SOLIDWORKS release, indexed by member name."""

    def __init__(self, module: Any) -> None:
        self.module = module
        # The generated module re-exports DispatchBaseClass itself, which declares nothing.
        classes = [c for c in vars(module).values()
                   if isinstance(c, type) and issubclass(c, DispatchBaseClass) and hasattr(c, "_prop_map_get_")]
        # Largest interface first: when several interfaces an object implements declare the same name,
        # the object's own interface is the richer one (ISketchSegment over IEntity for a sketch line).
        classes.sort(key=lambda cls: len(_members(cls)), reverse=True)
        by_name: dict[str, list[type]] = {}
        for cls in classes:
            for name in _members(cls):
                by_name.setdefault(name, []).append(cls)
        self._by_name = {name: tuple(found) for name, found in by_name.items()}
        self._hints: dict[str, type] = {}
        self._typelib: Any = _UNLOADED
        self._signatures: dict[tuple[type, str], tuple[bool, ...] | None] = {}

    def variant_parameters(self, cls: type, name: str) -> tuple[bool, ...] | None:
        """For method ``name`` of interface ``cls``: per parameter, whether the type library declares it a
        VARIANT (by value or by reference).  None when the signature cannot be read."""
        key = (cls, name)
        if key not in self._signatures:
            self._signatures[key] = self._read_signature(cls, name)
        return self._signatures[key]

    def _read_signature(self, cls: type, name: str) -> tuple[bool, ...] | None:
        if self._typelib is _UNLOADED:
            try:
                module = self.module
                self._typelib = pythoncom.LoadRegTypeLib(module.CLSID, module.MajorVersion, module.MinorVersion,
                                                         module.LCID)
            except Exception as exc:
                _warn_once("signatures", "Early binding could not read parameter types from the SOLIDWORKS type "
                           "library (%s); null and VARIANT arguments are passed unchanged.", exc)
                self._typelib = None
        if self._typelib is None:
            return None
        try:
            info = self._typelib.GetTypeInfoOfGuid(cls.CLSID)
            for index in range(info.GetTypeAttr().cFuncs):
                desc = info.GetFuncDesc(index)
                if desc.invkind == pythoncom.INVOKE_FUNC and info.GetNames(desc.memid)[0] == name:
                    return tuple(_is_variant_type(arg[0]) for arg in desc.args)
        except pythoncom.com_error:
            return None
        return None

    def declaring(self, name: str, put: bool = False) -> tuple[type, ...]:
        found = self._by_name.get(name, ())
        if put:
            found = tuple(cls for cls in found if name in cls._prop_map_put_)
        hint = self._hints.get(name)
        if hint is not None and hint in found:
            found = (hint,) + tuple(cls for cls in found if cls is not hint)
        return found

    def remember(self, name: str, cls: type) -> None:
        self._hints[name] = cls

    def wrap(self, value: Any, homes: tuple[type, ...] = ()) -> Any:
        """Whatever a COM call returned, with every dispatch in it behind an :class:`EarlyBound`."""
        if isinstance(value, EarlyBound):
            return value
        if isinstance(value, DispatchBaseClass):
            return EarlyBound(value._oleobj_, self, homes + (type(value),))
        oleobj = getattr(value, "_oleobj_", None)   # pywin32's dynamic CDispatch
        if oleobj is not None:
            return EarlyBound(oleobj, self, homes)
        if isinstance(value, _PyIDispatchType):
            return EarlyBound(value, self, homes)
        if isinstance(value, (tuple, list)):
            return tuple(self.wrap(item, homes) for item in value)
        return value


def _unwrap(value: Any) -> Any:
    return value._oleobj_ if isinstance(value, EarlyBound) else value


def _is_byref(value: Any) -> bool:
    return isinstance(value, win32com.client.VARIANT) and bool(value.varianttype & pythoncom.VT_BYREF)


def _is_variant_type(typedesc: Any) -> bool:
    """Whether a type library TYPEDESC is VARIANT or a pointer to VARIANT (``(VT_PTR, VT_VARIANT)``)."""
    return typedesc == pythoncom.VT_VARIANT or typedesc == (pythoncom.VT_PTR, pythoncom.VT_VARIANT)


_UNLOADED = object()
_NothingType = type(pythoncom.Nothing)


def _plain(arg: Any, variant_declared: bool | None) -> Any:
    """One argument as a generated method takes it.

    A by-reference VARIANT goes in as its value (see :func:`_call_early`).  A parameter the type
    library declares VARIANT takes the caller's VARIANT objects as they are, so ``nothing()`` stays a
    typed null dispatch.  Every other declared type refuses a VARIANT object and ``pythoncom.Nothing``
    (``TypeError: The Python instance can not be converted to a COM object`` for an interface
    parameter such as SelectByID2's Callout, #26); there the VARIANT's value goes in, and Nothing
    becomes None, which marshals to a null interface pointer.  Measured offline against InvokeTypes
    2026-10-29.  With no signature (``None``) the argument is passed unchanged.
    """
    if _is_byref(arg):
        return arg.value
    if variant_declared is False:
        if isinstance(arg, win32com.client.VARIANT):
            return arg.value
        if isinstance(arg, _NothingType):
            return None
    return _unwrap(arg)


def _call_early(member: Callable[..., Any], args: tuple[Any, ...],
                variant_parameters: tuple[bool, ...] | None = None) -> Any:
    """Call a generated method with arguments made the way late binding takes them.

    Late-bound callers pass out-parameters as ``VARIANT(VT_BYREF | ...)`` (``byref_long``) and read
    ``.value`` afterwards.  A generated method declares those parameters itself, refuses a VARIANT
    object for them (``TypeError: int() argument ... not 'VARIANT'``, #27) and returns
    ``(result, out1, out2, ...)`` instead.  So each by-reference VARIANT goes in as its value, and
    the outputs are written back into it in order, which keeps the caller's contract.  Null and
    VARIANT arguments follow the declared parameter types, see :func:`_plain`.
    """
    byref = [arg for arg in args if _is_byref(arg)]
    declared = variant_parameters or ()
    plain = tuple(_plain(arg, declared[index] if index < len(declared) else None)
                  for index, arg in enumerate(args))
    result = member(*plain)
    if byref and isinstance(result, tuple) and len(result) == len(byref) + 1:
        for variant, out in zip(byref, result[1:]):
            variant.value = out
        return result[0]
    return result


class EarlyBound:
    """One SOLIDWORKS object, every member called by dispid through a generated interface class.

    The type library declares a typed interface for some members (IModelDoc2.SketchManager is an
    ISketchManager; those are the object's ``homes``) and a bare IDispatch for most others
    (ActiveDoc, FirstFeature, every array of bodies, faces or segments).  A member of a home is called
    through it.  Any other member is looked up in every interface of the library that declares it, and
    the object is asked (QueryInterface) which of those it implements; the first that answers is used
    and remembered, both on this proxy and as the library's first guess for that name.  An interface
    the object rejects is remembered too.  A name no implemented interface declares is an
    AttributeError, as it is late-bound, so ``safe`` and ``call_versioned`` keep their contract.
    """

    def __init__(self, oleobj: Any, library: Library, homes: tuple[type, ...] = ()) -> None:
        self.__dict__.update(_oleobj_=oleobj, _library=library, _homes=tuple(homes), _bound={}, _rejected=set())

    def __repr__(self) -> str:
        names = ", ".join(cls.__name__ for cls in self._homes) or "untyped"
        return f"<EarlyBound {names}>"

    def __bool__(self) -> bool:
        return True

    # Identity is the COM object's, as for pywin32's own wrappers: the feature tree walk compares objects.
    def __eq__(self, other: object) -> bool:
        return self._oleobj_ == getattr(other, "_oleobj_", other)

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    def __hash__(self) -> int:
        return id(self)

    def _bind(self, cls: type, trusted: bool) -> Any:
        """The generated class wrapped around this object, or None when the object rejects the interface."""
        if cls in self._bound:
            return self._bound[cls]
        if cls in self._rejected:
            return None
        pointer = self._oleobj_
        if not trusted:
            try:
                pointer = self._oleobj_.QueryInterface(cls.CLSID, pythoncom.IID_IDispatch)
            except pythoncom.com_error as exc:
                if exc.hresult != winerror.E_NOINTERFACE:
                    raise
                self._rejected.add(cls)
                return None
        bound = cls(pointer)
        self._bound[cls] = bound
        return bound

    def _interface_for(self, name: str, put: bool = False) -> Any:
        homes = [cls for cls in self._homes if _declares(cls, name, put)]
        others = [cls for cls in self._library.declaring(name, put) if cls not in self._homes]
        if not homes and not others:
            raise AttributeError(f"No interface of the SOLIDWORKS type library declares '{name}'.")
        for cls in homes:
            bound = self._bind(cls, trusted=True)
            if bound is not None:
                return bound
        for cls in others:
            bound = self._bind(cls, trusted=False)
            if bound is not None:
                self._library.remember(name, cls)
                return bound
        names = ", ".join(cls.__name__ for cls in homes + others)
        raise AttributeError(f"This object implements none of the interfaces that declare '{name}': {names}.")

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        bound = self._interface_for(name)
        member = getattr(bound, name)
        if name in type(bound)._prop_map_get_:
            return self._library.wrap(member)
        return self._method(member, type(bound), name)

    def _method(self, member: Callable[..., Any], cls: type, name: str) -> Callable[..., Any]:
        library = self._library

        def call(*args: Any) -> Any:
            # The signature is read only when an argument depends on it: a non-by-reference VARIANT or Nothing.
            needs = any(isinstance(arg, (win32com.client.VARIANT, _NothingType)) and not _is_byref(arg)
                        for arg in args)
            declared = library.variant_parameters(cls, name) if needs else None
            return library.wrap(_call_early(member, args, declared))

        call.__name__ = getattr(member, "__name__", "call")
        return call

    def __setattr__(self, name: str, value: Any) -> None:
        bound = self._interface_for(name, put=True)
        setattr(bound, name, _unwrap(value))


# --------------------------------------------------------------------------
# Attaching
# --------------------------------------------------------------------------

_libraries: dict[int, Library] = {}
_active: Library | None = None
_warned: set[str] = set()


def _warn_once(key: str, message: str, *args: Any) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning(message, *args)


def _revision(raw: Any) -> str:
    member = raw.RevisionNumber
    if callable(member) and not hasattr(member, "_oleobj_"):
        member = member()
    return str(member)


def _load_library(raw: Any) -> Library | None:
    """The wrappers for the running release, generated on first use; None, with a warning, when that fails."""
    try:
        revision = _revision(raw)
        major = int(revision.split(".")[0])
    except Exception as exc:
        _warn_once("revision", "Early binding needs the SOLIDWORKS revision number and could not read it (%s); "
                   "binding late.", exc)
        return None
    library = _libraries.get(major)
    if library is not None:
        return library
    try:
        module = gencache.EnsureModule(TYPELIB_GUID, TYPELIB_LCID, major, 0)
    except Exception as exc:
        _warn_once("typelib", "Early binding needs the SOLIDWORKS type library (sldworks.tlb version %d.0, revision "
                   "%s), which could not be loaded or generated (%s); binding late.", major, revision, exc)
        return None
    if module is None or not hasattr(module, "ISldWorks"):
        _warn_once("typelib", "Early binding needs the SOLIDWORKS type library (sldworks.tlb version %d.0, revision "
                   "%s), which is not registered on this machine; binding late.", major, revision)
        return None
    library = Library(module)
    _libraries[major] = library
    logger.info("Early binding to SOLIDWORKS %s through %s.", revision, module.__name__)
    return library


def bind_application(raw: Any) -> Any:
    """``raw`` as ``win32com.client.GetActiveObject`` returned it, bound the configured way.

    Late binding returns a pywin32 dynamic dispatch.  ``GetActiveObject`` hands back the generated
    coclass ``SldWorks`` instead as soon as any process of the same user has generated wrappers for
    sldworks.tlb into the shared ``gen_py`` cache (makepy, ``EnsureDispatch``, or this module's
    early mode), and the late-bound tools break on it (#27).  Anything that is not a dynamic
    dispatch is therefore wrapped dynamically again; results of a dynamic dispatch stay dynamic, so
    the whole session is late-bound.  Early binding returns the application behind an
    :class:`EarlyBound` whose home is ISldWorks, and keeps the library for :func:`bind`.
    """
    global _active
    _active = None
    if mode() != "early":
        if isinstance(raw, win32com.client.dynamic.CDispatch):
            return raw
        return win32com.client.dynamic.Dispatch(getattr(raw, "_oleobj_", raw), "SldWorks.Application")
    library = _load_library(raw)
    if library is None:
        return raw
    _active = library
    return EarlyBound(raw._oleobj_, library, (library.module.ISldWorks,))


def bind(obj: Any) -> Any:
    """A bare dispatch pointer made usable the way the application is bound: early while it is, else late."""
    if _active is not None:
        return _active.wrap(obj)
    return win32com.client.Dispatch(obj)
