# Sealed Predictions — 封存并按时公开预测的 Commit-Reveal 服务

基于 **FastAPI + PostgreSQL** 的预测封存服务：先在截止前提交预测的承诺哈希（SHA-256），
进入揭示期后再公开原文与随机盐，服务端重算哈希校验，**仅首次匹配的揭示生效**，
并提供可核验的事件时间线。所有截止时间均按**服务端 UTC** 判定。

## 快速启动（一条命令）

```bash
docker compose up --build
```

- API 地址：**http://localhost:8000**
- 交互式文档（Swagger UI）：**http://localhost:8000/docs**
- 健康检查：`GET http://localhost:8000/health`

依赖安装与建表均在启动时自动完成：Compose 启动 Postgres 16 与 API 两个容器，
API 等待数据库健康检查后启动，并在启动时自动创建数据表（`events` / `commitments` /
`reveals` / `deadline_extensions`）。
两个容器均带 healthcheck，`docker compose ps` 中状态变为 `healthy` 即就绪。

## 承诺哈希方案

```
commitment = SHA-256( 原文字节长度的 8 字节大端整数 || 原文 UTF-8 字节 || 盐 UTF-8 字节 )
```

8 字节大端长度前缀用于消除「原文 + 盐」拼接歧义。可用服务端工具端点核对本地实现：

```bash
curl -s -X POST http://localhost:8000/utils/commitment \
  -H 'Content-Type: application/json' \
  -d '{"original_text": "明天下雨", "salt": "my-random-salt"}'
# {"commitment_hash": "...", "hash_scheme": "sha256(u64be(len(utf8(text))) || utf8(text) || utf8(salt))"}
```

## 事件生命周期（服务端 UTC 判定）

| 阶段 | 时间区间 | 允许操作 |
|------|----------|----------|
| `commit` | 创建时刻 ≤ now < commit_deadline | 提交承诺 |
| `reveal` | commit_deadline ≤ now < reveal_deadline | 提交揭示 |
| `closed` | now ≥ reveal_deadline | 只读 |

边界语义：到达 commit_deadline 的瞬间提交通道关闭、揭示通道开启；到达 reveal_deadline 揭示通道关闭。

## API 一览

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/events` | 创建事件（commit_deadline 须在未来，reveal_deadline 须更晚） |
| GET | `/events` | 事件列表（含状态与计数） |
| GET | `/events/{id}` | 事件详情 |
| GET | `/events/{id}/snapshot?at=...` | 按指定带时区历史时刻重建事件视图（回放延长与计数） |
| POST | `/events/{id}/extend` | 受约束地延长两个截止时间（仅 commit 阶段，幂等） |
| POST | `/events/conflicts` | 批量事件的阶段窗口冲突分析（sweep-line） |
| POST | `/events/schedule` | 批量事件的阶段窗口资源排程（最少轨道确定性着色） |
| POST | `/events/{id}/commitments` | 提交承诺（64 位十六进制哈希） |
| POST | `/events/{id}/commitments/batch` | 原子批量提交 1～100 个承诺（单事务，冲突整体回滚） |
| GET | `/events/{id}/commitments` | 承诺列表（含是否已揭示） |
| POST | `/events/{id}/reveals` | 揭示：提交原文 + 盐，服务端重算校验 |
| POST | `/events/{id}/reveals/batch` | 原子批量揭示 1～100 组原文与盐（单事务，冲突整体回滚） |
| GET | `/events/{id}/timeline` | 可核验的事件时间线 |
| POST | `/utils/commitment` | 按约定方案计算承诺哈希 |
| GET | `/health` | 健康检查 |

## 完整使用示例

```bash
# 1. 创建事件（截止时间须携带时区）
curl -s -X POST http://localhost:8000/events \
  -H 'Content-Type: application/json' \
  -d '{"title": "我的预测", "commit_deadline": "2030-01-01T00:00:00Z", "reveal_deadline": "2030-01-02T00:00:00Z"}'
