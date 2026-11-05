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

"""Live check: create_new_document with template_path (ClauSW#265).

Run this only on a workstation with SOLIDWORKS already running:

    ..\\.venv\\Scripts\\python.exe tests\\live_template_path.py [--template C:\\path\\to\\Other.prtdot]

Without `--template` the part template configured in SOLIDWORKS is copied under another name into a temporary
folder, so the named template is a file SOLIDWORKS does not have as its default. The script creates a part from it
and checks that the answer names exactly that file as `argument` and that the new part is the active document; then
it checks that a relative path, a missing file and an assembly template create nothing (the list of open documents
stays the same). The SOLIDWORKS settings are only read, never changed. The part it created is closed without saving.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solidworks_mcp.sw_core import running_app
from solidworks_mcp.sw_file import close_document, create_new_document, get_active_document_info, list_open_documents


def open_documents() -> list[tuple[str, str]]:
    answer = list_open_documents({})
    if not answer.get("ok"):
        raise AssertionError(f"list_open_documents failed: {answer.get('message')}")
    return [(str(d.get("title")), str(d.get("path"))) for d in answer["data"]["documents"]]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", help="a part template other than the SOLIDWORKS default; default: a copy of it")
    args = parser.parse_args()
    failures: list[str] = []

    def check(condition: bool, text: str) -> None:
        print(("ok    " if condition else "FAIL  ") + text)
        if not condition:
            failures.append(text)

    default = str(running_app().GetUserPreferenceStringValue(1) or "")
    print(f"SOLIDWORKS default part template: {default!r}")
    with tempfile.TemporaryDirectory() as folder:
        if args.template:
            template = str(Path(args.template))
        else:
            if not Path(default).is_file():
                print("FAIL  no default part template to copy; pass --template")
                return 1
            template = str(Path(folder) / "Live Copy.prtdot")
            shutil.copyfile(default, template)
        check(template.lower() != default.lower(), f"named template differs from the default: {template!r}")

        before = open_documents()
        answer: dict[str, Any] = create_new_document({"kind": "part", "template_path": template})
        data = answer.get("data") or {}
        title = str((data.get("document") or {}).get("title") or "")
        try:
            check(bool(answer.get("ok")), f"create_new_document ok: {answer.get('message')}")
            check(data.get("template") == template, f"answer names the template: {data.get('template')!r}")
            check(data.get("template_source") == "argument", f"template_source: {data.get('template_source')!r}")
            active = (get_active_document_info({}).get("data") or {}).get("document") or {}
            check(bool(title) and active.get("title") == title, f"new part {title!r} is active ({active.get('title')!r})")
        finally:
            if title:
                closed = close_document({"title": title})
                check(bool(closed.get("ok")), f"own part {title!r} closed: {closed.get('message')}")
        check(open_documents() == before, "open documents as before the call")

        assembly = Path(folder) / "Live Copy.asmdot"
        assembly.write_bytes(b"")
        for name, path in (("relative", "Live Copy.prtdot"), ("missing", str(Path(folder) / "Fehlt.prtdot")),
                           ("assembly template", str(assembly))):
            refused = create_new_document({"kind": "part", "template_path": path})
            check(not refused.get("ok") and "data" not in refused, f"{name} path refused: {refused.get('message')}")
            check(open_documents() == before, f"{name} path created no document")

    print("LIVE TEMPLATE PATH " + ("OK" if not failures else f"FAILED ({len(failures)})"))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
