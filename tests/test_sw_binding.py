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

"""Offline tests for sw_binding: the early-bound proxy over faked makepy classes, and the mode selection.

The generated classes are faked with pywin32's real DispatchBaseClass, so property reads and writes take the
same path as the generated code (``_prop_map_get_`` / ``_prop_map_put_`` through the object's ``InvokeTypes``
and ``Invoke``); only the COM pointer behind them is a fake that records what it was asked.

    ..\\.venv\\Scripts\\python.exe -m unittest discover -s tests
"""

from __future__ import annotations

import os
import sys
import types
import unittest
import unittest.mock
from pathlib import Path

import pythoncom
import winerror
import win32com.client
from win32com.client import CoClassBaseClass, DispatchBaseClass

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solidworks_mcp import sw_binding, sw_core
from solidworks_mcp.sw_binding import EarlyBound, Library


def no_interface() -> pythoncom.com_error:
    return pythoncom.com_error(winerror.E_NOINTERFACE, "No such interface supported", None, None)


class FakeOle:
    """Stands in for the PyIDispatch pointer: answers QueryInterface for the interfaces its object implements and
    records every QueryInterface, InvokeTypes and Invoke it receives.  `answers` maps a dispid to the raw result."""

    def __init__(self, name: str, implements=(), answers=None, type_info=True) -> None:
        self.name = name
        self.implements = set(implements)
        self.answers = dict(answers or {})
        self.type_info = type_info
        self.queried: list[str] = []
        self.calls: list[tuple] = []

    def QueryInterface(self, iid, iid_dispatch):  # noqa: N802 - pywin32 member name
        self.queried.append(iid)
        if iid in self.implements:
            return self
        raise no_interface()

    def InvokeTypes(self, dispid, lcid, flags, ret_type, arg_types, *args):  # noqa: N802
        self.calls.append((dispid, flags, args))
        return self.answers.get(dispid)

    def Invoke(self, dispid, lcid, flags, retval, *args):  # noqa: N802
        self.calls.append((dispid, flags, args))
        return None

    def GetTypeInfo(self):  # noqa: N802
        if self.type_info:
            return object()
        raise pythoncom.com_error(winerror.E_NOTIMPL, "Not implemented", None, None)


class Dynamic:
    """What pywin32 makes of an untyped IDispatch result: a wrapper whose only useful part is `_oleobj_`."""

    def __init__(self, ole: FakeOle) -> None:
        self._oleobj_ = ole


def interface(name: str, clsid: str, methods=(), props_get=(), props_put=()) -> type:
    """A class shaped like a makepy interface: methods call InvokeTypes with their dispid, properties sit in the maps."""
    namespace = {
        "CLSID": clsid,
        "coclass_clsid": None,
        "_prop_map_get_": {prop: (dispid, 2, (9, 0), (), prop, None) for prop, dispid in props_get},
        "_prop_map_put_": {prop: ((dispid, 0, 4, 0), ()) for prop, dispid in props_put},
    }
    for method, dispid in methods:
        def call(self, *args, _dispid=dispid):
            return self._oleobj_.InvokeTypes(_dispid, 0, 1, (12, 0), (), *args)
        namespace[method] = call
    return type(name, (DispatchBaseClass,), namespace)


# Member counts decide the search order: IModelDoc2 is the largest, IEntity the smallest.
ISldWorks = interface("ISldWorks", "{APP}", methods=[("ActivateDoc3", 11), ("RevisionNumber", 12)],
                      props_get=[("ActiveDoc", 1)], props_put=[("Visible", 27)])
IModelDoc2 = interface("IModelDoc2", "{DOC}", methods=[("GetType", 100), ("GetTitle", 101), ("FirstFeature", 102),
                                                       ("InsertSketch2", 103), ("EditRebuild3", 104)],
                       props_get=[("SketchManager", 110), ("FeatureManager", 111)])
IPartDoc = interface("IPartDoc", "{PART}", methods=[("GetBodies2", 200), ("GetPartBox", 201)])
IAssemblyDoc = interface("IAssemblyDoc", "{ASM}", methods=[("GetBox", 300)])
ISketchManager = interface("ISketchManager", "{SKM}", methods=[("CreateLine", 400), ("InsertSketch", 401)],
                           props_get=[("ActiveSketch", 410)], props_put=[("AddToDB", 411)])
