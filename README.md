# League Points Ledger

本地运行的联赛积分账本 API：Python + FastAPI + SQLite。

- **不可变事件**：积分以事件形式追加写入，写后不可改。
- **幂等写入**：每个 `event_id` 只生效一次；相同载荷重放返回原结果（200），相同 `event_id` 不同载荷返回 **409**。
- **更正链**：更正事件通过 `correction_of` 引用已有事件，更正本身可被再次更正（链式）。任一 revision 上，每条更正链只计**当时的末端事件**。
- **递增 revision**：每次成功写入获得严格递增的 revision；可查询当前或任一历史 revision 的排行榜与生效明细。
- **并发安全**：写入经 `BEGIN IMMEDIATE` 串行化，revision 在写锁内分配——并发写入不会产生重复 revision 或中间态。

## 快速启动

```bash
docker compose up --build
```

服务启动后访问 <http://localhost:8000>，交互式文档见 <http://localhost:8000/docs>。

数据持久化在命名卷 `ledger-data` 中（SQLite 文件 `/data/ledger.db`）。

## API

### 写入事件 `POST /events`

```json
{
  "event_id": "match-001-alice",
  "participant_id": "alice",
  "points": 10,
  "correction_of": null
}
```

| 场景 | 状态码 | 说明 |
|---|---|---|
| 首次写入 | `201` | 返回事件及分配的 `revision` |
| 相同 `event_id` + 相同载荷重放 | `200` | 返回原结果，不产生新 revision |
| 相同 `event_id` + 不同载荷 | `409` | 冲突，拒绝写入 |
| `correction_of` 引用不存在的事件 / 自引用 | `422` | 校验失败 |

### 更正事件

```bash
# 原事件
curl -X POST localhost:8000/events -H 'Content-Type: application/json' -d \
  '{"event_id":"e1","participant_id":"alice","points":10}'

# 第一次更正（引用 e1）
curl -X POST localhost:8000/events -H 'Content-Type: application/json' -d \
  '{"event_id":"e2","participant_id":"alice","points":12,"correction_of":"e1"}'

# 再次更正（引用 e2，形成更正链 e1 -> e2 -> e3）
curl -X POST localhost:8000/events -H 'Content-Type: application/json' -d \
  '{"event_id":"e3","participant_id":"alice","points":15,"correction_of":"e2"}'
```

链 `e1 → e2 → e3` 中：revision ≥ e3 时只有 `e3` 计入；在 e3 写入之前、e2 写入之后，只有 `e2` 计入。

### 排行榜 `GET /leaderboard`

```bash
curl localhost:8000/leaderboard                # 当前 revision
curl 'localhost:8000/leaderboard?revision=3'   # 指定历史 revision
```

返回按**总分降序、`participant_id` 升序**排列，并含每个参赛者的生效事件明细：

```json
{
  "revision": 3,
  "entries": [
    {
      "rank": 1,
      "participant_id": "alice",
      "total_points": 15,
      "effective_events": [
        {"event_id": "e3", "points": 15, "revision": 3, "correction_of": "e2"}
      ]
    }
  ]
}
```

查询不存在的 revision（`< 1` 或大于当前最大 revision）返回 `404`。

### 排行榜分页 `GET /leaderboard/page`

面向大榜单的游标分页：游标自包含、无服务器端状态，可并发续读。首次请求固定快照 revision，之后携带 `next_cursor` 续读——即使期间持续有新写入，所有页仍读取游标绑定的同一 revision，不会重复或遗漏。

| 查询参数 | 说明 |
|---|---|
| `revision` | 可选。首次请求固定快照（默认当前 revision）；携带 `cursor` 时若显式给出，必须与游标绑定的 revision 一致，否则 `422` |
| `limit` | 每页条数，`1`–`500`，默认 `50` |
| `cursor` | 上一页响应的 `next_cursor` |

```bash
curl 'localhost:8000/leaderboard/page?limit=2'
curl 'localhost:8000/leaderboard/page?limit=2&cursor=<next_cursor>'
```

```json
{
  "revision": 3,
  "entries": [
    {
      "rank": 1,
      "participant_id": "alice",
      "total_points": 15,
      "effective_events": [
        {"event_id": "e3", "points": 15, "revision": 3, "correction_of": "e2"}
      ]
    }
  ],
  "next_cursor": "eyJ2IjoxLCJyZXYiOjMsInB0cyI6MTUsInBpZCI6ImFsaWNlIn0.<签名>"
}
```

- 排序与 `/leaderboard` 一致（总分降序、`participant_id` 升序），`rank` 跨页连续。
- 末页 `next_cursor` 为 `null`；空榜正常返回 `{"revision": 0, "entries": [], "next_cursor": null}`。
- 游标格式为 `base64url(payload).base64url(HMAC-SHA256)`，payload 含版本号、revision、末条总分与 `participant_id`；篡改、格式错误、版本不支持或与显式 `revision` 冲突均返回 `422`，未知 revision 返回 `404`。