# => {"id": "<EVENT_ID>", "status": "commit", ...}

# 2. 本地计算承诺（Python 参考实现）
python3 - <<'EOF'
import hashlib
text, salt = "明天下雨", "my-random-salt"
tb = text.encode("utf-8")
print(hashlib.sha256(len(tb).to_bytes(8, "big") + tb + salt.encode()).hexdigest())
EOF

# 3. 提交截止前提交承诺
curl -s -X POST http://localhost:8000/events/<EVENT_ID>/commitments \
  -H 'Content-Type: application/json' \
  -d '{"commitment_hash": "<64位十六进制哈希>"}'

# 4. 进入揭示期后提交原文与盐
curl -s -X POST http://localhost:8000/events/<EVENT_ID>/reveals \
  -H 'Content-Type: application/json' \
  -d '{"original_text": "明天下雨", "salt": "my-random-salt"}'
# => {"verified": true, "recomputed_hash": "...", "commitment_hash": "...", ...}

# 5. 获取可核验时间线（含原文与盐，任何人可重算哈希比对）
curl -s http://localhost:8000/events/<EVENT_ID>/timeline
```

## 受约束的截止时间延长（`POST /events/{id}/extend`）

仅在 **commit 阶段**可由调用方延长事件窗口。请求体携带新的两个截止时间（**必须带
时区**）与 `reason`，服务端在**单个事务**中完成全部校验与更新：

- 仅允许在当前 commit 阶段调用；事件进入 reveal / closed 阶段返回
  `409 EXTENSION_NOT_IN_COMMIT_PHASE`；
- 新 `commit_deadline` 必须**严格晚于**当前 commit 截止时间；
- 新 `reveal_deadline` 必须**严格晚于**新 `commit_deadline`，且不得早于当前 reveal
  截止时间 —— **两个截止时间只能延长，不能缩短，也不能保持不变**；
- 任何校验失败均回滚事务，**不改变截止时间**，也不写入任何记录；
- 响应同时返回旧值、新值与 `reason`；每次成功延长都会写入审计表
  `deadline_extensions`，并作为 `deadline_extended` 条目出现在事件时间线中
  （时间线里的 `event_created` 始终展示创建时的原始截止时间）。

```bash
curl -s -X POST http://localhost:8000/events/<EVENT_ID>/extend \
  -H 'Content-Type: application/json' \
  -d '{"commit_deadline": "2030-01-01T06:00:00Z",
       "reveal_deadline": "2030-01-02T06:00:00Z",
       "reason": "数据源延迟，顺延提交与揭示窗口"}'
```

```json
{
  "event_id": "<EVENT_ID>",
  "old_commit_deadline": "2030-01-01T00:00:00Z",
  "old_reveal_deadline": "2030-01-02T00:00:00Z",
  "new_commit_deadline": "2030-01-01T06:00:00Z",
  "new_reveal_deadline": "2030-01-02T06:00:00Z",
  "reason": "数据源延迟，顺延提交与揭示窗口",
  "extended_at": "2030-01-01T00:05:00Z"
}
```

**幂等重放**：对**最近一次成功延长**提交完全相同的请求（新 `commit_deadline`、新
`reveal_deadline` 与 `reason` 三者均一致，按 UTC 时刻比较），无论间隔多久、服务端时钟
是否已推进，都直接返回首次延长的同一结果，不重复写入。仅时间相同而 `reason` 不同不算
重放，按普通请求校验（若当前值已未再延长，则收到 409，状态不变）。

```bash
# 与上一次成功延长完全一致 -> 200，返回与首次完全相同的结果（幂等）
curl -s -X POST http://localhost:8000/events/<EVENT_ID>/extend \
  -H 'Content-Type: application/json' \
  -d '{"commit_deadline": "2030-01-01T06:00:00Z",
       "reveal_deadline": "2030-01-02T06:00:00Z",
       "reason": "数据源延迟，顺延提交与揭示窗口"}'
