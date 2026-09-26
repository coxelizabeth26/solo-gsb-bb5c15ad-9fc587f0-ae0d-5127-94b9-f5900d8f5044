"""原文修订与片段迁移的单元测试。"""

from __future__ import annotations

from app.redaction import Fragment
from app.revision import Edit, apply_edits, migrate_fragment


def f(fid: int, weight: int, start: int, end: int) -> Fragment:
    return Fragment(id=fid, weight=weight, start=start, end=end)


# ---------------------------------------------------------------------------
# apply_edits：一次性应用编辑
# ---------------------------------------------------------------------------


def test_apply_no_edits_returns_original() -> None:
    assert apply_edits("abc", []) == "abc"


def test_apply_insert_delete_replace_together() -> None:
    text = "abcdef"
    edits = [
        Edit(0, 1, "X"),   # 替换 a -> X
        Edit(2, 4, ""),    # 删除 cd
        Edit(6, 6, "YZ"),  # 末尾插入 YZ
    ]
    assert apply_edits(text, edits) == "XbefYZ"


def test_apply_edits_use_original_coordinates() -> None:
    # 所有坐标都基于原文：插入不影响删除的坐标。
    text = "0123456789"
    edits = [Edit(0, 2, ""), Edit(5, 5, "ab")]
    assert apply_edits(text, edits) == "234ab56789"


def test_apply_insertion_at_replace_start_goes_before() -> None:
    # 插入在替换区间起点：插入内容位于替换内容之前。
    assert apply_edits("abcdef", [Edit(1, 1, "X"), Edit(1, 3, "Y")]) == "aXYdef"


def test_apply_insertion_at_replace_end_goes_after() -> None:
    # 插入在替换区间终点：插入内容位于替换内容之后。
    assert apply_edits("abcdef", [Edit(1, 3, "Y"), Edit(3, 3, "X")]) == "aYXdef"


def test_apply_edits_astral_code_points() -> None:
    text = "a😀b"  # 3 个码点
    assert apply_edits(text, [Edit(1, 2, "🌍")]) == "a🌍b"
    assert apply_edits(text, [Edit(1, 1, "xy")]) == "axy😀b"


# ---------------------------------------------------------------------------
# migrate_fragment：作废规则
# ---------------------------------------------------------------------------


def test_delete_or_replace_intersecting_invalidates() -> None:
    frag = f(1, 1, 5, 8)
    assert migrate_fragment(frag, [Edit(4, 6, "")]) is None    # 左搭界
    assert migrate_fragment(frag, [Edit(7, 9, "")]) is None    # 右搭界
    assert migrate_fragment(frag, [Edit(6, 7, "")]) is None    # 编辑在片段内部
    assert migrate_fragment(frag, [Edit(0, 10, "")]) is None   # 编辑包含片段
    assert migrate_fragment(frag, [Edit(5, 8, "x")]) is None   # 完全重合


def test_insert_strictly_inside_invalidates() -> None:
    frag = f(1, 1, 2, 5)
    assert migrate_fragment(frag, [Edit(3, 3, "x")]) is None
    assert migrate_fragment(frag, [Edit(4, 4, "x")]) is None


# ---------------------------------------------------------------------------
# migrate_fragment：迁移规则
# ---------------------------------------------------------------------------


def test_insert_at_start_shifts_whole_fragment_right() -> None:
    moved = migrate_fragment(f(1, 1, 2, 4), [Edit(2, 2, "abc")])
    assert moved is not None
    assert (moved.start, moved.end) == (5, 7)


def test_insert_at_end_does_not_extend_fragment() -> None:
    moved = migrate_fragment(f(1, 1, 2, 4), [Edit(4, 4, "abc")])
    assert moved is not None
    assert (moved.start, moved.end) == (2, 4)


def test_delete_before_shifts_left() -> None:
    moved = migrate_fragment(f(1, 1, 5, 8), [Edit(1, 3, "")])
    assert moved is not None
    assert (moved.start, moved.end) == (3, 6)


def test_replace_before_shifts_by_net_delta() -> None:
    # 2 个码点换成 4 个码点：净位移 +2。
    moved = migrate_fragment(f(1, 1, 5, 8), [Edit(1, 3, "abcd")])
    assert moved is not None
    assert (moved.start, moved.end) == (7, 10)


def test_adjacent_edits_do_not_invalidate() -> None:
    # 与片段左右相邻（不相交）的删除只产生位移。
    moved = migrate_fragment(f(1, 1, 5, 8), [Edit(3, 5, ""), Edit(8, 10, "")])
    assert moved is not None
    assert (moved.start, moved.end) == (3, 6)


def test_cumulative_displacement_from_multiple_edits() -> None:
    # -2（删除）+ 3（插入）+ 1（替换净增）= +2
    edits = [Edit(0, 2, ""), Edit(5, 5, "abc"), Edit(8, 9, "XY")]
    moved = migrate_fragment(f(1, 1, 10, 12), edits)
    assert moved is not None
    assert (moved.start, moved.end) == (12, 14)


def test_edits_after_fragment_have_no_effect() -> None:
    moved = migrate_fragment(f(1, 1, 2, 4), [Edit(6, 8, "xyz"), Edit(9, 9, "q")])
    assert moved is not None
    assert (moved.start, moved.end) == (2, 4)


def test_migration_preserves_id_and_weight() -> None:
    moved = migrate_fragment(f(7, 9, 2, 4), [Edit(0, 0, "ab")])
    assert moved is not None
    assert (moved.id, moved.weight) == (7, 9)


# ---------------------------------------------------------------------------
# 随机化不变量：未作废片段迁移后的区间，在修订后原文中必须对应相同内容。
# ---------------------------------------------------------------------------


def test_migrated_intervals_preserve_content_random() -> None:
    import random

    rng = random.Random(20260926)
    alphabet = "ab中😀"
    for trial in range(300):
        n = rng.randint(1, 30)
        text = "".join(rng.choice(alphabet) for _ in range(n))

        # 用采样出的分界点两两配对，构造互不重叠的删除/替换；
        # 余下的点作为插入位置（落在编辑边界上，不会与任何编辑重叠）。
        points = sorted(rng.sample(range(n + 1), k=rng.randint(0, min(10, n + 1))))
        edits: list[Edit] = []
        i = 0
        while i + 1 < len(points):
            edits.append(Edit(points[i], points[i + 1], rng.choice(["", "X", "YZ", "中文"])))
            i += 2
        if i < len(points):
            edits.append(Edit(points[i], points[i], rng.choice(["q", "😀"])))

        revised = apply_edits(text, edits)

        for _ in range(5):
            start = rng.randrange(n)
            end = rng.randrange(start + 1, n + 1)
            frag = f(1, 1, start, end)
            moved = migrate_fragment(frag, edits)
            if moved is None:
                continue
            assert revised[moved.start : moved.end] == text[start:end], (
                f"trial {trial}: text={text!r} edits={edits} frag=({start},{end})"
            )