ISketchSegment = interface("ISketchSegment", "{SEG}", methods=[("GetType", 500), ("GetID", 501), ("Select4", 502)],
                           props_get=[("ConstructionGeometry", 510)], props_put=[("ConstructionGeometry", 510)])
IEntity = interface("IEntity", "{ENT}", methods=[("GetType", 600), ("Select4", 601)])
IFeature = interface("IFeature", "{FEAT}", methods=[("GetSpecificFeature2", 700), ("GetNextFeature", 701)],
                     props_get=[("Name", 710)], props_put=[("Name", 710)])

class SldWorksCoClass(CoClassBaseClass):
    """The generated coclass GetActiveObject returns once gen_py holds sldworks.tlb wrappers."""

    CLSID = "{COCLASS}"
    default_interface = ISldWorks


# Like a generated module, this one also carries DispatchBaseClass itself (makepy imports it at module level),
# which declares no members and must not be indexed.
MODULE = types.SimpleNamespace(
    __name__="fake_sldworks", ISldWorks=ISldWorks, IModelDoc2=IModelDoc2, IPartDoc=IPartDoc,
    IAssemblyDoc=IAssemblyDoc, ISketchManager=ISketchManager, ISketchSegment=ISketchSegment, IEntity=IEntity,
    IFeature=IFeature, DispatchBaseClass=DispatchBaseClass, not_a_class=42,
)


class ProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.library = Library(MODULE)
        self.skm_ole = FakeOle("sketch manager", answers={400: Dynamic(FakeOle("line", {"{SEG}", "{ENT}"}))})
        self.doc_ole = FakeOle("part", {"{DOC}", "{PART}"},
                               answers={100: 1, 101: "Part1", 110: ISketchManager(self.skm_ole)})
        self.app_ole = FakeOle("app", {"{APP}"}, answers={1: Dynamic(self.doc_ole)})
        self.app = EarlyBound(self.app_ole, self.library, (ISldWorks,))

    def test_a_home_member_is_called_by_dispid_without_a_query(self) -> None:
        self.app.ActivateDoc3("Part1", True, 0, None)
        self.assertEqual(self.app_ole.calls, [(11, 1, ("Part1", True, 0, None))])
        self.assertEqual(self.app_ole.queried, [], "the declared interface is trusted, not queried")

    def test_a_typed_result_is_used_through_its_declared_interface(self) -> None:
        doc = self.app.ActiveDoc
        manager = doc.SketchManager
        self.assertIsInstance(manager, EarlyBound)
        self.assertEqual(repr(manager), "<EarlyBound ISketchManager>")
        line = manager.CreateLine(0.0, 0.0, 0.0, 0.05, 0.0, 0.0)
        self.assertEqual(self.skm_ole.calls, [(400, 1, (0.0, 0.0, 0.0, 0.05, 0.0, 0.0))])
        self.assertIsInstance(line, EarlyBound)

    def test_an_untyped_result_finds_its_interfaces_by_query(self) -> None:
        doc = self.app.ActiveDoc
        self.assertIsInstance(doc, EarlyBound)
        self.assertEqual(repr(doc), "<EarlyBound untyped>")
        self.assertEqual(doc.GetType(), 1)
        self.assertEqual(doc.GetTitle(), "Part1")
        doc.GetBodies2(0, True)
        self.assertEqual(self.doc_ole.calls, [(100, 1, ()), (101, 1, ()), (200, 1, (0, True))])
        # IModelDoc2 answered once and is reused; IPartDoc was queried for the part-only member.
        self.assertEqual(self.doc_ole.queried, ["{DOC}", "{PART}"])

    def test_a_member_of_an_interface_the_object_rejects_is_an_attribute_error(self) -> None:
        doc = self.app.ActiveDoc
        with self.assertRaisesRegex(AttributeError, "IAssemblyDoc"):
            doc.GetBox()
        with self.assertRaises(AttributeError):
            doc.GetBox()
        self.assertEqual(self.doc_ole.queried.count("{ASM}"), 1, "a rejected interface is not queried again")
        self.assertEqual(sw_core.safe(doc, "GetBox", "fallback"), "fallback")

    def test_a_name_no_interface_declares_is_an_attribute_error(self) -> None:
        with self.assertRaisesRegex(AttributeError, "No interface"):
            self.app.NoSuchMember()
        with self.assertRaises(AttributeError):
            self.app._FlagAsMethod("ActivateDoc3")

    def test_the_largest_implemented_interface_wins_a_shared_name(self) -> None:
        line = self.app.ActiveDoc.SketchManager.CreateLine(0, 0, 0, 1, 0, 0)
        line_ole = line._oleobj_
        line.GetType()
        self.assertEqual(line_ole.calls, [(500, 1, ())], "ISketchSegment.GetType, not IEntity.GetType")
        self.assertEqual(line_ole.queried[0], "{DOC}", "larger interfaces are asked first")
        self.assertIn("{SEG}", line_ole.queried)
        self.assertNotIn("{ENT}", line_ole.queried)

    def test_an_answered_interface_is_the_first_guess_for_that_name(self) -> None:
        manager = self.app.ActiveDoc.SketchManager
        manager.CreateLine(0, 0, 0, 1, 0, 0).GetType()
        self.skm_ole.answers[400] = Dynamic(FakeOle("second line", {"{SEG}", "{ENT}"}))
        second = manager.CreateLine(0, 0, 0, 2, 0, 0)
        second.GetType()
        self.assertEqual(second._oleobj_.queried, ["{SEG}"], "the hint spares the queries that failed last time")
        self.assertEqual(second._oleobj_.calls, [(500, 1, ())])

    def test_a_property_write_goes_through_the_put_map(self) -> None:
        feature = self.library.wrap(Dynamic(FakeOle("feature", {"{FEAT}"})))
        feature.Name = "Base"
        self.assertEqual(feature._oleobj_.calls, [(710, 4, ("Base",))])
        manager = self.app.ActiveDoc.SketchManager
        manager.AddToDB = True
        self.assertEqual(self.skm_ole.calls[-1], (411, 4, (True,)))
        with self.assertRaises(AttributeError):
            self.app.ActiveDoc.NoSuchProperty = 1

    def test_proxies_passed_as_arguments_are_unwrapped(self) -> None:
        doc = self.app.ActiveDoc
        self.app.ActivateDoc3(doc, True, 0, None)
        self.assertEqual(self.app_ole.calls[-1][0], 11)
        self.assertIs(self.app_ole.calls[-1][2][0], self.doc_ole)

    def test_by_reference_variants_go_in_as_values_and_come_back_filled(self) -> None:
        """#27: a generated method refuses VARIANT(VT_BYREF|VT_I4) and returns (result, out...) instead."""
        self.app_ole.answers[11] = (Dynamic(self.doc_ole), 7)
        errors = sw_core.byref_long(0)
        activated = self.app.ActivateDoc3("Part1", True, 0, errors)
        self.assertIsInstance(activated, EarlyBound)
        self.assertEqual(activated, Dynamic(self.doc_ole)._oleobj_)
        self.assertEqual(errors.value, 7)
        self.assertEqual(self.app_ole.calls[-1], (11, 1, ("Part1", True, 0, 0)), "the value, not the VARIANT")

    def test_two_outputs_are_written_back_in_order(self) -> None:
        self.app_ole.answers[11] = (True, 3, 5)
        errors, warnings = sw_core.byref_long(0), sw_core.byref_long(0)
        self.assertIs(self.app.ActivateDoc3("x", errors, 0, warnings), True)
        self.assertEqual((errors.value, warnings.value), (3, 5))

    def test_a_variant_that_is_not_by_reference_passes_unchanged(self) -> None:
        values = sw_core.double_array([1.0, 2.0])
        self.app.ActivateDoc3(values, True, 0, None)
        self.assertIs(self.app_ole.calls[-1][2][0], values)

    def test_an_array_result_without_out_parameters_is_left_whole(self) -> None:
        self.app_ole.answers[11] = (1.0, 2.0)
        self.assertEqual(self.app.ActivateDoc3("x", True, 0, None), (1.0, 2.0))

    def test_arrays_and_nested_arrays_are_wrapped(self) -> None:
        faces = (Dynamic(FakeOle("f1")), (Dynamic(FakeOle("f2")), None), 3.5)
        wrapped = self.library.wrap(faces)
        self.assertIsInstance(wrapped[0], EarlyBound)
        self.assertIsInstance(wrapped[1][0], EarlyBound)
        self.assertIsNone(wrapped[1][1])
        self.assertEqual(wrapped[2], 3.5)
        self.assertEqual(sw_core.as_list(wrapped), [wrapped[0], wrapped[1], 3.5])

    def test_value_and_flag_methods_keep_their_contract(self) -> None:
        doc = self.app.ActiveDoc
        self.assertEqual(sw_core.value(doc, "GetTitle"), "Part1")
        manager = sw_core.value(doc, "SketchManager")
        self.assertIsInstance(manager, EarlyBound, "a dispatch-valued property is handed back, not called")
        with self.assertNoLogs("solidworks-mcp", level="WARNING"):
            self.assertIs(sw_core.flag_methods(doc, "GetBox", "NoSuchMember"), doc)
        self.assertEqual(self.doc_ole.queried, ["{DOC}"], "flagging queries nothing")

    def test_identity_is_the_com_objects(self) -> None:
        first = self.app.ActiveDoc
        second = self.app.ActiveDoc
        self.assertIsNot(first, second)
        self.assertEqual(first, second)
        self.assertTrue(first)
        self.assertNotEqual(first, self.app)