### 名次变化 `GET /leaderboard/changes`

比较两个 revision 快照之间的名次/总分变化。两个查询参数均**必填**：

| 查询参数 | 说明 |
|---|---|
| `from_revision` | 起点 revision，允许传 `0` 表示空榜；须满足 `0 ≤ from_revision` |
| `to_revision` | 终点 revision，须满足 `from_revision < to_revision ≤ 当前 revision` |

- 顺序非法（缺参/非整数/负数、`from_revision ≥ to_revision`）返回 `422`；
- 超出当前 revision 范围返回 `404`；
- 响应仅包含**总分或名次发生变化**的参与者，按 `participant_id` 升序输出；
- `before`/`after` 在对应快照中不存在时为 `null`，存在时含 `rank` 与 `total_points`；`points_delta = after.total_points - before.total_points`（缺席一侧按 0 计）；
- 生效规则与排行榜一致：每个 revision 只计更正链当时的末端事件。若更正把某事件从参赛者 A 改挂到 B，同一响应中会同时出现 A 退出（`after: null`）与 B 进入（`before: null`）。

```bash
# 空榜（revision 0）到当前 revision 的全部变化
curl 'localhost:8000/leaderboard/changes?from_revision=0&to_revision=4'
```

```json
{
  "from_revision": 0,
  "to_revision": 4,
  "changes": [
    {
      "participant_id": "alice",
      "before": null,
      "after": {"rank": 2, "total_points": 12},
      "points_delta": 12
    },
    {
      "participant_id": "bob",
      "before": null,
      "after": {"rank": 3, "total_points": 5},
      "points_delta": 5
    },
    {
      "participant_id": "carol",
      "before": null,
      "after": {"rank": 1, "total_points": 20},
      "points_delta": 20
    }
  ]
}
```

更正把事件从 `alice` 改挂到 `dave`（`from_revision=4&to_revision=5`）时：

```json
{
  "from_revision": 4,
  "to_revision": 5,
  "changes": [
    {
      "participant_id": "alice",
      "before": {"rank": 2, "total_points": 12},
      "after": null,
      "points_delta": -12
    },
    {
      "participant_id": "dave",
      "before": null,
      "after": {"rank": 2, "total_points": 12},
      "points_delta": 12
    }
  ]
}
```

### 其他端点

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 健康检查 |
| `GET` | `/events` | 全部事件（按 revision 升序） |
| `GET` | `/events/{event_id}` | 单个事件 |

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `LEDGER_DB_PATH` | `/data/ledger.db` | SQLite 数据库文件路径 |
| `LEADERBOARD_CURSOR_SECRET` | 内置开发默认值 | `/leaderboard/page` 游标的 HMAC-SHA256 签名密钥。**生产环境务必覆盖为强随机值**（如 `openssl rand -hex 32`）；更换密钥后旧游标全部失效（续读返回 `422`）。 |

`docker-compose.yml` 中通过 `LEADERBOARD_CURSOR_SECRET: ${LEADERBOARD_CURSOR_SECRET:-dev-only-insecure-cursor-secret}` 注入，可在 shell 环境或 `.env` 文件中覆盖；不配置时 `docker compose up --build` 仍可单命令启动。

## 本地开发（不用 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
LEDGER_DB_PATH=./ledger.db uvicorn app.main:app --port 8000
```

## 测试

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -v
```

测试覆盖：幂等重放、409 冲突、更正链末端计数、历史 revision 查询、排行榜排序、50 路并发写入下 revision 唯一且连续，`/leaderboard/page` 游标分页（快照固定、并发续读、篡改/冲突返回 422、末页与空榜），以及 `/leaderboard/changes`（空榜对比、仅名次变化、更正链 A 退出/B 进入、参数顺序 422、超范围 404、按 `participant_id` 排序）。

## 设计要点

- **存储**：单表 `events`（`event_id` 主键，`revision` 唯一），WAL 模式。
- **幂等**：请求载荷序列化为规范化 JSON（键排序）随事件存储；重放时比对，一致返回原结果，不一致返回 409。
- **生效判定**：revision R 上事件 e 生效 ⟺ `e.revision ≤ R` 且不存在 `c.revision ≤ R` 的更正事件 `c.correction_of = e.event_id`。
- **并发**：所有写操作在 `BEGIN IMMEDIATE` 事务内先取写锁、再计算 `MAX(revision)+1` 并插入，配合 `busy_timeout`，写者完全串行；读者走 WAL 快照，互不阻塞。
- **游标分页**：`/leaderboard/page` 的游标经 HMAC-SHA256 签名（密钥来自 `LEADERBOARD_CURSOR_SECRET`），绑定 revision 与末条排序键 `(total_points, participant_id)`；续读始终落在游标指定的 revision 上，配合 WAL 快照读，写入并发进行也不会造成分页漂移。