```

延长后可基于新窗口继续延长（连续延长时，旧值为上一次延长后的新值，链条完整记录在
审计表与时间线中）。失败语义：

| HTTP | code | 场景 |
|------|------|------|
| 404 | `EVENT_NOT_FOUND` | 事件不存在 |
| 409 | `EXTENSION_NOT_IN_COMMIT_PHASE` | 事件已进入 reveal / closed 阶段 |
| 409 | `INVALID_EXTENSION` | 新 commit 未晚于当前值，或新 reveal 未晚于新 commit / 当前 reveal（只延不缩） |
| 422 | — | 时间格式非法、时间无时区、`reason` 缺失或为空、事件 ID 非 UUID |

## 历史时刻事件快照（`GET /events/{id}/snapshot?at=...`）

按调用方指定的**带时区历史时刻**重建事件在当时的只读视图，与服务端当前时钟无关，
也不会修改任何数据。典型用途：审计「某个截止时间延长生效之前」事件处于什么阶段、
当时已有多少承诺与揭示。

```bash
curl -s 'http://localhost:8000/events/<EVENT_ID>/snapshot?at=2030-01-01T00:30:00Z'
```

```json
{
  "event_id": "<EVENT_ID>",
  "title": "我的预测",
  "description": null,
  "created_at": "2030-01-01T00:00:00Z",
  "snapshot_at": "2030-01-01T00:30:00Z",
  "commit_deadline": "2030-01-01T06:00:00Z",
  "reveal_deadline": "2030-01-02T06:00:00Z",
  "status": "commit",
  "commitment_count": 2,
  "reveal_count": 0
}
```

**回放规则**（所有比较按 UTC 时刻，边界均**含 `at` 本身**）：

- 以事件创建记录为起点，按发生时间 `(created_at, id)` 升序回放
  `deadline_extensions`：仅 `created_at <= at` 的延长在该时刻已生效，
  **最近一条**已生效延长的新截止时间即当时生效的 `commit_deadline` /
  `reveal_deadline`；若首次延长尚未发生，则使用创建时的原始截止时间
  （即首条延长审计记录的旧值）；
- `status` 用**回放后**的截止时间按常规半开语义判定：`at < commit_deadline`
  为 `commit`，`at < reveal_deadline` 为 `reveal`，其后为 `closed`；
- `commitment_count` / `reveal_count` 只统计 `created_at <= at` 的记录
  （该时刻之后才写入的承诺/揭示不计入）；
- 响应回显 `created_at` 与归一化到 UTC 的 `snapshot_at`。

`at` 接受任意携带时区偏移的 ISO 8601 时间（`Z` 后缀与 `+08:00` 这类偏移均可，
按同一 UTC 时刻处理）。允许取**未来**时刻：返回截至查询时已知的最新截止时间、
全部已写入计数与按该时刻判定的状态。

| HTTP | code | 场景 |
|------|------|------|
| 200 | — | 快照重建成功 |
| 400 | `MISSING_SNAPSHOT_TIME` | 缺少查询参数 `at` |
| 400 | `INVALID_SNAPSHOT_TIME` | `at` 为空或无法解析为 ISO 8601 时间 |
| 400 | `NAIVE_SNAPSHOT_TIME` | `at` 未携带时区偏移（如 `2030-01-01T00:30:00`） |
| 400 | `SNAPSHOT_BEFORE_CREATION` | `at` 早于事件创建时间（事件在该时刻尚不存在） |
| 404 | `EVENT_NOT_FOUND` | 事件不存在 |
| 422 | — | 事件 ID 非 UUID |

## 事件窗口冲突分析（`POST /events/conflicts`）

把每个事件按**服务端 UTC** 视为两段**半开**时间区间，用 sweep-line 计算任意时刻
有哪些事件同时处于**同一阶段**（同为 `commit` 或同为 `reveal`，跨阶段不算冲突）：

```
commit: [created_at, commit_deadline)
reveal: [commit_deadline, reveal_deadline)
```

半开边界语义：到达 `commit_deadline` 的瞬间旧事件已离开 commit 阶段、新事件可恰好
进入——首尾相接（一个区间的右端点等于另一个的左端点）**不产生**冲突。返回的冲突片段
按 `start`、`end`、`phase` 排序，片段内 `event_ids` 升序；无冲突（或请求为空）时
稳定返回空列表。

```bash
curl -s -X POST http://localhost:8000/events/conflicts \
  -H 'Content-Type: application/json' \
  -d '{"event_ids": ["<EVENT_ID_1>", "<EVENT_ID_2>"]}'
