# 公开档案敏感片段遮盖 API

基于 Python + FastAPI 的无状态服务：提交一段原文和若干候选敏感片段，服务在
**互不重叠**的前提下选出最优片段组合，并用等长的 `█`（U+2588 FULL BLOCK）
遮盖原文。片段之间允许重叠（提交时无需预处理），相邻片段可同时入选。

另提供修订接口：提交针对原文的一批编辑（插入/删除/替换），服务一次性应用
编辑、迁移或作废既有片段后，再对保留片段做同样的最优选择与遮盖。

## 一条命令启动

需要 Docker（含 Compose 插件，Docker Desktop 自带），**无需任何外部账号或密钥**：

```bash
docker compose up --build
```

服务地址与端口：

- API 地址：`http://localhost:8000`
- 遮盖接口：`POST http://localhost:8000/redact`
- 修订接口：`POST http://localhost:8000/redact/revise`
- 健康检查：`GET http://localhost:8000/health`
- 交互文档（Swagger UI）：`http://localhost:8000/docs`
- OpenAPI 描述：`http://localhost:8000/openapi.json`

后台运行可加 `-d`，停止使用 `docker compose down`。

## API

### `POST /redact`

请求体（`Content-Type: application/json`）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `text` | string | 待处理原文 |
| `fragments` | array | 候选片段列表，允许重叠 |
| `fragments[].id` | integer 或 string | 片段唯一 ID；一次请求内所有 ID 必须同为整数或同为字符串 |
| `fragments[].weight` | integer | 权重，**正整数**（`>= 1`） |
| `fragments[].start` | integer | 起始位置，**左闭**，按 Unicode 码点计数，从 0 开始 |
| `fragments[].end` | integer | 结束位置，**右开**；区间为 `[start, end)` |

返回体：

| 字段 | 说明 |
| --- | --- |
| `selected_ids` | 入选片段 ID，按升序排列 |
| `rejected_ids` | 落选片段 ID，按升序排列 |
| `masked_text` | 入选片段覆盖的每个码点替换为一个 `█` 后的正文（长度与原文一致） |

#### 选择目标（按优先级）

1. **权重总和最大**；
2. 权重总和并列时，**遮盖字符数（区间长度之和）最多**；
3. 仍并列时，取**入选 ID 升序列表字典序最小**的组合
   （Python 序列字典序：逐位比较，前缀较短者更小）。

相邻区间（如 `[0,3)` 与 `[3,5)`）不重叠，可以同时入选。
算法为带权区间调度动态规划，时间复杂度 O(n log n)。

#### 示例

```bash
curl -s http://localhost:8000/redact \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "你好，世界！",
    "fragments": [
      {"id": 1, "weight": 5, "start": 0, "end": 3},
      {"id": 2, "weight": 3, "start": 0, "end": 2},
      {"id": 3, "weight": 1, "start": 2, "end": 5}
    ]
  }'
```

响应：

```json
{
  "selected_ids": [1],
  "rejected_ids": [2, 3],
  "masked_text": "███世界！"
}
```

片段 1 与片段 2、3 都重叠；片段 2+3 虽能相邻组合且遮盖字符更多，但权重总和
4 < 5，因此入选片段 1。

相邻片段同时入选的例子：

```bash
curl -s http://localhost:8000/redact \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "abcdef",
    "fragments": [
      {"id": 1, "weight": 1, "start": 0, "end": 3},
      {"id": 2, "weight": 1, "start": 3, "end": 6}
    ]
  }'
# {"selected_ids":[1,2],"rejected_ids":[],"masked_text":"██████"}
```

#### 校验规则（不合法请求返回 HTTP 400）

以下情况一律拒绝，响应形如 `{"detail": "中文错误说明"}`：

- 缺少 `text` / `fragments`，或片段缺少 `id` / `weight` / `start` / `end`；
- 类型错误：如权重或位置不是整数（不接受 `1.0`、`true`、`"1"`），ID
  不是整数或字符串；
- 权重不是正整数（`<= 0`）；
- 区间越界（`start < 0` 或 `end > 原文码点长度`）；
- 空片段或反向区间（`start >= end`）；
- 重复 ID；
- 整数 ID 与字符串 ID 混用；
- 请求体不是合法 JSON。

### `POST /redact/revise`

先按编辑修订原文，再对保留片段执行与 `/redact` 相同的最优选择与遮盖。

