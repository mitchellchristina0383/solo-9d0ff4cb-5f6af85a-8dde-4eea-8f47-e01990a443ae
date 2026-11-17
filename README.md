# Dependency Alert API

值班工程师面对一串服务故障告警时，需要先处理真正的**上游根因故障**，同时保留尚未恢复的
下游告警但予以抑制。本服务用 **FastAPI + SQLite（WAL）** 实现：

- 整版提交服务依赖图（`A` 依赖 `B`，则 `B` 是 `A` 的上游）；
- 带**每服务递增正整数序号**上报故障 / 恢复，支持幂等重试、冲突拒绝、迟到不回退；
- 查询时动态计算**需处理故障**与**被抑制故障**及其上游根因；
- 替换依赖图时按服务名保留状态，恢复后下游自动重新归类，无需重报；
- 所有写操作走序列化事务，并发上报 / 换版 / 查询不会混合不同已提交状态。

## 运行

### Docker Compose（推荐）

```bash
docker compose up --build
# API:   http://localhost:8000
# 文档:  http://localhost:8000/docs   （Swagger UI，可直接操作）
# 健康:  http://localhost:8000/health
```

SQLite 数据文件保存在命名卷 `alert-data`（容器内 `/data/alerts.db`）。

### 本地直接运行

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
ALERT_DB_PATH=./data/alerts.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/graph` | 整版替换依赖图。校验失败（环路 / 未知引用 / 重名 / 重边 / 自依赖）返回 `422` 并**保留旧版** |
| `GET` | `/graph` | 读取当前已提交图（含内部修订号 `revision`） |
| `POST` | `/reports` | 上报故障 / 恢复 |
| `GET` | `/alerts` | 查询告警分类（需处理 / 被抑制 / 根因 / 路径，路径支持游标分页） |
| `POST` | `/admin/reset` | 清空全部数据（测试 / 运维辅助，同时作废旧游标） |
| `GET` | `/health` | 健康检查 |

### 提交依赖图

```json
POST /graph
{
  "version": "v1",
  "services": [
    {"name": "A", "dependencies": ["B", "C"]},
    {"name": "B", "dependencies": ["D"]},
    {"name": "C", "dependencies": ["D"]},
    {"name": "D", "dependencies": []}
  ]
}
```

`dependencies` 中列出的是**上游**服务；所有被引用服务必须在同一次提交中出现，
整图必须是 DAG（Kahn 算法检环）。每次成功提交 `revision` 递增。

### 上报故障 / 恢复

```json
POST /reports
{"service": "B", "seq": 7, "status": "fault"}
```

序号 `seq` 为**每个服务各自维护的严格递增正整数**，规则：

| 情况 | 结果 |
| --- | --- |
| `seq > last_seq` | 应用新状态 |
| `seq == last_seq` 且内容相同 | 幂等重试，`applied=false, duplicate=true`，状态不变 |
| `seq == last_seq` 但内容不同（fault/recover 互斥） | `409 conflict`，状态不变 |
| `seq < last_seq`（迟到上报） | `409 conflict`，**绝不回退状态** |
| 服务不在当前图中 | `404 unknown_service` |
| `seq <= 0` | `422`（请求体校验） |

序号允许有空洞（如 1 之后直接 3）。每次上报（含被拒绝的）都写入 `events` 审计表。

### 查询告警

```json
GET /alerts
{
  "graph_revision": 1,
  "graph_version": "v1",
  "services": ["A", "B", "C", "D"],
  "actionable_faults": [{"service": "D", "last_seq": 1}],
  "suppressed_faults": [
    {
      "service": "B",
      "last_seq": 1,
      "root_causes": ["D"],
      "paths": [["B", "D"]]
    }
  ],
  "down_services": ["B", "D"],
  "unreported_services": ["C"],
  "healthy_services": []
}
```

### 路径分页（深链与高分叉）

分类（需处理 / 被抑制 / 根因）始终在一次查询内完整计算且语义不变；可能指数膨胀的
只有被抑制故障的 `paths`。路径计算全部改为**迭代实现**（拓扑序 + 组合数计数 /
反解），千级深度的依赖链不再触发递归深度错误，高分叉图也不会一次性枚举全部路径。

- **无参数 `GET /alerts`（兼容旧行为）**：当全部全故障路径不超过 100 条时，响应与
  旧版完全一致（没有 `pagination` 字段）。超过 100 条时返回**首页**并附带
  `pagination` 块与 `next_cursor`，明确提示结果分页——绝不截断后冒充完整结果。
- **`GET /alerts?limit=N`**：`N` 取值 1–1000，按稳定顺序返回至多 N 条路径（服务按
  名称排序，服务内路径按字典序）。
- **翻页**：把上一页的 `next_cursor` 原样回传
  （`GET /alerts?cursor=<token>`）。游标内固化了页大小，只带游标翻页时仍按原 `limit`
  分页；也可显式传入新的 `limit` 覆盖。
- 游标是 HMAC 签名的不透明令牌，内含 `(graph_revision, state_version, offset, limit)`：
  - 依赖图换版（`revision` 变化）或有**生效的**新上报（`state_version` 变化）后，旧游标
    立即失效，返回 `409 stale_cursor`，要求从无游标查询重新开始，避免拼接不同状态的路径；
  - 同序号幂等重试、被 409/422 拒绝的上报不改变任何版本号，**游标保持有效**；
  - 伪造或损坏的游标返回 `400 invalid_cursor`；`/admin/reset` 会轮换签名密钥，旧令牌
    一律作废。
- 每次请求仍在单个读事务快照内完成，游标只记录偏移，不缓存任何路径；除当前页外不会
  枚举其他页的路径。

分页响应在原有字段之外增加：

```json
{
  "...": "分类字段与旧版相同（本页之外的 suppressed 服务 paths 为空数组）",
  "pagination": {
    "limit": 100,
    "offset": 0,
    "next_offset": 100,
    "returned_paths": 100,
    "total_paths": 4096,
    "path_counts": {"A": 2048, "B": 2048},
    "complete": false,
    "truncated": true,
    "next_cursor": "eyJ...签名令牌",
    "hint": "result is paginated: repeat GET /alerts with cursor=next_cursor ..."
  }
}
```

`total_paths` 是全部全故障路径的总数（高分叉时可以是任意大的整数，仅计数不枚举）；
最后一页 `complete=true`、`next_cursor=null`。

## 抑制规则（核心语义）

1. 只有当前仍处于故障（down）状态的服务才会出现在 `actionable_faults` 或
   `suppressed_faults` 中，两类互斥且并集恰为全部 down 服务。
2. **根因**：故障且没有任何故障上游的服务。根因永远是需处理故障。
3. 一个非根因故障被抑制，当且仅当存在一条到达某根因的依赖路径，且**路径上每个节点都
   处于故障中**。
4. 健康节点或**从未上报**的节点会截断路径：其下游即使故障也不能经由该路径被抑制；
   若所有路径都被截断，该故障仍归入需处理。
5. 分类是查询时根据当前图 + 当前状态**动态计算**的。服务恢复、依赖图换版后立即重新
   归类，不需要下游重新上报。

## 换版与状态保留

- 服务状态（up/down、最后序号）独立于图存储，按服务名为主键。换版后同名服务的状态与
  序号历史原样保留；被移除服务的状态行不删除但不会出现在查询中，将来重新加入时恢复可见。
- 新图中首次出现的服务归入 `unreported_services`。

## 并发模型

- SQLite 开启 WAL；每个写事务以 `BEGIN IMMEDIATE` 开始，写者全局串行化，遇
  `SQLITE_BUSY` 短时重试。
- 读事务持有单一已提交快照，因此换版与查询并发时，每次 `/alerts` 看到的节点集、边与
  修订号必然属于同一个已提交版本，不会出现混合状态。
- 每个请求使用独立的线程级连接，请求结束即关闭。

## 自测

`tests/selftest.py` 仅依赖标准库，对运行中的服务做端到端校验：

```bash
python tests/selftest.py                      # 默认 http://127.0.0.1:8000
BASE_URL=http://localhost:8000 python tests/selftest.py
# 容器内：docker compose exec alert-api python tests/selftest.py
```

覆盖：图校验与原子替换、序号幂等 / 冲突 / 迟到语义、全故障路径抑制与健康 / 未上报截断、
恢复即时重分类、换版状态保留，同序号并发重试、同序号异内容竞争、多序号乱序竞争、
并发换版、换版与查询交错等关键并发场景，以及千级深链、高分叉计数 / 反解、路径分页、
游标失效（换版 / 生效上报失效，幂等重试 / 被拒上报不失效）与伪造游标等场景
（共 94 项断言）。

## 目录结构

```
app/
  main.py            FastAPI 路由与异常映射
  database.py        SQLite 连接、schema、事务管理、状态版本
  graph_service.py   图校验、整版替换、迭代告警分类与分页反解
  cursors.py         HMAC 签名的不透明分页游标
  report_service.py  上报序号语义
  schemas.py         Pydantic 模型
  errors.py          领域错误
tests/selftest.py    端到端自测（标准库）
Dockerfile, docker-compose.yml, requirements.txt
```