```

```json
{
  "generated_at": "2030-06-01T00:00:00Z",
  "phases": ["commit", "reveal"],
  "count": 2,
  "segments": [
    {
      "start": "2030-01-01T01:00:00Z",
      "end": "2030-01-01T02:00:00Z",
      "phase": "commit",
      "event_ids": ["<EVENT_ID_1>", "<EVENT_ID_2>"]
    },
    {
      "start": "2030-01-02T01:00:00Z",
      "end": "2030-01-02T02:00:00Z",
      "phase": "reveal",
      "event_ids": ["<EVENT_ID_1>", "<EVENT_ID_2>"]
    }
  ]
}
```

空结果同样稳定：`POST /events/conflicts -d '{"event_ids": []}'`（或省略字段）返回
`{"generated_at": "...", "phases": ["commit", "reveal"], "count": 0, "segments": []}`。
请求中的重复 ID 自动去重；任一 ID 不存在返回 `404 EVENT_NOT_FOUND`；非 UUID 返回 422。

## 事件窗口资源排程（`POST /events/schedule`）

为若干事件的两阶段半开窗口安排 **track（轨道）**：同一 track、同一阶段的两个窗口
不得重叠；commit 与 reveal **分别独立着色**（两套轨道编号互不影响，均从 1 开始）。
窗口定义与半开语义同冲突分析：

```
commit: [created_at, commit_deadline)
reveal: [commit_deadline, reveal_deadline)
```

首尾相接（前一窗口右端点恰为后一窗口左端点）不重叠，**可复用同一 track**。排程按
`(起点, 终点, event_id)` 升序做确定性贪心着色（结束窗口先离场、再取编号最小的空闲
track），所用 track 数等于该阶段的最大并发深度，即**最小所需轨道数**；响应中的
`required_tracks` 取 commit / reveal 两阶段的较大值。请求体携带去重后的事件 ID 与
可用 track 数 `track_limit`（>= 1 的整数）：

```bash
curl -s -X POST http://localhost:8000/events/schedule \
  -H 'Content-Type: application/json' \
  -d '{"event_ids": ["<EVENT_ID_1>", "<EVENT_ID_2>", "<EVENT_ID_3>"],
       "track_limit": 2}'