class LibraryIndexTests(unittest.TestCase):
    def test_the_base_class_a_generated_module_carries_is_not_indexed(self) -> None:
        """Regression (live on 2016 SP3): indexing DispatchBaseClass itself raised AttributeError on
        _prop_map_get_ at the first early-bound attach."""
        library = Library(MODULE)
        self.assertNotIn(DispatchBaseClass, library.declaring("GetType"))
        self.assertEqual([cls.__name__ for cls in library.declaring("GetType")],
                         ["IModelDoc2", "ISketchSegment", "IEntity"])


class RawApp(win32com.client.dynamic.CDispatch):
    """What GetActiveObject returns without a gen_py cache (a dynamic dispatch), as far as the binding looks at it."""

    def __init__(self, revision="34.4.0", revision_as_method=False) -> None:
        self.__dict__.update(
            _oleobj_=FakeOle("app", {"{APP}"}, type_info=False),
            RevisionNumber=(lambda: revision) if revision_as_method else revision,
        )


class BindApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        sw_binding._libraries.clear()
        sw_binding._active = None
        sw_binding._warned.clear()
        self.requested: list[tuple] = []

        def ensure_module(guid, lcid, major, minor):
            self.requested.append((guid, lcid, major, minor))
            return MODULE

        patches = [
            unittest.mock.patch.object(sw_binding.gencache, "EnsureModule", ensure_module),
            unittest.mock.patch.dict(os.environ, {}, clear=False),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop(sw_binding.ENV_VAR, None)
        self.addCleanup(sw_binding._libraries.clear)
        self.addCleanup(setattr, sw_binding, "_active", None)

    def test_the_default_is_late_and_loads_no_type_library(self) -> None:
        raw = RawApp()
        self.assertIs(sw_binding.bind_application(raw), raw)
        self.assertEqual(self.requested, [])
        self.assertIsNone(sw_binding._active)
        with unittest.mock.patch("win32com.client.Dispatch", side_effect=lambda o: ("late", o)):
            self.assertEqual(sw_binding.bind("pointer"), ("late", "pointer"))

    def test_late_rewraps_a_generated_class_that_a_shared_cache_produced(self) -> None:
        """#27: with sldworks.tlb wrappers in gen_py, GetActiveObject returns the generated coclass
        SldWorks (a CoClassBaseClass, measured live on 2016 SP3), or an interface class; late binding
        must not change because of either."""
        for make in (lambda ole: SldWorksCoClass(ole), lambda ole: ISldWorks(ole)):
            ole = FakeOle("app", {"{APP}"}, type_info=False)
            seen = []
            with unittest.mock.patch("win32com.client.dynamic.Dispatch",
                                     side_effect=lambda o, name: seen.append((o, name)) or "dynamic"):
                self.assertEqual(sw_binding.bind_application(make(ole)), "dynamic")
            self.assertEqual(seen, [(ole, "SldWorks.Application")])
        self.assertEqual(self.requested, [])

    def test_late_keeps_a_dynamic_dispatch_as_it_is(self) -> None:
        raw = win32com.client.dynamic.CDispatch(FakeOle("app"), None)
        with unittest.mock.patch("win32com.client.dynamic.Dispatch") as rewrap:
            self.assertIs(sw_binding.bind_application(raw), raw)
        rewrap.assert_not_called()

    def test_late_flag_methods_does_not_warn_for_a_cache_produced_class(self) -> None:
        generated = SldWorksCoClass(FakeOle("app", {"{APP}"}, type_info=False))
        flagged = []

        class FakeCDispatch:
            def _FlagAsMethod(self, name):  # noqa: N802 - pywin32 member name
                flagged.append(name)

        with unittest.mock.patch.object(sw_core.win32com.client, "GetActiveObject", return_value=generated), \
                unittest.mock.patch("win32com.client.dynamic.Dispatch", return_value=FakeCDispatch()):
            with self.assertNoLogs("solidworks-mcp", level="WARNING"):
                sw_core.running_app()
        self.assertEqual(flagged, list(sw_core._APP_METHODS))

    def test_late_can_be_set_explicitly(self) -> None:
        os.environ[sw_binding.ENV_VAR] = "late"
        raw = RawApp()
        self.assertIs(sw_binding.bind_application(raw), raw)
        self.assertEqual(self.requested, [])

    def test_early_loads_the_type_library_of_the_running_release(self) -> None:
        os.environ[sw_binding.ENV_VAR] = "EARLY"
        app = sw_binding.bind_application(RawApp("34.4.0"))
        self.assertIsInstance(app, EarlyBound)
        self.assertEqual(repr(app), "<EarlyBound ISldWorks>")
        self.assertEqual(self.requested, [(sw_binding.TYPELIB_GUID, 0, 34, 0)])
        self.assertIsInstance(sw_binding.bind(Dynamic(FakeOle("view", {"{FEAT}"}))), EarlyBound)
        # A second attach to the same release reuses the library; a method-shaped RevisionNumber reads too.
        sw_binding.bind_application(RawApp("34.4.0", revision_as_method=True))
        self.assertEqual(len(self.requested), 1)
        sw_binding.bind_application(RawApp("24.3.0"))
        self.assertEqual(self.requested[-1], (sw_binding.TYPELIB_GUID, 0, 24, 0))

    def test_switching_back_to_late_drops_the_early_library(self) -> None:
        os.environ[sw_binding.ENV_VAR] = "early"
        sw_binding.bind_application(RawApp())
        self.assertIsNotNone(sw_binding._active)
        os.environ[sw_binding.ENV_VAR] = "late"
        raw = RawApp()
        self.assertIs(sw_binding.bind_application(raw), raw)
        self.assertIsNone(sw_binding._active)

    def test_an_unknown_mode_is_late_with_a_warning(self) -> None:
        os.environ[sw_binding.ENV_VAR] = "auto"
        raw = RawApp()
        with self.assertLogs("solidworks-mcp", level="WARNING") as logs:
            self.assertIs(sw_binding.bind_application(raw), raw)
        self.assertIn("auto", logs.output[0])
        self.assertEqual(self.requested, [])

    def test_a_missing_type_library_falls_back_to_late_binding_with_one_warning(self) -> None:
        os.environ[sw_binding.ENV_VAR] = "early"

        def not_registered(*args):
            raise pythoncom.com_error(winerror.TYPE_E_LIBNOTREGISTERED, "Library not registered.", None, None)

        with unittest.mock.patch.object(sw_binding.gencache, "EnsureModule", not_registered):
            raw = RawApp("34.4.0")
            with self.assertLogs("solidworks-mcp", level="WARNING") as logs:
                self.assertIs(sw_binding.bind_application(raw), raw)
                self.assertIs(sw_binding.bind_application(raw), raw)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("34.0", logs.output[0])
        self.assertIsNone(sw_binding._active)

    def test_a_module_without_the_application_interface_falls_back(self) -> None:
        os.environ[sw_binding.ENV_VAR] = "early"
        with unittest.mock.patch.object(sw_binding.gencache, "EnsureModule", lambda *a: types.SimpleNamespace()):
            raw = RawApp("34.4.0")
            with self.assertLogs("solidworks-mcp", level="WARNING"):
                self.assertIs(sw_binding.bind_application(raw), raw)

    def test_an_unreadable_revision_falls_back(self) -> None:
        os.environ[sw_binding.ENV_VAR] = "early"
        raw = RawApp("")
        with self.assertLogs("solidworks-mcp", level="WARNING") as logs:
            self.assertIs(sw_binding.bind_application(raw), raw)
        self.assertIn("revision", logs.output[0])
        self.assertEqual(self.requested, [])

    def test_running_app_binds_what_get_active_object_returns(self) -> None:
        os.environ[sw_binding.ENV_VAR] = "early"
        raw = RawApp("34.4.0")
        with unittest.mock.patch.object(sw_core.win32com.client, "GetActiveObject", return_value=raw):
            app = sw_core.running_app()
        self.assertIsInstance(app, EarlyBound)
        self.assertEqual(raw._oleobj_.calls, [], "flagging an early-bound application makes no call")


if __name__ == "__main__":
    unittest.main()
