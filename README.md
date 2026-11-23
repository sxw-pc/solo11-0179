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

### 签署授权链

`python -m exhibitflow authority --db authority.db --fixture fixtures/delegation-chain.json`

采录董事会决议、馆长授予书、转授权书、人事撤权、文物估值、签章坐标与签约时刻。每条代理边保存上游签名、有效区间、金额上限、目的地范围和是否允许再委托。一条签署权路径的有效范围取所有边的时间交集、金额最小上限与目的地交集；并行路径只作备选，**不相加**。同一人按员工号区分同名；同一公文重印按内容去重，改版按源版本保留。

`authority.evaluate` 返回每份协议在签字时刻的可核路径（`paths`/`valid_paths`）、单条路径瓶颈 `narrowest_limit`、失效边 `invalid_edges` 与待补授权 `missing_approvals`；并行路径各自限额见 `limits_by_path`，可用路径取其中最强一条，绝不跨路径求和。

`fixtures/delegation-chain.json` 是董事会→馆长（B 地 200，日 1–10）→策展人 C（B 地 100，日 1–8）→助理 D（B 地 150，日 1–10），artifact-1 估值 120、D 于时刻 6 签字。整条路径的瓶颈是 100，因此 `loan.authorize` 与 `transit.seal` 都会拒绝 120 的文物。

两种补救：董事会以带明确生效时刻的新决议直接授权 D（新协议版本放行原文物，旧无权签字保留）；或不补签，只签估值 80 的独立低价值附件/复制件协议（沿用 C→D 原路径），原 artifact-1 仍不能出馆。迟到的撤权或旧错误封箱会生成 `compensations` 记录，阻止下一条移交，已发生的物理交接保留在历史中。

## 持久化与恢复

`events` 留原始 JSON、摘要、有效时刻和接收时刻。`subjects`、`documents`、`allocations`、`receipts` 留业务主体、版本、占位与回执；`authority_evaluations` 与 `compensations` 留每次授权裁定及停止移交的补偿决定；`decisions` 保存每次裁定。事件 ID 相同且摘要相同视为重试，摘要不同拒绝。每个命令在 SQLite 事务内更新事件、状态和决定，避免半写。`audit` 在独立内存数据库从原始事件重新运行规则，对照各业务表，可发现被篡改的裁定。

## 业务能力

- **借展许可冻结**：批准版本限定文物与时间范围，改变最终业务可执行状态。

- **签署权路径裁定**：按员工号串起代理链，取时间交集、额度瓶颈与目的地交集，拒绝超出最窄额度的文物，改变最终业务可执行状态。

- **签发时刻授权快照**：每份协议保留签字发生时刻的可核路径、失效边与待补授权；新授权不倒填旧签字。

- **迟到撤权补偿**：撤权或旧错误封箱阻止下一条移交，已发生交接保留在历史中，可重放责任版本。

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