```

```json
{
  "track_limit": 2,
  "required_tracks": 3,
  "feasible": false,
  "assignments": [
    {"event_id": "<EVENT_ID_1>", "commit_track": 1, "reveal_track": 1},
    {"event_id": "<EVENT_ID_2>", "commit_track": 2, "reveal_track": 2},
    {"event_id": "<EVENT_ID_3>", "commit_track": 3, "reveal_track": 3}
  ],
  "conflicts": [
    {"phase": "commit", "event_id_1": "<EVENT_ID_1>", "event_id_2": "<EVENT_ID_2>"},
    {"phase": "commit", "event_id_1": "<EVENT_ID_1>", "event_id_2": "<EVENT_ID_3>"},
    {"phase": "commit", "event_id_1": "<EVENT_ID_2>", "event_id_2": "<EVENT_ID_3>"},
    {"phase": "reveal", "event_id_1": "<EVENT_ID_1>", "event_id_2": "<EVENT_ID_2>"},
    {"phase": "reveal", "event_id_1": "<EVENT_ID_1>", "event_id_2": "<EVENT_ID_3>"},
    {"phase": "reveal", "event_id_1": "<EVENT_ID_2>", "event_id_2": "<EVENT_ID_3>"}
  ]
}
```

字段与语义：

- `required_tracks`：排下所有窗口所需的最小 track 数（两阶段最大并发深度的较大值）；
- `feasible`：`required_tracks <= track_limit` 时为 `true`，HTTP 状态码始终为 200；
- `assignments`：按 `event_id` 升序返回每个事件的 `commit_track` / `reveal_track`
  （1 起编号；零长度阶段窗口不占任何时刻，对应编号为 `null`）；
- `conflicts`：`feasible=false` 时给出导致超出上限的冲突事件对——即某阶段并发窗口数
  **严格多于** `track_limit` 的时间片段内同时活动的每一对事件（同阶段、半开重叠；
  跨片段去重，按 `phase`、`event_id_1`、`event_id_2` 排序，对内 ID 升序）；
  `feasible=true` 时恒为空数组。注意：仅普通两两重叠、但从未超过上限的事件对
  **不在**此列（上限内排得下即不算阻塞冲突）。

确定性与只读：响应只取决于各事件当前的窗口与 `track_limit`，不含任何时间戳字段，
**相同输入重复请求（含打乱/重复 ID 的等价输入）结果逐字节一致**；排程不写入数据库，
不修改任何事件数据。

| HTTP | code | 场景 |
|------|------|------|
| 200 | — | 排程成功（`feasible=false` 同样返回 200，冲突见 `conflicts`） |
| 400 | `INVALID_TRACK_LIMIT` | `track_limit` 缺失、为 null、不是整数（含布尔值）或小于 1 |
| 404 | `EVENT_NOT_FOUND` | 请求中的任一事件 ID 不存在 |
| 422 | — | 事件 ID 非 UUID 或请求体结构错误 |

## 原子批量提交承诺（`POST /events/{id}/commitments/batch`）

在**单个数据库事务**中按输入顺序提交 1～100 个承诺哈希。服务端在写入前完成全部校验，
**任何一项冲突都整体回滚**，不会留下部分承诺：

- 逐项校验哈希格式（64 位十六进制，大小写均可，存储时归一化为小写）；
- 检查请求内重复（第二次及以后出现的下标记为请求内重复，大小写不敏感）；
- 一次性查询数据库中该事件已有的哈希。

冲突时返回 `409 BATCH_COMMITMENT_CONFLICT`，统一错误结构内附 `conflicts` 数组，
每项给出 `index`（从 0 开始的输入下标）、`type` 与提交时的原始哈希，按下标升序、
每个下标至多一项：

| 冲突 type | 含义 |
|-----------|------|
| `INVALID_FORMAT` | 不是 64 位十六进制哈希 |
| `DUPLICATE_IN_REQUEST` | 同一哈希在本次请求中重复（首次出现之后的下标） |
| `DUPLICATE_COMMITMENT` | 数据库中该事件已存在此哈希（请求内首次出现的下标） |

```bash
# 成功：按输入顺序创建，返回 count 与承诺记录（201）
curl -s -X POST http://localhost:8000/events/<EVENT_ID>/commitments/batch \
  -H 'Content-Type: application/json' \
  -d '{"commitment_hashes": [
        "9f2c7fd25e1b3afad3e85a0bd17d9b100db4b3bd306a4f6e5535e23e15e82d7d",
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
      ]}'
# => {"count": 2, "commitments": [{...}, {...}]}

# 冲突：整体回滚（409），逐项指出冲突下标与类型
curl -s -X POST http://localhost:8000/events/<EVENT_ID>/commitments/batch \
  -H 'Content-Type: application/json' \
  -d '{"commitment_hashes": [
        "9f2c7fd25e1b3afad3e85a0bd17d9b100db4b3bd306a4f6e5535e23e15e82d7d",
        "not-a-valid-hash",
        "9f2c7fd25e1b3afad3e85a0bd17d9b100db4b3bd306a4f6e5535e23e15e82d7d"
      ]}'
