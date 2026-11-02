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


"""The view chain the drawing fakes share: IDrawingDoc.GetFirstView returns the current sheet, and IView.GetNextView
walks the views on that sheet in SOLIDWORKS' own order (sw_drawing._iter_views)."""

from __future__ import annotations


class ChainView:
    """A sheet (`kind` 1) or a drawing view in the chain, with the members every drawing fake needs."""

    def __init__(self, name: str, kind: int) -> None:
        self.name = name
        self.Type = kind
        self.next: ChainView | None = None

    def _FlagAsMethod(self, *names: str) -> None:  # noqa: N802 - pywin32 member name
        pass

    def GetName2(self) -> str:  # noqa: N802 - COM member name
        return self.name

    def GetNextView(self):  # noqa: N802 - COM member name
        return self.next


def first_view(views: list) -> ChainView | None:
    """Link `views` in list order and return the head of the chain, as GetFirstView does; None for an empty sheet."""
    for a, b in zip(views, views[1:] + [None]):
        a.next = b
    return views[0] if views else None
