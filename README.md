# 松门纪念园·入园凭条分区

真实 PostgreSQL 范围分区分流、迟到凭条回补并核验保留期边界。面向演示数据和本地复现，场景、操作剧本和业务边界见 [BUSINESS.md](BUSINESS.md)。

## 业务模块

- 凭条账簿
- 分区边界
- 生命周期执行
- 回收核验
- 在线账簿

## 在线账簿与节庆月拆分

`ledger` 父表按 `entered_on` 范围分区：每个在册月份一张月分区 `ledger_YYYYMM`，未登记月份落入 `ledger_default` 待核池。闸机补传按 `(slip_id, entered_on)` 幂等入账，已回执的凭条重传不丢不重，回执与查账页面都展示凭条实际所在的物理分区（`tableoid`）。

节庆月查询卡住时，经办员在「在线账簿」把该月拆成日分区：

1. **建影子树**：为当月建立 `ledger_YYYYMM_daily` 及每日一个叶子分区，登记拆分任务（同时只允许一个）。
2. **双写追齐**：worker 按检查点分批把月表拷入日分区，每批独立提交，中断后从检查点续拷；拆分期间补传在同一事务里双写月表与日分区，新到凭条始终两边一致。
3. **对账**：游标扫描耗尽后用 `EXCEPT` 双向核对；月表多出的行先补拷，日分区多出的行属异常、暂停切换。
4. **一次性切换**：对齐后在一条事务里 `DETACH` 月表、`ATTACH` 日分区树，旧月表改名 `ledger_YYYYMM_archive` 留档。查询始终走父表，切换前后不会查出新旧混用的结果；其他月份的入账和查询不受影响。

```sh
cd backend && python -m pytest tests/   # 需要可用的 PostgreSQL（DB_HOST/DB_PORT 等环境变量）
```

## 本地运行

```sh
cp -n .env.example .env
docker compose up -d --build --wait
```

- 工作台 `http://127.0.0.1:18304`
- FastAPI 文档 `http://127.0.0.1:19304/docs`
- PostgreSQL 16 端口 `127.0.0.1:17804`，账号 `app`，演示库 `workflow`
- `docker compose exec -T backend python -m scripts.seed_demo` 手动重跑演示数据
- `docker compose logs -f backend worker` 查看运行情况
- `docker compose down` 停容器并保留演示卷

### 演示账号

统一密码 `Demo-Walkthrough-2026`。

| 账号 | 角色 | 能执行的操作 |
|---|---|---|
| `operator` | 经办员 | 读取档案、创建实验输入、提交执行、导出结果 |
| `reviewer` | 复核员 | 读取、导出和签署结果 |
| `viewer` | 观察员 | 读取、导出已有结果 |

`SEED_DEMO=true` 启动时生成账号与一个业务样例。工作台新建档案会预填样例。可编辑表单中的数值、文本和结构化 JSON 后提交。业务说明中的原始文件是上游示例，演示数据通过表单录入。角色枚举位于后端 `src/constants/workflow.py` 与前端 `src/constants/workflow.js`。执行状态枚举也位于这两个文件，使用于队列、列表和执行详情。证据校验码是报告 JSON 按 UTF-8、键排序且省略多余空格序列化后的 SHA-256。

## 工程结构

| 服务 | 实现 |
|---|---|
| frontend | Nginx 与原生 HTML、CSS、JavaScript |
| backend | Python 3.11、FastAPI、SQLAlchemy |
| worker | Python 持久队列执行与 Redis 唤醒信号 |
| db | PostgreSQL 16，持久卷保存档案、执行、证据及审计 |
| redis | Redis 7，追加日志卷保存队列唤醒信号 |

编辑 `backend/src/services/domain.py` 可修改试算方法与业务表单说明。在线账簿的分区拆分逻辑位于 `backend/src/services/ledger.py`，验收测试位于 `backend/tests/`。API 路由、数据定义和页面位于 `backend/src/routes/`、`backend/src/models/` 与 `frontend/src/pages/`。开发时更新源文件后重新构建对应服务。

`.env.example` 提供 `FRONTEND_PORT`、`BACKEND_PORT`、`DB_PORT`、`DB_PASSWORD`、`AUTH_KEY` 、`SEED_DEMO` 与 `NETWORK_SUBNET`。修改 `NETWORK_SUBNET` 可避开本机现有容器网段。所有发布的端口绑定本机回环地址。接入独立部署前需配置自己的凭据并按业务范围做独立验收。

[MIT License](LICENSE)