```

```json
{
  "detail": {
    "code": "BATCH_COMMITMENT_CONFLICT",
    "message": "批量提交包含 2 个冲突项，已整体回滚，未创建任何承诺",
    "conflicts": [
      {"index": 1, "type": "INVALID_FORMAT", "commitment_hash": "not-a-valid-hash"},
      {"index": 2, "type": "DUPLICATE_IN_REQUEST",
       "commitment_hash": "9f2c7fd25e1b3afad3e85a0bd17d9b100db4b3bd306a4f6e5535e23e15e82d7d"}
    ]
  }
}
```

列表为空或超过 100 项返回 422；事件不存在返回 `404 EVENT_NOT_FOUND`；
已过 `commit_deadline` 与单个提交端点一致返回 `409 COMMIT_PHASE_CLOSED`，
以上情形同样不产生任何写入。

## 原子批量揭示（`POST /events/{id}/reveals/batch`）

进入揭示期后，可在**单个数据库事务**中按输入顺序提交 1～100 组「原文 + 盐」。
服务端按约定方案逐项重算哈希并与该事件的承诺匹配，**所有项目先完成全部校验，
任何一项冲突都整体回滚**，不会留下部分揭示：

- 逐项校验格式（盐须为 1～256 字符的字符串）；
- 按重算哈希检查请求内重复（第二次及以后出现的下标记为请求内重复）；
- 一次性查询命中的承诺（并对这些承诺加行锁，串行化并发批量揭示），判定
  「无匹配承诺」与「承诺已揭示」；
- 仅在揭示期内可调用：提前揭示返回 `409 REVEAL_PHASE_NOT_STARTED`，
  逾期揭示返回 `409 REVEAL_PHASE_CLOSED`（与单个揭示端点一致）。

冲突时返回 `409 BATCH_REVEAL_CONFLICT`，统一错误结构内附 `conflicts` 数组，
每项给出 `index`（从 0 开始的输入下标）与 `type`，按下标升序、每个下标至多
一项；除格式非法项外均附带 `recomputed_hash`，每项都回显提交时的原文与盐：

| 冲突 type | 含义 |
|-----------|------|
| `INVALID_FORMAT` | 盐为空或超过 256 字符（原文允许任意 UTF-8 文本，含空串） |
| `DUPLICATE_IN_REQUEST` | 重算哈希在本次请求中重复（首次出现之后的下标） |
| `NO_MATCHING_COMMITMENT` | 重算哈希不匹配本事件中的任何承诺 |
| `DUPLICATE_REVEAL` | 匹配到的承诺此前已被揭示（请求内首次出现的下标） |

```bash
# 成功：揭示期内按输入顺序一次事务写入，返回 count 与揭示记录（201）
curl -s -X POST http://localhost:8000/events/<EVENT_ID>/reveals/batch \
  -H 'Content-Type: application/json' \
  -d '{"reveals": [
        {"original_text": "明天下雨", "salt": "my-random-salt-1"},
        {"original_text": "明年晴天", "salt": "my-random-salt-2"}
      ]}'
# => {"count": 2, "reveals": [{...verified: true...}, {...}]}

# 冲突：整体回滚（409），逐项指出冲突下标与类型
curl -s -X POST http://localhost:8000/events/<EVENT_ID>/reveals/batch \
  -H 'Content-Type: application/json' \
  -d '{"reveals": [
        {"original_text": "明天下雨", "salt": "my-random-salt-1"},
        {"original_text": "错误原文", "salt": "wrong-salt"},
        {"original_text": "明天下雨", "salt": "my-random-salt-1"}
      ]}'
