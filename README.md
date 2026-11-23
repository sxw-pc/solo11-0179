# 巡回展文物借展护运采证台

面向真实业务采录的离线样例项目，包名 `exhibitflow`。全部样例均为虚构身份和资源；运行不需要外部服务。

## 安装与运行

`python -m pip install -e . --no-deps`

`python -m exhibitflow init --db local.db`

`python -m exhibitflow demo --db local.db`

`python -m exhibitflow audit --db local.db`

`python -m unittest discover -s tests -v`

## 三个独立入口

### 借展契约

`python -m exhibitflow loan --db loan.db --fixture fixtures/loan_normal.json`

失败证据：`fixtures/loan_failure.json`。展期与借展授权是否允许出库。持久化对象包括 借展合同、展期许可，领域模块位于 `exhibitflow/loan.py`。

### 包装护运

`python -m exhibitflow transit --db transit.db --fixture fixtures/transit_normal.json`

失败证据：`fixtures/transit_failure.json`。箱件封签、交接与途中异常是否保持责任连续。持久化对象包括 箱件清单、护运交接，领域模块位于 `exhibitflow/transit.py`。

### 展陈状况

`python -m exhibitflow condition --db condition.db --fixture fixtures/condition_normal.json`

失败证据：`fixtures/condition_failure.json`。到馆状态、环境记录与归还比较决定是否开箱展示。持久化对象包括 状况照片、展陈环境，领域模块位于 `exhibitflow/condition.py`。

### 车队路线采录与派车

`python -m exhibitflow demo --db convoy.db --fixture fixtures/convoy-routes.json`

采录外部证据（道路公告截图/OCR、承运 ICS 循环实例、临时改线 CSV、逐车护运资质、道路通行许可、车厢件号、封条换车记录），归一化后求全局派路，领域模块位于 `exhibitflow/route_capture.py`。派生表全部由 `sources` 存档重建，重跑得到相同车次身份、冲突图与当前派车表。`transit` 的 `dispatch` 事件写入 `convoy_plans`、`handoff_orders`（已发指令被改线推翻时保留 `superseded_by` 历史并记 `compensation_events`）与 `conflict_edges`。

- 公告按发布主体与有效区间归并：不同发行方相同 `road_id` 互不覆盖，撤回仅撤自己发行方的通知。
- `RECURRENCE-ID` 只替换该次车次，未触及的系列实例保留；夏令时回拨用 `FOLD` 保持同墙钟两次车次身份不同。
- 冲突边显式建模单车道路段容量与同一份许可的消耗；`solve` 穷举全局交换，`greedy` 复现按车次先后的局部贪心以便对照。
- 签认协议两种边界：`transferable-endorsement` 允许发车前双签改线，`locked-endorsement` 固定原签认路线并把另一车标为无解。

## 持久化与恢复

`events` 留原始 JSON、摘要、有效时刻和接收时刻。`subjects`、`documents`、`allocations`、`receipts` 留业务主体、版本、占位与回执；`decisions` 保存每次裁定。事件 ID 相同且摘要相同视为重试，摘要不同拒绝。每个命令在 SQLite 事务内更新事件、状态和决定，避免半写。`audit` 在独立内存数据库从原始事件重新运行规则，对照五类表，可发现被篡改的裁定。

## 业务能力

- **借展许可冻结**：批准版本限定文物与时间范围，改变最终业务可执行状态。

- **展期冲突裁定**：同一藏品不得被两个馆同时借占，改变最终业务可执行状态。

- **保险责任挂接**：授权与责任时段决定风险归属，改变最终业务可执行状态。

- **箱件封签验收**：清单与封签一致才能出库，改变最终业务可执行状态。

- **护运交接连续性**：交接人和时间链断裂时暂停接收，改变最终业务可执行状态。

- **途中异常处置**：事故后重封必须保留旧证据与批准，改变最终业务可执行状态。

- **到馆状况比对**：与离馆基线比对决定开箱展示，改变最终业务可执行状态。

- **展厅环境放行**：环境窗合格且责任已交接才可开放，改变最终业务可执行状态。

## 跨系统案例

- 借展合同被撤销使装箱护运停止，到馆状况记录仍可采证但不得开箱展出。异常：撤销晚于出库时保留已发生交接责任和在途拦截报告。

- 途中封签破损触发到馆独立状况检查，损伤证据反向改变借展合同的保险责任归属。异常：重封事件先于破封事件导入时应按业务时刻重建责任链。

- 展厅温湿超限暂停展出并影响借展展期，合同修订后护运模块重新确定归还交接时间。异常：环境记录迟到时不得抹去此前合规展出期间及护运责任。

## 验证范围与局限

自动测试覆盖身份冲突、状态传播、拒绝与恢复、幂等重放及审计篡改。fixture 展示正常与失败输入。项目使用离线确定性规则和小规模虚构样例；尚未处理真实部门系统、真实证照签名或现场设备。
