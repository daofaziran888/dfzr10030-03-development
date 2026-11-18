# 多源数据采集与RPA自动化平台

面向网页抓取、API接入、增量采集、RPA流程编排、数据校验与采集监控的数据工程平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 数据源配置 | `datasource` | 数据源 | 源端编号、连接类型、接入地址 |
| 网页采集规则 | `crawler` | 采集规则 | 规则编号、目标网址、页面选择器 |
| API接入管理 | `apiclient` | API客户端 | 客户端编号、接口名称、请求方法 |
| 增量采集 | `incremental` | 增量游标 | 游标编号、来源标识、增量字段 |
| RPA任务编排 | `robot_task` | RPA任务 | 任务编号、任务名称、页面定位器 |
| 数据接入通道 | `ingestion` | 接入通道 | 通道编号、通道名称、输入格式 |
| 格式解析器 | `parser` | 解析配置 | 解析编号、输入格式、输出格式 |
| 采集结果校验 | `validator` | 校验规则 | 规则编号、校验字段、校验类型 |
| 采集去重 | `dedup` | 去重策略 | 策略编号、输入来源、去重键 |
| 采集调度 | `schedule` | 调度计划 | 计划编号、关联任务、调度周期 |
| 采集监控 | `monitor` | 监控面板 | 面板编号、采集源总数、今日采集量 |
| 采集日志 | `log_viewer` | 日志条目 | 日志编号、关联任务、日志级别 |
| 凭证管理 | `auth` | 认证凭证 | 凭证编号、凭证类型、关联数据源 |
| 采集转换管道 | `transform` | 转换节点 | 节点编号、输入来源、输出目标 |
| 数据流转编排 | `flow` | 流转管道 | 管道编号、管道名称、起始节点 |
| 数据血缘追踪 | `lineage` | 血缘关系 | 关系编号、上游节点、下游节点 |
| 采集报告 | `report` | 采集报告 | 报告编号、采集源、统计周期 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 采集规则发布快照

网页采集规则页面上编辑的是**草稿**；生产采集只读取 `crawler_snapshot` 里
**已测试通过的不可变快照**。同一份快照把目标网址、页面选择器、正文选择器、
翻页参数、规则版本和选择器测试结果绑在一起，每次采集只引用其中一个版本。

快照流转：

```text
草稿 ──选择器测试通过──> 待发布 ──显式确认（旧快照无运行任务）──> 已发布 ──停用──> 已停用
                            └──────旧快照仍有运行任务──────> 待生效 ──旧任务结束自动切换──> 已发布
任意阶段 ──归档──> 已归档（终态，不能回到运行阶段）
```

- `POST /api/crawler/parse`：解析目标网址、页面选择器与翻页参数，返回结构化结果与校验错误。
- `POST /api/crawler/{id}/tests`：提交选择器测试，测试记录与规则版本、选择器、翻页参数同版。
- `GET /api/crawler/snapshots?rule_id=`：查看草稿、待发布、待生效与已发布快照。
- `POST /api/crawler/snapshots/{id}/actions`：`确认发布`、`归档`、`停用`、`重算摘要`。
  同一快照并发确认只有一次成功，其余因状态已变化被拒绝。
- `GET /api/crawler/{id}/effective`：生产采集当前唯一读取的版本。
- `POST /api/crawler/runs`：启动采集运行，只能引用已发布且测试通过的快照；
  `POST /api/crawler/runs/{id}/complete` 结束后会把待生效快照自动推进为已发布。
- `POST /api/monitor/sync`：监控分析服务消费发布快照，按版本汇总抓取条数与选择器错误，
  把差异回写采集规则，并写入 `GET /api/monitor/todos` 的待办工作台。

回归测试：`cd backend && .venv/bin/python -m unittest tests.test_crawler_snapshot`，
前端链路在 `frontend/tests/crawler-snapshot.spec.ts`。

领域：数据工程
子方向：数据采集、爬虫与RPA
