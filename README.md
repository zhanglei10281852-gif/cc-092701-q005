# 职业教学任务运营服务

这是一个面向职业院校教务团队、授课教师和课程管理员的 Python 后端服务，用于管理课程任务模板、学员提交、执行队列、教师工作者、结果版本和运营干预。服务保留登录、角色权限、会话、审计和配额等基础能力，所有业务状态与审计事件写入本地 SQLite 数据库，适合在单个应用容器中离线运行。

## 运行环境

- Python 3.11
- SQLite 3（由 Python 标准库提供）
- FastAPI 与 Uvicorn

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `./data/compute-operations.db`，可复制 `.env.example` 并设置 `TOWNSHIP_DATABASE_PATH` 指向其他本地路径。

## 初始化与启动

```bash
python -m app.cli init-db
python -m app.cli check-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

课程任务运营接口使用 `/api/compute` 前缀，身份、角色、审计和系统接口分别位于 `/api/auth`、`/api/roles`、`/api/audit` 与 `/api/system`。

## 领取调度的公平性规则

领取（`POST /api/compute/tasks/claim`）不再只按原始优先级取队首，而是按以下确定性规则选择：

- **班级权重**：班级由任务的 `project_code` 标识，可在 `/api/compute/class-policies` 配置权重（基准 100）与并发份额；未配置的班级使用 `/api/compute/schedule-config` 中的默认值。
- **有效排序分**：`priority × 班级权重/100 + 等待老化加分`。老化以任务可领取时刻 `available_at` 起算，每 `aging_step_seconds` 秒增加 `aging_bonus_per_step` 分，封顶 `max_age_bonus`。高优先级（含高权重班级）任务在新到达时仍然领先，但等待足够久的任务会逐步提升有效排序。
- **并发份额跳过**：某班级运行中任务数达到其 `max_concurrent`、或班级策略被停用时，该班级候选会被跳过，调度继续扫描后续班级，不会因队首班级暂时无槽位而饿死后面的队列。
- **能力不匹配跳过**：工作者通过 `capabilities` 声明可处理的算法（模板 `algorithm`），不匹配的任务不参与选择，跳过原因单独记录。
- **可审计**：每次领取都会写入 `compute_claim_decisions`（`GET /api/compute/claim-decisions`），保存当时的调度配置快照、班级策略快照、各班运行计数、每个候选的打分明细与跳过原因；返回体附带 `decision_id`、`outcome`（`claimed`/`skipped`/`empty`）。

配置与班级策略变更只影响之后的领取，历史决策保留当时的快照。打分逻辑位于 `app/compute/scheduling.py`，不访问数据库和时钟；配合可注入时钟，在固定时钟与确定任务集下重复执行会得到相同的领取顺序与相同的解释。


## 测试与编译检查

```bash
python -m pytest
python -m compileall -q app tests
```

本地冒烟命令：

```bash
python -m app.cli smoke
python -m app.cli compute-demo
```

## 目录结构

```text
app/compute/       任务模板、配额、提交、领取、回执和人工干预
app/api/            登录、角色、审计和系统管理接口
app/core/           时钟、安全、异常和分页能力
app/repositories/   SQLite 查询与事务封装
app/services/       身份、审计和后台任务服务
app/database.py     SQLite 连接、事务、表结构和权限初始化
tests/              领域、接口、调度和身份回归测试
tools/              本地维护脚本
```

## 数据一致性

SQLite 连接启用外键、WAL 和忙等待策略。提交、领取、回执和人工干预在即时事务中完成；租约、配额与结果版本使用可注入时钟，便于复现跨日和恢复边界。会话令牌只保存摘要，审计与人工干预记录不会写入明文密码或令牌。
