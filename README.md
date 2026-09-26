# 公开档案敏感片段遮盖 API

基于 Python + FastAPI 的无状态服务：提交一段原文和若干候选敏感片段，服务在
**互不重叠**的前提下选出最优片段组合，并用等长的 `█`（U+2588 FULL BLOCK）
遮盖原文。片段之间允许重叠（提交时无需预处理），相邻片段可同时入选。

## 一条命令启动

需要 Docker（含 Compose 插件，Docker Desktop 自带），**无需任何外部账号或密钥**：

```bash
docker compose up --build
```

服务地址与端口：

- API 地址：`http://localhost:8000`
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

测试包含核心算法单元测试、API 端到端测试，以及 300 组随机用例下
动态规划结果与 2^n 暴力枚举的对照校验。

## 项目结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由、请求校验
│   └── redaction.py     # 区间调度 DP 与遮盖算法（与框架解耦）
├── tests/               # pytest 测试
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── requirements-dev.txt
```
