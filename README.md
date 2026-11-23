# 巡回展文物借展护运采证台

面向真实业务采录的离线样例项目，包名 `exhibitflow`。全部样例均为虚构身份和资源；运行不需要外部服务。

## 安装与运行

`python -m pip install -e . --no-deps`

`python -m exhibitflow init --db local.db`

`python -m exhibitflow demo --db local.db`

`python -m exhibitflow audit --db local.db`

`python -m unittest discover -s tests -v`

## 四个独立入口

### 借展契约

`python -m exhibitflow loan --db loan.db --fixture fixtures/loan_normal.json`

失败证据：`fixtures/loan_failure.json`。展期与借展授权是否允许出库。持久化对象包括 借展合同、展期许可，领域模块位于 `exhibitflow/loan.py`。

### 包装护运

`python -m exhibitflow transit --db transit.db --fixture fixtures/transit_normal.json`

失败证据：`fixtures/transit_failure.json`。箱件封签、交接与途中异常是否保持责任连续。持久化对象包括 箱件清单、护运交接，领域模块位于 `exhibitflow/transit.py`。

### 展陈状况

`python -m exhibitflow condition --db condition.db --fixture fixtures/condition_normal.json`

失败证据：`fixtures/condition_failure.json`。到馆状态、环境记录与归还比较决定是否开箱展示。持久化对象包括 状况照片、展陈环境，领域模块位于 `exhibitflow/condition.py`。

### 通关取证（双栏放行单）

`python -m exhibitflow customs --db customs.db --fixture fixtures/customs-columns.json --evidence sealing_scan`

同一份 fixture 用 `--evidence unboxing_recheck --legacy` 重放另一路取证，会得到相反的逐件名单。原票：一箱 artifact-1/artifact-2，车次 QR=TR-8，左栏 X-31（允许目的地 B），右栏 X-32（只允许本地转运），票面不写件号。领域模块位于 `exhibitflow/customs.py`。

- **原件与字格**：保存放行单原图字节与摘要、每栏 OCR 字符格（含修订版本）、人工修字、海关电子签章原字节与落章矩形。验签要求签章完整覆盖件号格，OCR 字符格必须落在该栏印字区域内。
- **归属入口**：车次、箱号、封条只写入 `customs_candidates`，不参与逐件裁定。逐件归属只接受 `sealing_scan`（封箱时逐件签名扫描台账）或 `unboxing_recheck`（海关独立开箱复核）两种有权证据。
- **逐件结果**：`confirmed` / `held` / `unresolved`。归属未定时保留两种完整匹配，绝不按页序或上传时间挑一栏。同一 `(trip, document, col_no)` 只由一件占用，由 `customs_column_claims` 事务锁保证。
- **错关联撤销**：旧 RPA 的 `legacy_associate` 只在 `customs_attributions` 记为 `provisional`。被权威证据或迟到页面推翻时，追加 `customs_voids` 反向作废，并使已封/在途分配进入 `customs_intercepts`；旧归属行与已发护运裁定永不改写。
- **证明包**：`customs.package(...)` 按文物输出唯一栏位、证据链、原件、作废链、拦截链与局限声明（历史封箱≠当前箱内实物；开箱复核不倒填封箱扫描顺序；未定时并列完整匹配）。

`loan.authorize` 与 `transit.seal/handoff/reseal` 消费逐件证明包：凡本票点名而件号未 `confirmed` 的文物一律拦截，本票未点名的文物不受牵连。

## 持久化与恢复

`events` 留原始 JSON、摘要、有效时刻和接收时刻。`subjects`、`documents`、`allocations`、`receipts` 留业务主体、版本、占位与回执；`decisions` 保存每次裁定。事件 ID 相同且摘要相同视为重试，摘要不同拒绝。每个命令在 SQLite 事务内更新事件、状态和决定，避免半写。`audit` 在独立内存数据库从原始事件重新运行规则，对照所有业务表（含海关原件、字格、签章、归属、作废与拦截），可发现被篡改的裁定。海关识别结果与 SQLite 归属写入在同一事务提交；同一证据时刻重试是幂等的，不会让同一放行栏被两件各自占用。

## 业务能力

- **借展许可冻结**：批准版本限定文物与时间范围，改变最终业务可执行状态。

- **展期冲突裁定**：同一藏品不得被两个馆同时借占，改变最终业务可执行状态。

- **保险责任挂接**：授权与责任时段决定风险归属，改变最终业务可执行状态。

- **箱件封签验收**：清单与封签一致才能出库，改变最终业务可执行状态。

- **护运交接连续性**：交接人和时间链断裂时暂停接收，改变最终业务可执行状态。

- **途中异常处置**：事故后重封必须保留旧证据与批准，改变最终业务可执行状态。

- **到馆状况比对**：与离馆基线比对决定开箱展示，改变最终业务可执行状态。

- **展厅环境放行**：环境窗合格且责任已交接才可开放，改变最终业务可执行状态。

- **逐件海关栏位归属**：车次二维码只证明整车放行，逐件归属须由封箱逐件签名扫描或海关开箱复核确认，改变最终业务可执行状态。

- **错关联反向作废**：旧 RPA 误把件号关联错位时，追加作废链并拦截在途文物，不回写旧归属或已发护运决定，改变最终业务可执行状态。

- **两种取证相反名单**：同一张双栏原票，封箱扫描与开箱复核可交付相反的 confirmed/held 名单，各自注明证据时点局限，改变最终业务可执行状态。

## 跨系统案例

- 借展合同被撤销使装箱护运停止，到馆状况记录仍可采证但不得开箱展出。异常：撤销晚于出库时保留已发生交接责任和在途拦截报告。

- 途中封签破损触发到馆独立状况检查，损伤证据反向改变借展合同的保险责任归属。异常：重封事件先于破封事件导入时应按业务时刻重建责任链。

- 展厅温湿超限暂停展出并影响借展展期，合同修订后护运模块重新确定归还交接时间。异常：环境记录迟到时不得抹去此前合规展出期间及护运责任。

- 报关员收到手机拍摄的双栏放行单，页首二维码只指向车次。逐件归属未定前不得把“车次获放行”写成“车上每件均获放行”；补采封箱扫描或海关开箱复核后，两路证据可给出相反的逐件名单。异常：旧 RPA 已按页序误关联并触发封箱时，须生成反向作废事件并拦截在途文物，迟到页面、OCR 重跑或报关补正不得覆写旧归属。

## 验证范围与局限

自动测试覆盖身份冲突、状态传播、拒绝与恢复、幂等重放及审计篡改。fixture 展示正常与失败输入。项目使用离线确定性规则和小规模虚构样例；尚未处理真实部门系统、真实证照签名或现场设备。
