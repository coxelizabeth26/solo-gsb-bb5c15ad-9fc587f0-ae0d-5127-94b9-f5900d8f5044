"""API 端到端测试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def post(payload: object) -> object:
    return client.post("/redact", json=payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 正常流程
# ---------------------------------------------------------------------------


def test_end_to_end_overlapping_and_masking() -> None:
    resp = post(
        {
            "text": "你好，世界！",
            "fragments": [
                {"id": 1, "weight": 5, "start": 0, "end": 3},
                {"id": 2, "weight": 3, "start": 0, "end": 2},
                {"id": 3, "weight": 1, "start": 2, "end": 5},
            ],
        }
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # 片段 1（[0,3) 权重 5）与 2、3 都冲突；2+3 相邻可组合（权重 4）。
    # 按权重总和取 1，尽管 2+3 遮盖字符更多。
    assert data["selected_ids"] == [1]
    assert data["rejected_ids"] == [2, 3]
    # 原文 0..3 为“你好，”，遮盖后保留“世界！”。
    assert data["masked_text"] == "███世界！"


def test_adjacent_fragments_both_selected() -> None:
    resp = post(
        {
            "text": "abcdef",
            "fragments": [
                {"id": 2, "weight": 1, "start": 3, "end": 6},
                {"id": 1, "weight": 1, "start": 0, "end": 3},
            ],
        }
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["selected_ids"] == [1, 2]
    assert data["rejected_ids"] == []
    assert data["masked_text"] == "██████"


def test_empty_fragment_list_returns_original_text() -> None:
    resp = post({"text": "abc", "fragments": []})
    assert resp.status_code == 200
    assert resp.json() == {"selected_ids": [], "rejected_ids": [], "masked_text": "abc"}


def test_string_ids_supported_and_sorted() -> None:
    resp = post(
        {
            "text": "abc",
            "fragments": [
                {"id": "x", "weight": 1, "start": 0, "end": 1},
                {"id": "a", "weight": 1, "start": 1, "end": 2},
            ],
        }
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["selected_ids"] == ["a", "x"]
    assert data["rejected_ids"] == []
    assert data["masked_text"] == "██c"


def test_health_and_root() -> None:
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/").status_code == 200


def test_openapi_available() -> None:
    schema = client.get("/openapi.json").json()
    assert "/redact" in schema["paths"]


# ---------------------------------------------------------------------------
# 拒绝类输入：全部 400
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        # 缺字段
        {"fragments": []},
        {"text": "abc"},
        # 类型错误
        {"text": 123, "fragments": []},
        {"text": "abc", "fragments": {}},
        # 片段缺字段 / 类型错误
        {"text": "abc", "fragments": [{"id": 1, "weight": 1, "start": 0}]},
        {"text": "abc", "fragments": [{"id": 1, "weight": "1", "start": 0, "end": 1}]},
        {"text": "abc", "fragments": [{"id": 1.0, "weight": 1, "start": 0, "end": 1}]},
        {"text": "abc", "fragments": [{"id": True, "weight": 1, "start": 0, "end": 1}]},
        {"text": "abc", "fragments": [{"id": 1, "weight": True, "start": 0, "end": 1}]},
        {"text": "abc", "fragments": [{"id": 1, "weight": 1, "start": 0.5, "end": 1}]},
        {"id": None},
        # 权重非正
        {"text": "abc", "fragments": [{"id": 1, "weight": 0, "start": 0, "end": 1}]},
        {"text": "abc", "fragments": [{"id": 1, "weight": -2, "start": 0, "end": 1}]},
        # 越界
        {"text": "abc", "fragments": [{"id": 1, "weight": 1, "start": 0, "end": 4}]},
        {"text": "abc", "fragments": [{"id": 1, "weight": 1, "start": -1, "end": 2}]},
        # 空片段 / 反向
        {"text": "abc", "fragments": [{"id": 1, "weight": 1, "start": 1, "end": 1}]},
        {"text": "abc", "fragments": [{"id": 1, "weight": 1, "start": 2, "end": 1}]},
        # 重复 ID
        {"text": "abc", "fragments": [
            {"id": 1, "weight": 1, "start": 0, "end": 1},
            {"id": 1, "weight": 1, "start": 1, "end": 2},
        ]},
        # ID 类型混用
        {"text": "abc", "fragments": [
            {"id": 1, "weight": 1, "start": 0, "end": 1},
            {"id": "1", "weight": 1, "start": 1, "end": 2},
        ]},
        # 空字符串 ID
        {"text": "abc", "fragments": [{"id": "", "weight": 1, "start": 0, "end": 1}]},
    ],
)
def test_invalid_payloads_rejected_with_400(payload: object) -> None:
    resp = post(payload)
    assert resp.status_code == 400, payload
    assert "detail" in resp.json()


def test_invalid_json_body_rejected_with_400() -> None:
    resp = client.post("/redact", content="{not json", headers={"Content-Type": "application/json"})
    assert resp.status_code == 400


def test_empty_body_rejected_with_400() -> None:
    resp = client.post("/redact", content=b"", headers={"Content-Type": "application/json"})
    assert resp.status_code == 400


def test_extra_fields_are_ignored() -> None:
    resp = post(
        {
            "text": "abc",
            "fragments": [{"id": 1, "weight": 1, "start": 0, "end": 1, "note": "x"}],
            "request_id": "r-1",
        }
    )
    assert resp.status_code == 200
    assert resp.json()["selected_ids"] == [1]


# ---------------------------------------------------------------------------
# POST /redact/revise：修订原文 + 迁移/作废片段 + 选择遮盖
# ---------------------------------------------------------------------------


def post_revise(payload: object) -> object:
    return client.post("/redact/revise", json=payload)  # type: ignore[arg-type]


def test_revise_insert_before_fragment_shifts_it() -> None:
    resp = post_revise(
        {
            "text": "abcdef",
            "fragments": [{"id": 1, "weight": 5, "start": 2, "end": 4}],  # "cd"
            "edits": [{"start": 0, "end": 0, "replacement": "XY"}],
        }
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["revised_text"] == "XYabcdef"
    assert data["invalidated_ids"] == []
    assert data["selected_ids"] == [1]
    assert data["rejected_ids"] == []
    # 片段迁移到 [4, 6)，遮盖的仍是 "cd"。
    assert data["masked_text"] == "XYab██ef"


def test_revise_insert_at_fragment_start_shifts_right() -> None:
    resp = post_revise(
        {
            "text": "abcdef",
            "fragments": [{"id": 1, "weight": 5, "start": 2, "end": 4}],
            "edits": [{"start": 2, "end": 2, "replacement": "XY"}],
        }
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["revised_text"] == "abXYcdef"
    assert data["invalidated_ids"] == []
    # 整段右移，不吞并插入内容：遮盖 "cd" 而非 "XYcd"。
    assert data["masked_text"] == "abXY██ef"


def test_revise_insert_at_fragment_end_does_not_extend() -> None:
    resp = post_revise(
        {
            "text": "abcdef",
            "fragments": [{"id": 1, "weight": 5, "start": 2, "end": 4}],
            "edits": [{"start": 4, "end": 4, "replacement": "XY"}],
        }
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["revised_text"] == "abcdXYef"
    assert data["invalidated_ids"] == []
    # 片段不扩展：遮盖的仍是 "cd"。
    assert data["masked_text"] == "ab██XYef"


def test_revise_insert_strictly_inside_invalidates() -> None:
    resp = post_revise(
        {
            "text": "abcdef",
            "fragments": [{"id": 1, "weight": 5, "start": 2, "end": 5}],
            "edits": [{"start": 3, "end": 3, "replacement": "X"}],
        }
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["revised_text"] == "abcXdef"
    assert data["invalidated_ids"] == [1]
    assert data["selected_ids"] == []
    assert data["rejected_ids"] == []
    assert data["masked_text"] == "abcXdef"


def test_revise_delete_invalidates_intersecting_and_shifts_rest() -> None:
    resp = post_revise(
        {
            "text": "abcdef",
            "fragments": [
                {"id": 1, "weight": 5, "start": 2, "end": 4},  # 与删除相交
                {"id": 2, "weight": 3, "start": 5, "end": 6},  # 在删除之后
            ],
            "edits": [{"start": 3, "end": 5, "replacement": ""}],
        }
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["revised_text"] == "abcf"
    assert data["invalidated_ids"] == [1]
    assert data["selected_ids"] == [2]
    assert data["rejected_ids"] == []
    # 片段 2（"f"）左移 2 码点到 [3, 4)。
    assert data["masked_text"] == "abc█"


def test_revise_replace_before_fragment_shifts_by_delta() -> None:
    resp = post_revise(
        {
            "text": "hello world",
            "fragments": [{"id": 1, "weight": 2, "start": 6, "end": 11}],  # "world"
            "edits": [{"start": 0, "end": 5, "replacement": "hi"}],
        }
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["revised_text"] == "hi world"
    assert data["invalidated_ids"] == []
    assert data["masked_text"] == "hi █████"


def test_revise_selection_reuses_redact_logic() -> None:
    # 与 /redact 文档示例相同的片段；编辑只追加字符，不影响选择结果。
    resp = post_revise(
        {
            "text": "你好，世界！",
            "fragments": [
                {"id": 1, "weight": 5, "start": 0, "end": 3},
                {"id": 2, "weight": 3, "start": 0, "end": 2},
                {"id": 3, "weight": 1, "start": 2, "end": 5},
            ],
            "edits": [{"start": 6, "end": 6, "replacement": "~"}],
        }
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["revised_text"] == "你好，世界！~"
    assert data["invalidated_ids"] == []
    assert data["selected_ids"] == [1]
    assert data["rejected_ids"] == [2, 3]
    assert data["masked_text"] == "███世界！~"


def test_revise_surviving_fragments_compete_after_migration() -> None:
    # 迁移后仍重叠的保留片段按权重竞争，落选者进 rejected_ids。
    resp = post_revise(
        {
            "text": "abcdef",
            "fragments": [
                {"id": 1, "weight": 5, "start": 1, "end": 4},
                {"id": 2, "weight": 3, "start": 2, "end": 5},
            ],
            "edits": [{"start": 0, "end": 0, "replacement": "Z"}],
        }
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["revised_text"] == "Zabcdef"
    assert data["invalidated_ids"] == []
    assert data["selected_ids"] == [1]
    assert data["rejected_ids"] == [2]
    assert data["masked_text"] == "Za███ef"


def test_revise_empty_edits_matches_redact() -> None:
    payload = {
        "text": "你好，世界！",
        "fragments": [
            {"id": 1, "weight": 5, "start": 0, "end": 3},
            {"id": 2, "weight": 3, "start": 0, "end": 2},
            {"id": 3, "weight": 1, "start": 2, "end": 5},
        ],
    }
    redact = post(payload).json()
    revise = post_revise({**payload, "edits": []}).json()
    assert revise["revised_text"] == payload["text"]
    assert revise["invalidated_ids"] == []
    assert revise["selected_ids"] == redact["selected_ids"]
    assert revise["rejected_ids"] == redact["rejected_ids"]
    assert revise["masked_text"] == redact["masked_text"]


def test_revise_empty_fragment_list() -> None:
    resp = post_revise(
        {
            "text": "abc",
            "fragments": [],
            "edits": [{"start": 1, "end": 2, "replacement": "X"}],
        }
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "revised_text": "aXc",
        "invalidated_ids": [],
        "selected_ids": [],
        "rejected_ids": [],
        "masked_text": "aXc",
    }


def test_revise_unicode_code_points() -> None:
    resp = post_revise(
        {
            "text": "a😀b",  # 3 个码点
            "fragments": [{"id": 1, "weight": 1, "start": 1, "end": 2}],  # 😀
            "edits": [{"start": 1, "end": 1, "replacement": "z"}],
        }
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["revised_text"] == "az😀b"
    assert data["invalidated_ids"] == []
    assert data["masked_text"] == "az█b"


def test_revise_string_ids_sorted() -> None:
    resp = post_revise(
        {
            "text": "abcdef",
            "fragments": [
                {"id": "b", "weight": 1, "start": 1, "end": 3},
                {"id": "a", "weight": 1, "start": 0, "end": 2},
            ],
            "edits": [{"start": 6, "end": 6, "replacement": "!"}],
        }
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["revised_text"] == "abcdef!"
    # 两片段迁移后仍重叠（[1,3) 与 [0,2)），权重并列、遮盖长度并列，
    # 取 ID 升序列表字典序最小者："a"。
    assert data["selected_ids"] == ["a"]
    assert data["rejected_ids"] == ["b"]
    assert data["masked_text"] == "██cdef!"


def test_revise_openapi_available() -> None:
    schema = client.get("/openapi.json").json()
    assert "/redact/revise" in schema["paths"]


# ---------------------------------------------------------------------------
# /redact/revise 拒绝类输入：全部 400
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        # 缺字段
        {"text": "abc", "fragments": []},
        {"fragments": [], "edits": []},
        {"text": "abc", "edits": []},
        # edits 类型错误
        {"text": "abc", "fragments": [], "edits": {}},
        {"text": "abc", "fragments": [], "edits": ["x"]},
        # 编辑缺字段 / 类型错误
        {"text": "abc", "fragments": [], "edits": [{"start": 0, "end": 1}]},
        {"text": "abc", "fragments": [], "edits": [
            {"start": "0", "end": 1, "replacement": "x"},
        ]},
        {"text": "abc", "fragments": [], "edits": [
            {"start": 0, "end": True, "replacement": "x"},
        ]},
        {"text": "abc", "fragments": [], "edits": [
            {"start": 0, "end": 1, "replacement": 1},
        ]},
        # 越界
        {"text": "abc", "fragments": [], "edits": [
            {"start": -1, "end": 1, "replacement": ""},
        ]},
        {"text": "abc", "fragments": [], "edits": [
            {"start": 0, "end": 4, "replacement": ""},
        ]},
        {"text": "abc", "fragments": [], "edits": [
            {"start": 4, "end": 4, "replacement": "x"},
        ]},
        # 逆序
        {"text": "abc", "fragments": [], "edits": [
            {"start": 2, "end": 1, "replacement": ""},
        ]},
        # 编辑彼此重叠
        {"text": "abcdef", "fragments": [], "edits": [
            {"start": 0, "end": 3, "replacement": ""},
            {"start": 2, "end": 4, "replacement": ""},
        ]},
        # 插入点落在另一编辑区间内部
        {"text": "abcdef", "fragments": [], "edits": [
            {"start": 1, "end": 4, "replacement": "x"},
            {"start": 2, "end": 2, "replacement": "y"},
        ]},
        # 同一位置的多次插入
        {"text": "abc", "fragments": [], "edits": [
            {"start": 1, "end": 1, "replacement": "x"},
            {"start": 1, "end": 1, "replacement": "y"},
        ]},
        # 片段校验沿用 /redact 规则（针对原文）
        {"text": "abc", "fragments": [
            {"id": 1, "weight": 1, "start": 0, "end": 4},
        ], "edits": []},
        {"text": "abc", "fragments": [
            {"id": 1, "weight": 0, "start": 0, "end": 1},
        ], "edits": []},
    ],
)
def test_revise_invalid_payloads_rejected_with_400(payload: object) -> None:
    resp = post_revise(payload)
    assert resp.status_code == 400, payload
    assert "detail" in resp.json()


def test_revise_invalid_json_body_rejected_with_400() -> None:
    resp = client.post(
        "/redact/revise", content="{not json", headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 400


def test_revise_allows_edits_at_replace_boundaries() -> None:
    # 插入在替换区间起点或终点不算重叠；相邻删除/替换也合法。
    resp = post_revise(
        {
            "text": "abcdef",
            "fragments": [{"id": 1, "weight": 1, "start": 4, "end": 6}],
            "edits": [
                {"start": 1, "end": 1, "replacement": "X"},   # 在替换起点插入
                {"start": 1, "end": 3, "replacement": "Y"},   # 替换 bc -> Y
                {"start": 3, "end": 3, "replacement": "Z"},   # 在替换终点插入
                {"start": 3, "end": 4, "replacement": ""},    # 相邻删除 d
            ],
        }
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["revised_text"] == "aXYZef"
    assert data["invalidated_ids"] == []
    # 片段 "ef" 累计位移 +1-1+1-1 = 0，仍在 [4, 6)。
    assert data["masked_text"] == "aXYZ██"