请求体（`Content-Type: application/json`）：`text` 与 `fragments` 的含义、
校验规则同 `/redact`（片段区间均针对**原**文），另需 `edits` 数组：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `edits` | array | 针对原文的编辑列表，**一次性同时应用**；坐标全部按原文码点定位 |
| `edits[].start` | integer | 起始位置，左闭，`0 <= start <= end` |
| `edits[].end` | integer | 结束位置，右开；`start == end` 表示在该位置**插入** |
| `edits[].replacement` | string | 替换 `[start, end)` 区间的内容；`""` 即删除该区间 |

返回体：

| 字段 | 说明 |
| --- | --- |
| `revised_text` | 应用全部编辑后的原文 |
| `invalidated_ids` | 被编辑波及而**作废**的片段 ID（升序），不参与选择 |
| `selected_ids` | 保留片段中入选的片段 ID（升序） |
| `rejected_ids` | 保留片段中落选的片段 ID（升序） |
| `masked_text` | 修订后原文中入选片段以等长 `█` 遮盖后的正文 |

#### 片段作废与迁移规则

对每个既有片段（按原文坐标判断）：

- 删除/替换区间与片段**有交集**，或插入点**严格落在片段内部**：片段作废，
  进入 `invalidated_ids`；
- 插入在片段**起点**：整段右移，不吞并插入内容；
- 插入在片段**终点**：片段不扩展；
- 其余保留片段按前方所有编辑的**累计码点位移**平移区间
  （如前方删除 2 码点又插入 3 码点，则整体左移 -2+3=右移 1 码点）。

#### 编辑校验规则（不合法请求返回 HTTP 400）

- 缺少 `edits`，或编辑缺少 `start` / `end` / `replacement`；
- 类型错误：位置不是整数（不接受 `1.0`、`true`、`"1"`），`replacement` 不是字符串；
- 区间越界（`start < 0` 或 `end > 原文码点长度`）或逆序（`start > end`）；
- 编辑区间彼此重叠（插入点落在另一编辑区间内部也算重叠；
  插入在另一编辑区间的起点或终点、以及相邻编辑均合法）；
- 同一位置的多次插入。

#### 示例

```bash
curl -s http://localhost:8000/redact/revise \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "你好，世界！",
    "fragments": [
      {"id": 1, "weight": 5, "start": 0, "end": 3},
      {"id": 2, "weight": 4, "start": 3, "end": 5}
    ],
    "edits": [
      {"start": 3, "end": 3, "replacement": "美丽的"},
      {"start": 5, "end": 6, "replacement": "地球"}
    ]
  }'
```

响应：

```json
{
  "revised_text": "你好，美丽的世界地球",
  "invalidated_ids": [],
  "selected_ids": [1, 2],
  "rejected_ids": [],
  "masked_text": "███美丽的██地球"
}
```

插入 `美丽的` 位于片段 1 的终点（不扩展）与片段 2 的起点（整段右移 3 码点
到 `[6, 8)`）；替换 `！`→`地球` 在两个片段之后，不影响区间。两片段迁移后
仍相邻不重叠，同时入选。

作废示例：删除与片段相交的区间，或把内容插入片段内部——

```bash
curl -s http://localhost:8000/redact/revise \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "abcdef",
    "fragments": [
      {"id": 1, "weight": 5, "start": 2, "end": 4},
      {"id": 2, "weight": 3, "start": 5, "end": 6}
    ],
    "edits": [{"start": 3, "end": 5, "replacement": ""}]
  }'
# {"revised_text":"abcf","invalidated_ids":[1],"selected_ids":[2],
#  "rejected_ids":[],"masked_text":"abc█"}
```

删除区间 `[3, 5)` 与片段 1（`[2, 4)`）相交，片段 1 作废；片段 2（`f`）
左移 2 码点到 `[3, 4)`，入选并遮盖。

## 码点计数说明

位置与遮盖均以 **Unicode 码点**为单位（即 Python 3 中 `str` 的索引与
`len()`），不是字节，也不是用户感知字符（grapheme cluster）。例如
`"a😀b"` 长度为 3，遮盖 emoji `[1,2)` 得到 `"a█b"`。

## 本地开发（不使用 Docker）

需要 Python 3.11+：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

uvicorn app.main:app --reload --port 8000   # 启动开发服务器
pytest -q                                    # 运行测试
```

测试包含核心算法单元测试、API 端到端测试，以及随机用例对照校验
（动态规划结果 vs 2^n 暴力枚举；迁移后区间内容 vs 修订后原文切片）。

## 项目结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由、请求校验
│   ├── redaction.py     # 区间调度 DP 与遮盖算法（与框架解耦）
│   └── revision.py      # 编辑应用与片段迁移/作废（与框架解耦）
├── tests/               # pytest 测试
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── requirements-dev.txt
```