```

```json
{
  "detail": {
    "code": "BATCH_REVEAL_CONFLICT",
    "message": "批量揭示包含 3 个冲突项，已整体回滚，未创建任何揭示",
    "conflicts": [
      {"index": 0, "type": "DUPLICATE_REVEAL",
       "original_text": "明天下雨", "salt": "my-random-salt-1",
       "recomputed_hash": "…"},
      {"index": 1, "type": "NO_MATCHING_COMMITMENT",
       "original_text": "错误原文", "salt": "wrong-salt",
       "recomputed_hash": "…"},
      {"index": 2, "type": "DUPLICATE_IN_REQUEST",
       "original_text": "明天下雨", "salt": "my-random-salt-1",
       "recomputed_hash": "…"}
    ]
  }
}
```

列表为空或超过 100 项、请求体结构错误（如项不是对象、缺少 `original_text`
或 `salt`、字段类型错误）返回 422；事件不存在返回 `404 EVENT_NOT_FOUND`，
以上情形均不产生任何写入。批量端点与单个揭示端点共享同一套存储与唯一约束：
**并发下仅首次揭示生效**——预检阶段对命中的承诺加行锁（PostgreSQL），
提交阶段再由 `reveals.commitment_id` 唯一约束兜底，竞争落败的整批请求回滚并
收到逐项 `DUPLICATE_REVEAL` 清单。

## 错误响应

错误统一返回 `{"detail": {"code": "...", "message": "..."}}`，且**均不改变任何状态**
（所有校验先于写入，并发冲突由数据库唯一约束兜底并回滚）：

| HTTP | code | 场景 |
|------|------|------|
| 400 | `INVALID_DEADLINES` | 截止时间顺序非法或 commit_deadline 不在未来 |
| 400 | `INVALID_TRACK_LIMIT` | 资源排程的 track_limit 缺失、非整数（含布尔值）或小于 1 |
| 400 | `MISSING_SNAPSHOT_TIME` / `INVALID_SNAPSHOT_TIME` / `NAIVE_SNAPSHOT_TIME` | 历史快照缺少 `at`、时间无法解析或未携带时区 |
| 400 | `SNAPSHOT_BEFORE_CREATION` | 快照时刻早于事件创建时间 |
| 404 | `EVENT_NOT_FOUND` | 事件不存在（含冲突分析/资源排程请求中的任一 ID） |
| 409 | `EXTENSION_NOT_IN_COMMIT_PHASE` | 延长截止时间时事件已离开 commit 阶段（reveal / closed） |
| 409 | `INVALID_EXTENSION` | 延长请求未满足「只延不缩」或 reveal 须晚于新 commit 的约束 |
| 409 | `COMMIT_PHASE_CLOSED` | 迟到提交（已过 commit_deadline） |
| 409 | `DUPLICATE_COMMITMENT` | 同一事件内重复的承诺哈希（单个提交端点） |
| 409 | `BATCH_COMMITMENT_CONFLICT` | 批量提交存在逐项冲突（`detail.conflicts` 给出每个冲突项的 index/type/hash） |
| 409 | `REVEAL_PHASE_NOT_STARTED` | 提前揭示（未到 commit_deadline） |
| 409 | `REVEAL_PHASE_CLOSED` | 逾期揭示（已过 reveal_deadline） |
| 409 | `NO_MATCHING_COMMITMENT` | 原文 + 盐重算后不匹配任何承诺 |
| 409 | `DUPLICATE_REVEAL` | 该承诺已被揭示（仅首次匹配生效） |
| 409 | `BATCH_REVEAL_CONFLICT` | 批量揭示存在逐项冲突（`detail.conflicts` 给出每个冲突项的 index/type 与原文、盐、重算哈希） |
| 422 | — | 请求体格式校验失败（如哈希非 64 位十六进制、截止时间无时区、延长缺少 reason） |

「仅首次匹配的揭示生效」由 `reveals.commitment_id` 唯一约束保证：并发揭示同一承诺时，
后到者事务回滚并收到 `DUPLICATE_REVEAL`。

## 本地开发与测试

### Docker Compose 启动（推荐）

前置条件：已安装 Docker 与 Docker Compose Plugin（`docker compose version` 可用）。

```bash
docker compose up --build       # 前台启动（加 -d 后台运行）
docker compose ps               # 查看两个容器健康状态（db / api 均为 healthy 即就绪）
docker compose logs -f api      # 跟踪 API 日志（含「database schema ready」建表日志）
docker compose down             # 停止并删除容器（保留数据卷）
docker compose down -v          # 停止并连同 Postgres 数据卷 pgdata 一起删除（彻底重置）
```

Compose 编排说明（见 `docker-compose.yml`）：

- **db**：PostgreSQL 16（`postgres:16-alpine`），库名/用户名/密码均为
  `postgres` / `postgres` / `predictions`，数据持久化在命名卷 `pgdata` 中，
  宿主端口 `5432`；自带 `pg_isready` healthcheck。
- **api**：由仓库 `Dockerfile` 构建（`python:3.12-slim` + `requirements.txt`，
  启动命令为 `uvicorn app.main:app --host 0.0.0.0 --port 8000`），
  通过 `depends_on: condition: service_healthy` 等待数据库健康后再启动，
  启动时自动建表（`events` / `commitments` / `reveals` /
  `deadline_extensions`），宿主端口 `8000`；自带 `/health` healthcheck。
- API 通过环境变量 `DATABASE_URL=postgresql+psycopg2://postgres:postgres@db:5432/predictions`
  连接数据库；如本机 5432 或 8000 端口被占用，可在 `docker-compose.yml`
  中修改 `ports` 映射（容器内端口不变）。

