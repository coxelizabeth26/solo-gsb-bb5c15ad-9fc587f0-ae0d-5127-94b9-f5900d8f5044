"""原文修订：把一批编辑一次性应用到原文，并迁移/作废既有片段。

本模块与 HTTP 框架解耦，便于单元测试。

约定（与 redaction 模块一致）：
- 位置按 Unicode 码点计数，区间左闭右开 ``[start, end)``。
- 编辑坐标一律针对**原**文：``start == end`` 为插入，``start < end`` 为
  删除（``replacement == ""``）或替换。
- 所有编辑须互不重叠，且同一位置不允许多次插入；由调用方（接口校验层）
  保证，本模块不再重复检查。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .redaction import Fragment


@dataclass(frozen=True)
class Edit:
    """一处针对原文的编辑。"""

    start: int
    end: int
    replacement: str

    @property
    def is_insertion(self) -> bool:
        return self.start == self.end


def apply_edits(text: str, edits: Sequence[Edit]) -> str:
    """把 ``edits`` 一次性应用到 ``text``，返回修订后文本。

    所有编辑坐标都基于原文，因此从右往左依次应用：已应用的编辑位于
    未应用编辑的右侧，不会改变后者的坐标。
    """
    revised = text
    for edit in sorted(edits, key=lambda e: (e.start, e.end), reverse=True):
        revised = revised[: edit.start] + edit.replacement + revised[edit.end :]
    return revised


def migrate_fragment(fragment: Fragment, edits: Sequence[Edit]) -> Fragment | None:
    """按编辑迁移片段区间；片段被编辑波及时返回 ``None``（作废）。

    作废条件（均按原文坐标判断）：
    - 删除/替换区间与片段有交集；
    - 插入点严格落在片段内部。

    迁移规则（针对未作废的片段）：
    - 插入在片段起点：整段右移，不吞并插入内容；
    - 插入在片段终点：片段不扩展；
    - 其余位于片段前方的编辑，按累计码点位移平移片段区间。
    """
    start, end = fragment.start, fragment.end
    delta = 0
    for edit in edits:
        if edit.is_insertion:
            point = edit.start
            if start < point < end:
                return None  # 插入点严格落在片段内部
            if point <= start:
                # 插入在起点（或更前方）：整段右移
                delta += len(edit.replacement)
            # point >= end：插入在终点或之后，片段不变
        else:
            if edit.start < end and start < edit.end:
                return None  # 删除/替换区间与片段有交集
            if edit.end <= start:
                # 编辑整体在片段之前：累计码点位移
                delta += len(edit.replacement) - (edit.end - edit.start)
    return Fragment(
        id=fragment.id,
        weight=fragment.weight,
        start=start + delta,
        end=end + delta,
    )
