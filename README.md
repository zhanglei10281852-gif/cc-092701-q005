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

## 调度公平性与领取审计

任务按 `project_code` 归属班级。领取（claim）时不再只看队首，而是对全部可领取候选计算有效分并排序：

```text
有效分 = 原始优先级 × 班级权重 + 等待老化加成
等待老化加成 = min(老化上限, 等待秒数 × 老化速率 / 3600)
```

- **班级权重**：`PUT /api/compute/scheduling-policy` 按班级配置权重，高权重班级有效分更高。
- **并发份额**：每个班级可配置 `max_concurrent`，占用达到份额的班级其排队记录会被跳过，不会因为队首班级暂时无槽位而饿死后面的队列；能力不匹配的记录同样被跳过。
- **等待老化**：高优先级仍然领先，但等待足够久的任务会逐步提升有效排序。
- **选择依据**：每次领取（包括未领到任务的领取）都会写入一条 `compute_claim_decisions` 记录，包含策略版本与完整快照、每个候选的分数与跳过原因，可通过 `GET /api/compute/claim-decisions` 与 `GET /api/compute/claim-decisions/{id}` 审计。
- **配置版本化**：策略每次变更生成新的自包含版本，历史版本只读，配置变更只影响后续决策；`GET /api/compute/scheduling-policy/history` 查看版本历史。
- **确定性**：排序键为（有效分降序、优先级降序、创建时间升序、任务 id 升序）的全序，配合可注入时钟，在固定时钟和确定任务集下重复执行得到同样的顺序与可审计解释。

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