启动后即可访问：

- API 地址：**http://localhost:8000**
- 交互式文档（Swagger UI）：**http://localhost:8000/docs**
- 健康检查：`curl http://localhost:8000/health` → `{"status":"ok"}`

### 不使用 Docker（本地直跑）

```bash
pip install -r requirements-dev.txt
DATABASE_URL=sqlite:///./dev.db uvicorn app.main:app --reload   # 本地运行（默认连 PostgreSQL）
python3 -m pytest tests/ -q                                     # 运行测试（SQLite + 模拟时钟）
```

测试使用临时 SQLite 文件库并猴子补丁服务端时钟，无需启动 Postgres；
生产/Compose 环境的行锁（`SELECT … FOR UPDATE`）在 SQLite 上自动降级为普通
查询，并发正确性仍由唯一约束保证。

## 项目结构

```
app/
  main.py       # 路由、生命周期、时间判定与错误处理
  clock.py      # 统一时钟（阶段判定与记录 created_at 的唯一时间来源，可整体替换）
  conflicts.py  # 阶段窗口 sweep-line 冲突分析（纯函数，可独立单测）
  scheduling.py # 阶段窗口资源排程：最少轨道的确定性区间着色（纯函数，可独立单测）
  models.py     # SQLAlchemy 模型（events / commitments / reveals / deadline_extensions）
  schemas.py    # Pydantic 请求/响应模型
  hashing.py    # 承诺哈希方案实现
  database.py   # 引擎与会话（DATABASE_URL 可配置）
tests/test_api.py             # 业务规则冒烟测试（SQLite + 模拟时钟）
tests/test_snapshot.py        # 历史时刻快照：延长回放、阶段与计数重放、400/404 错误
tests/test_conflicts.py       # 冲突分析：sweep-line 边界、排序与 API 用例
tests/test_schedule.py        # 资源排程：最少轨道着色、半开复用、冲突对与确定性
tests/test_extend.py          # 截止时间延长：只延不缩、阶段限制、幂等与时间线
tests/test_batch_commitments.py  # 批量承诺：逐项冲突、整体回滚与并发兜底
tests/test_batch_reveals.py   # 批量揭示：四类逐项冲突、整体回滚与并发仅首次生效
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```
