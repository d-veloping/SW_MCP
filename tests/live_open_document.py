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

"""Live measurement: open_document after another process opened a document, already_open, only_if_clean.

ClauSW issue #107 saw open_document answer with the previously active document when another process had opened it
a second before.  Per round this script lets a second process open `--foreign` (or the target itself), then calls
open_document on `--target` and records whether the answer names the target, `already_open` and the wait.  The
`clean` condition checks close_document with only_if_clean on an unchanged and on a changed target.

Run this only on a workstation with SOLIDWORKS already running and nothing else driving it, on copies of parts:

    ..\\.venv\\Scripts\\python.exe tests\\live_open_document.py --target C:\\tmp\\Bolzen.sldprt --foreign C:\\tmp\\Fremd.sldprt

Every document is closed again without saving at the end of its round.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solidworks_mcp.sw_feature import rename_feature_tool
from solidworks_mcp.sw_file import close_document, list_open_documents, open_document
from solidworks_mcp.sw_inspect import list_features


def open_elsewhere(path: str) -> None:
    """open_document in a second process, like a person or another client."""
    code = ("import sys; sys.path.insert(0, sys.argv[2]); from solidworks_mcp.sw_file import open_document; "
            "a = open_document({'path': sys.argv[1]}); sys.exit(0 if a.get('ok') else 1)")
    subprocess.run([sys.executable, "-c", code, path, str(ROOT)], check=True, timeout=120)


def open_titles() -> list[str]:
    return [str(d.get("title")) for d in (list_open_documents({}).get("data") or {}).get("documents", [])]


def close_all(paths: list[str]) -> None:
    for path in paths:
        close_document({"title": path})


def round_open(condition: str, target: str, foreign: str) -> dict[str, Any]:
    open_elsewhere(foreign if condition == "foreign" else target)
    answer = open_document({"path": target})
    data = answer.get("data") or {}
    named = str((data.get("document") or {}).get("path") or "")
    row = {"condition": condition, "ok": bool(answer.get("ok")), "names_target": named.lower() == target.lower(),
           "already_open": data.get("already_open"), "wait_ms": data.get("wait_ms"), "message": answer.get("message")}
    close_all([target, foreign])
    return row


def round_clean(target: str) -> dict[str, Any]:
    open_document({"path": target})
    clean = close_document({"title": target, "only_if_clean": True})
    open_document({"path": target})
    features = [f.get("name") for f in (list_features({}).get("data") or {}).get("features", [])]
    rename_feature_tool({"feature_name": features[-1], "new_name": f"{features[-1]}_live"})
    dirty = close_document({"title": target, "only_if_clean": True})
    still_open = any(Path(target).stem == t for t in open_titles())
    close_all([target])
    return {"condition": "clean", "clean_closed": bool(clean.get("ok")),
            "dirty_refused": not dirty.get("ok") and (dirty.get("data") or {}).get("reason") == "dirty",
            "dirty_still_open": still_open}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", required=True)
    parser.add_argument("--foreign", required=True)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--json", help="write every round as JSON to this file")
    args = parser.parse_args()
    target, foreign = str(Path(args.target).resolve()), str(Path(args.foreign).resolve())
    rows = []
    for condition in ("foreign", "already"):
        for _ in range(args.rounds):
            rows.append(round_open(condition, target, foreign))
            print(json.dumps(rows[-1]), flush=True)
    rows.append(round_clean(target))
    print(json.dumps(rows[-1]), flush=True)
    summary = {c: {"rounds": len(g), "not_ok": sum(not r["ok"] for r in g),
                   "wrong_document": sum(not r["names_target"] for r in g),
                   "already_open": sorted({str(r["already_open"]) for r in g}),
                   "wait_ms_max": max((r["wait_ms"] or 0) for r in g)}
               for c in ("foreign", "already") for g in [[r for r in rows if r["condition"] == c]]}
    summary["clean"] = rows[-1]
    print(json.dumps(summary, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps({"summary": summary, "rounds": rows}, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
