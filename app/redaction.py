"""敏感片段选择与遮盖的核心算法。

本模块与 HTTP 框架解耦，便于单元测试。

约定：
- 文本位置按 Unicode 码点（Python ``str`` 的索引）计数，区间为左闭右开
  ``[start, end)``；``[0, 3)`` 与 ``[3, 5)`` 互不重叠，可同时入选。
- 片段 ID 允许为整数或字符串，一次请求内类型须一致。
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import Sequence

# 遮盖字符：U+2588 FULL BLOCK，一个码点对应一个被遮盖码点。
MASK_CHAR = "█"

ID = int | str


@dataclass(frozen=True)
class Fragment:
    """一条候选敏感片段。"""

    id: ID
    weight: int
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


def _insert_sorted(ids: tuple[ID, ...], value: ID) -> tuple[ID, ...]:
    """把 ``value`` 有序插入已升序排列的 ``ids``。"""
    lo, hi = 0, len(ids)
    while lo < hi:
        mid = (lo + hi) // 2
        if ids[mid] < value:
            lo = mid + 1
        else:
            hi = mid
    return ids[:lo] + (value,) + ids[lo:]


def select_fragments(fragments: Sequence[Fragment]) -> tuple[ID, ...]:
    """从候选片段中选出互不重叠的最优组合。

    目标依次为：
    1. 权重总和最大；
    2. 权重总和并列时，遮盖字符数最多；
    3. 仍并列时，取入选 ID 升序列表字典序最小者。

    使用带权区间调度动态规划，按右端点排序，``p[i]`` 指向与第 ``i`` 个
    片段相容（``end <= start``）的最后一个片段。

    返回入选 ID 的升序元组。
    """
    if not fragments:
        return ()

    # 唯一确定的排序，便于结果可复现；最优解由比较目标决定，与顺序无关。
    ordered = sorted(fragments, key=lambda f: (f.end, f.start, f.id))
    ends = [f.end for f in ordered]

    # dp[i]：仅考虑前 i 个片段（ordered[0:i]）时的最优状态
    #   (权重总和, 遮盖字符数, 入选 ID 升序元组)
    dp: list[tuple[int, int, tuple[ID, ...]]] = [(0, 0, ())]

    for i, frag in enumerate(ordered, start=1):
        p = bisect_right(ends, frag.start, 0, i - 1)
        base_weight, base_len, base_ids = dp[p]
        take = (
            base_weight + frag.weight,
            base_len + frag.length,
            _insert_sorted(base_ids, frag.id),
        )
        skip = dp[i - 1]
        # 元组按 (权重, 字符数, ID 列表字典序) 比较；前两项求最大，
        # 第三项取字典序最小。
        if take[:2] > skip[:2] or (take[:2] == skip[:2] and take[2] < skip[2]):
            dp.append(take)
        else:
            dp.append(skip)

    return dp[-1][2]


def mask_text(text: str, fragments: Sequence[Fragment]) -> str:
    """用等长的 ``█`` 遮盖入选片段覆盖到的码点。

    ``fragments`` 须已保证互不重叠（相邻可以）。重叠区域只遮盖一次。
    """
    covered = bytearray(len(text))
    for frag in fragments:
        covered[frag.start : frag.end] = b"\x01" * frag.length
    chars = list(text)
    for i, flag in enumerate(covered):
        if flag:
            chars[i] = MASK_CHAR
    return "".join(chars)
