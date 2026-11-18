# 配送签收

乡村配送资料的本地演练来源与可追溯采集。目录有稳定标识，详情随发布期改变；合成资料位于 fixtures/source.json。app.capture 读取来源，app.domain 解释业务身份，app.records 保存不可变原件、每次交接与凭证版本。

## 业务模型

- **运单**以来源稳定标识 `id` 为业务键，显示名称不作键；`waybills` 表只跟踪运单当前路线（最新游标指向的交接站）。
- **交接**是运单在某个游标（revision/发布期）上的一次实际交接，自然键为 `运单id#r游标`。改派产生新交接，旧交接与旧凭证原样保留，旧签名不会配给新路线。
- **凭证**（签收回执/签收图片）按 `运单+游标` 锚定到具体交接，按内容指纹去重：同内容重复上传识别为同一版本；换内容追加为“待核”新版本，已核实版本不被改写。
- 到站表先到、凭证未到时，交接状态为**待核**，系统不会推断为“未签收”；回执在下一趟网络恢复后补传即核实。
- `raw_receipts` 只追加保存每次来源读取的原件。重启续跑时 `process_pending` 按 `progress` 游标与已存原件的落差补齐关联，每条原件独立事务、重复执行幂等。
- `trace_handoff` 可追溯任意一次交接的真实出处：到站原件收据、采集时间、全部凭证版本及其原件收据。

## 用法

- `python -m unittest discover -s tests -v`：运行本地来源与追溯规则测试。
- `python -m compileall -q app tests`：检查项目代码。
- `python -m app.site`：在 8765 端口启动演练来源。
- `python -m app.cli collect [db路径] [发布期]`：采集一轮到站表与全部凭证（默认 records.sqlite3、发布期 1），采集前自动补齐落差。
- `python -m app.cli overview`：客服总览，运单当前路线与最新交接状态。
- `python -m app.cli pending`：待核交接（凭证缺席或换版待核）。
- `python -m app.cli trace <handoff_key>`：追问某一次交接的完整出处。
- `python -m app.cli receipts`：不可变原件流水。
- `python -m app.cli progress`：各来源游标与原件落差。
- `python -m app.cli backfill`：手动触发重启落差补齐。
