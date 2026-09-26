"""公开档案敏感片段遮盖 API。

启动：``uvicorn app.main:app --host 0.0.0.0 --port 8000``
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .redaction import Fragment, mask_text, select_fragments
from .revision import Edit, apply_edits, migrate_fragment

app = FastAPI(
    title="敏感片段遮盖 API",
    version="1.1.0",
    description=(
        "提交原文及候选片段（含唯一 ID、正整数权重、左闭右开码点区间），"
        "返回互不重叠的最优入选组合、落选 ID 与等长遮盖文本。"
        "亦可提交针对原文的编辑（插入/删除/替换），一次性修订原文、"
        "迁移或作废既有片段后再进行选择遮盖。"
    ),
)


# /docs 中展示的请求体 JSON Schema；实际校验由 validate_payload 严格执行。
REDACT_REQUEST_SCHEMA = {
    "type": "object",
    "required": ["text", "fragments"],
    "properties": {
        "text": {
            "type": "string",
            "description": "待处理的原文，位置按 Unicode 码点计数",
        },
        "fragments": {
            "type": "array",
            "description": "候选片段，允许重叠；所有 ID 必须同为整数或同为字符串",
            "items": {
                "type": "object",
                "required": ["id", "weight", "start", "end"],
                "properties": {
                    "id": {
                        "oneOf": [{"type": "integer"}, {"type": "string"}],
                        "description": "片段唯一 ID，不可重复",
                    },
                    "weight": {
                        "type": "integer",
                        "exclusiveMinimum": 0,
                        "description": "正整数权重",
                    },
                    "start": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "左闭右开起始码点位置（含）",
                    },
                    "end": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "左闭右开结束码点位置（不含）",
                    },
                },
            },
        },
    },
    "example": {
        "text": "你好，世界！",
        "fragments": [
            {"id": 1, "weight": 5, "start": 0, "end": 2},
            {"id": 2, "weight": 3, "start": 3, "end": 5},
        ],
    },
}


class RedactOut(BaseModel):
    selected_ids: list[int | str] = Field(..., description="入选片段 ID（升序）")
    rejected_ids: list[int | str] = Field(..., description="落选片段 ID（升序）")
    masked_text: str = Field(..., description="入选片段以等长 █ 遮盖后的正文")


# /redact/revise 的请求体 JSON Schema（/docs 展示用）。
REVISE_REQUEST_SCHEMA = {
    "type": "object",
    "required": ["text", "fragments", "edits"],
    "properties": {
        "text": REDACT_REQUEST_SCHEMA["properties"]["text"],
        "fragments": REDACT_REQUEST_SCHEMA["properties"]["fragments"],
        "edits": {
            "type": "array",
            "description": (
                "针对原文的编辑，按原文码点定位；区间互不重叠，"
                "同一位置不允许多次插入"
            ),
            "items": {
                "type": "object",
                "required": ["start", "end", "replacement"],
                "properties": {
                    "start": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "左闭起始码点位置（含）",
                    },
                    "end": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "右开结束码点位置（不含）；与 start 相等表示插入",
                    },
                    "replacement": {
                        "type": "string",
                        "description": "替换 [start, end) 区间的内容；start == end 时为纯插入",
                    },
                },
            },
        },
    },
    "example": {
        "text": "你好，世界！",
        "fragments": [
            {"id": 1, "weight": 5, "start": 0, "end": 3},
            {"id": 2, "weight": 4, "start": 3, "end": 5},
        ],
        "edits": [
            {"start": 3, "end": 3, "replacement": "美丽的"},
            {"start": 5, "end": 6, "replacement": "地球"},
        ],
    },
}


class ReviseOut(BaseModel):
    revised_text: str = Field(..., description="应用全部编辑后的原文")
    invalidated_ids: list[int | str] = Field(
        ..., description="被编辑波及而作废的片段 ID（升序）"
    )
    selected_ids: list[int | str] = Field(
        ..., description="保留片段中入选的片段 ID（升序）"
    )
    rejected_ids: list[int | str] = Field(
        ..., description="保留片段中落选的片段 ID（升序）"
    )
    masked_text: str = Field(
        ..., description="修订后原文中入选片段以等长 █ 遮盖后的正文"
    )


def _bad(message: str) -> ValueError:
    """构造带统一中文消息的校验错误。"""
    return ValueError(message)


def _extract_text(payload: dict[str, Any]) -> str:
    if "text" not in payload:
        raise _bad("缺少字段：text")
    text = payload["text"]
    if not isinstance(text, str):
        raise _bad("text 必须是字符串")
    return text


def _validate_fragments(text: str, raw_fragments: Any) -> list[Fragment]:
    """校验候选片段数组，返回 Fragment 列表。"""
    if not isinstance(raw_fragments, list):
        raise _bad("fragments 必须是数组")

    fragments: list[Fragment] = []
    id_type: type | None = None
    seen_ids: set[Any] = set()

    for index, item in enumerate(raw_fragments):
        where = f"fragments[{index}]"
        if not isinstance(item, dict):
            raise _bad(f"{where} 必须是对象")

        for field in ("id", "weight", "start", "end"):
            if field not in item:
                raise _bad(f"{where} 缺少字段：{field}")

        raw_id = item["id"]
        # bool 是 int 的子类，必须显式排除；不接受 float（包括 1.0）。
        if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)):
            raise _bad(f"{where}.id 必须是整数或字符串")
        if isinstance(raw_id, str) and raw_id == "":
            raise _bad(f"{where}.id 不能为空字符串")
        if id_type is None:
            id_type = type(raw_id)
        elif not isinstance(raw_id, id_type):
            raise _bad(
                f"{where}.id 类型 {type(raw_id).__name__} 与既有 ID 类型 "
                f"{id_type.__name__} 不一致；一次请求内所有 ID 必须同为整数或同为字符串"
            )
        if raw_id in seen_ids:
            raise _bad(f"重复的片段 ID：{raw_id!r}")
        seen_ids.add(raw_id)

        weight = item["weight"]
        if isinstance(weight, bool) or not isinstance(weight, int):
            raise _bad(f"{where}.weight 必须是整数")
        if weight <= 0:
            raise _bad(f"{where}.weight 必须是正整数")

        start = item["start"]
        end = item["end"]
        if isinstance(start, bool) or not isinstance(start, int):
            raise _bad(f"{where}.start 必须是整数")
        if isinstance(end, bool) or not isinstance(end, int):
            raise _bad(f"{where}.end 必须是整数")
        if start < 0:
            raise _bad(f"{where}.start 不能为负数")
        if end > len(text):
            raise _bad(
                f"{where} 越界：end={end} 超出原文码点长度 {len(text)}"
            )
        if start >= end:
            raise _bad(
                f"{where} 是空片段或区间非法：要求 0 <= start < end，"
                f"实际 start={start}, end={end}"
            )

        fragments.append(Fragment(id=raw_id, weight=weight, start=start, end=end))

    return fragments


def validate_payload(payload: Any) -> tuple[str, list[Fragment]]:
    """严格校验 /redact 请求体，返回 ``(原文, Fragment 列表)``。

    任何不合法输入（类型错误、越界、空片段、重复 ID 等）均抛出
    ``ValueError``，由上层转为 HTTP 400。
    """
    if not isinstance(payload, dict):
        raise _bad("请求体必须是 JSON 对象，包含 text 与 fragments 字段")

    text = _extract_text(payload)

    if "fragments" not in payload:
        raise _bad("缺少字段：fragments")
    fragments = _validate_fragments(text, payload["fragments"])

    return text, fragments


def _validate_edits(text: str, raw_edits: Any) -> list[Edit]:
    """校验编辑数组，返回 Edit 列表。

    编辑坐标针对原文：要求 ``0 <= start <= end <= 原文码点长度``；
    编辑区间互不重叠，同一位置不允许多次插入。
    """
    if not isinstance(raw_edits, list):
        raise _bad("edits 必须是数组")

    edits: list[Edit] = []
    for index, item in enumerate(raw_edits):
        where = f"edits[{index}]"
        if not isinstance(item, dict):
            raise _bad(f"{where} 必须是对象")

        for field in ("start", "end", "replacement"):
            if field not in item:
                raise _bad(f"{where} 缺少字段：{field}")

        start = item["start"]
        end = item["end"]
        if isinstance(start, bool) or not isinstance(start, int):
            raise _bad(f"{where}.start 必须是整数")
        if isinstance(end, bool) or not isinstance(end, int):
            raise _bad(f"{where}.end 必须是整数")
        replacement = item["replacement"]
        if not isinstance(replacement, str):
            raise _bad(f"{where}.replacement 必须是字符串")
        if start < 0:
            raise _bad(f"{where}.start 不能为负数")
        if end > len(text):
            raise _bad(
                f"{where} 越界：end={end} 超出原文码点长度 {len(text)}"
            )
        if start > end:
            raise _bad(
                f"{where} 区间逆序：要求 start <= end，实际 start={start}, end={end}"
            )

        edits.append(Edit(start=start, end=end, replacement=replacement))

    # 重叠检查：按 (start, end) 排序后，相邻编辑须满足 prev.end <= cur.start；
    # 零宽区间（插入）天然不与任何区间相交，需单独拒绝同一位置的多次插入。
    ordered = sorted(edits, key=lambda e: (e.start, e.end))
    for prev, cur in zip(ordered, ordered[1:]):
        if cur.start < prev.end:
            raise _bad(
                f"编辑区间重叠：[{prev.start}, {prev.end}) 与 "
                f"[{cur.start}, {cur.end})"
            )
        if prev.is_insertion and cur.is_insertion and prev.start == cur.start:
            raise _bad(f"同一位置 {cur.start} 的多次插入")

    return edits


def validate_revise_payload(payload: Any) -> tuple[str, list[Fragment], list[Edit]]:
    """严格校验 /redact/revise 请求体，返回 ``(原文, Fragment 列表, Edit 列表)``。"""
    if not isinstance(payload, dict):
        raise _bad("请求体必须是 JSON 对象，包含 text、fragments 与 edits 字段")

    text = _extract_text(payload)

    if "fragments" not in payload:
        raise _bad("缺少字段：fragments")
    fragments = _validate_fragments(text, payload["fragments"])

    if "edits" not in payload:
        raise _bad("缺少字段：edits")
    edits = _validate_edits(text, payload["edits"])

    return text, fragments, edits


@app.exception_handler(ValueError)
async def value_error_handler(_request: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {"service": "sensitive-fragment-redaction", "docs": "/docs"}


@app.get("/health", tags=["其他"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/redact",
    response_model=RedactOut,
    tags=["遮盖"],
    summary="选择互不重叠片段并遮盖原文",
    openapi_extra={"requestBody": {"content": {"application/json": {"schema": REDACT_REQUEST_SCHEMA}}}},
)
async def redact(request: Request) -> RedactOut:
    # 自行解析 JSON：语法错误（空请求体、非法 JSON）统一转为 400；
    # 字段语义全部由 validate_payload 统一校验。
    try:
        payload = await request.json()
    except ValueError:
        raise _bad("请求体不是合法的 JSON")
    text, fragments = validate_payload(payload)

    selected = select_fragments(fragments)

    by_id = {f.id: f for f in fragments}
    chosen = [by_id[fragment_id] for fragment_id in selected]
    all_ids = sorted(f.id for f in fragments)

    masked = mask_text(text, chosen)
    return RedactOut(
        selected_ids=list(selected),
        rejected_ids=[i for i in all_ids if i not in set(selected)],
        masked_text=masked,
    )


@app.post(
    "/redact/revise",
    response_model=ReviseOut,
    tags=["遮盖"],
    summary="应用编辑修订原文，迁移/作废片段后再选择遮盖",
    openapi_extra={"requestBody": {"content": {"application/json": {"schema": REVISE_REQUEST_SCHEMA}}}},
)
async def revise(request: Request) -> ReviseOut:
    # 与 /redact 一样自行解析 JSON，语法错误统一转为 400。
    try:
        payload = await request.json()
    except ValueError:
        raise _bad("请求体不是合法的 JSON")
    text, fragments, edits = validate_revise_payload(payload)

    revised = apply_edits(text, edits)

    surviving: list[Fragment] = []
    invalidated: list[int | str] = []
    for fragment in fragments:
        migrated = migrate_fragment(fragment, edits)
        if migrated is None:
            invalidated.append(fragment.id)
        else:
            surviving.append(migrated)

    # 对保留片段复用现有最优选择与遮盖逻辑。
    selected = select_fragments(surviving)
    by_id = {f.id: f for f in surviving}
    chosen = [by_id[fragment_id] for fragment_id in selected]
    surviving_ids = sorted(f.id for f in surviving)

    return ReviseOut(
        revised_text=revised,
        invalidated_ids=sorted(invalidated),
        selected_ids=list(selected),
        rejected_ids=[i for i in surviving_ids if i not in set(selected)],
        masked_text=mask_text(revised, chosen),
    )
