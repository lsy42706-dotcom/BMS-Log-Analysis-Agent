# BMS 日志分析 Agent · v0.2

电子信息/BMS方向的纯软件项目：上传或读取电压、电流、温度CSV，确定性规则生成证据；可选Agent决定如何取证，程序核验事实后生成报告。

**在线体验**：https://bms-log-agent.lsy42706.chatgpt.site

本仓库包含两个版本：

- **公开网页版（`website/`）**：默认 **Qwen3-1.7B**，通过 WebLLM 在使用者浏览器本地推理，无需 API Key、不产生付费 API 调用；免费模式的 CSV、问题与证据不上传。首次需允许下载模型，并具备 WebGPU 及足够 GPU/共享内存。设备算力、存储、流量和电量由使用者承担。
- **Python 本地版（仓库根目录）**：仍默认离线规则分析，不需要 Key、不调用模型 API，界面仅监听本机。原有 CLI、评测和恢复功能保持不变。

网页版的免费模型只审阅前 6 个事件及问题前 500 字符，确定性规则仍扫描全部采样；运行失败或取消会明确回退到本地规则，不会自动转付费模型。只有主动选择高级付费 Agent 并填写本次自己的 Key 才会调用付费 API，费用归属该 Key 对应的项目；没有作者或共享 Key。手动选择“本站规则分析”或“高级付费 Agent”时，CSV 会经 HTTPS 发送至本站后端。

2026-10-20：免费模型更新已同步至 `website/`。网页版 93 项 Node 测试通过，并完成真实 Qwen3-1.7B 的 31 采样演示推理（3 个保护事件，证据引用 3/3）；仅为合成样例功能验收，不是电池安全认证或模型效果基准。源码复现和隐私边界见 [网页版说明](website/README.md)。下文的 Python 启动、七项升级和评测说明主要对应本地版。

## 快速体验

在线版直接打开上方链接即可使用；本地版需要Python 3.10+，从项目目录运行；已有 `.venv` 可以直接使用。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m bms_agent.web --port 8765
```

打开启动时显示的 http://127.0.0.1:8765 （本地版），或直接使用在线版 https://bms-log-agent.lsy42706.chatgpt.site ，点击”载入演示数据”或选择UTF-8 CSV，再”开始分析”。支持列映射、单位换算、规则/质量配置、通道切换、点击事件定位窗口和JSON/Markdown导出。环境变量PORT可设默认端口，显式--port优先。Ctrl+C停止本地服务。

仅支持127.0.0.1，不能直接改为公网/LAN监听。页面不使用浏览器持久化存储；上传、分析缓存和Key仅在进程/页面内存，导出由使用者自行保存。Key提交时立即清空，不回退环境变量；再次Agent分析需要重新输入。

命令行离线分析：

```powershell
.\.venv\Scripts\python.exe -m bms_agent samples/demo.csv
.\.venv\Scripts\python.exe -m bms_agent samples/multichannel.csv --rules rules/multichannel-example.json
```

Linux/macOS对应使用 `.venv/bin/python`。离线分析和本地网页都不需要模型SDK。

## 按顺序完成的七项升级

| 阶段 | 能力 | 可信边界 |
|---|---|---|
| 1 | 类型化事实与覆盖校验 | ID、规则、通道、时间、峰值和状态精确匹配；拒绝伪造/重复引用及虚假full |
| 2 | 事件分页与全量聚合 | offset按事件数而非采样数；30,000事件回归；全量规则统计不等于模型逐项取证 |
| 3 | 关键点波形 | 保留端点、通道极值和事件边界；容量不足明确披露遗漏 |
| 4 | 本地上传工作台 | 映射、单位、曲线、事件、导出；默认离线、仅loopback |
| 5 | 多通道与质量规则 | 通道身份、充放电约定、压差/温差、合理范围/低变化/突变候选 |
| 6 | 独立标注评测 | 手工CSV/标签/graders分离；规则与SDK假模型分开；真实模型未测 |
| 7 | 预算与恢复 | 已返回/未决用量、错误分类、配置指纹缓存、本地证据检查点；不自动重试 |

模型自由文本不作为已验证结论：应用丢弃模型summary/explanation/limitations，按核验事实重建；coverage也由程序计算。这核验可计算的日志事实，不提供未经证实的物理根因解释。

## CSV、通道和单位

旧四列继续支持：

```csv
timestamp_s,cell_voltage_v,current_a,temperature_c
0,3.70,10,25
1,3.69,50,25
2,3.68,50,25
3,3.67,50,25
```

多通道：

```csv
timestamp_s,current_a,cell_voltage_v_C01,cell_voltage_v_C02,temperature_c_T01,temperature_c_T02
0,10,3.70,3.69,25,26
1,10,3.68,3.69,25,26
```

要求时间、电流和至少一路电压/温度。通道ID为短ASCII字母/数字/下划线，以字母开头；不把宽表第一路/最小值冒充旧标量。pack电流只分析一次，电压/温度逐通道。事件含pack/cell/probe/channel身份、CSV行号与输入SHA-256。

时间必须有限且严格递增。无效、缺失、NaN/Infinity传感器值转缺测并产生质量证据，不保留原始无效字符串；其他非传感器列忽略。默认s/V/A/degC，支持ms/mV/mA/K和温度C别名，不猜单位。

非标准列通过canonical→source映射。例如自己的导入配置JSON：

```json
{
  "column_map": {
    "timestamp_s": "Time_ms", "current_a": "Current_mA",
    "cell_voltage_v_C01": "Cell1_mV", "temperature_c_T01": "Probe1_K"
  },
  "units": {
    "timestamp_s": "ms", "current_a": "mA",
    "cell_voltage_v_C01": "mV", "temperature_c_T01": "K"
  }
}
```

```powershell
.\.venv\Scripts\python.exe -m bms_agent your-log.csv --mapping your-mapping.json
```

网页高级设置分别填column_map与units对象，无需外层包装。标准宽表自动识别，不需逐路手填。

默认正电流表示放电；网页或 `--current-convention positive_charge` 改为正充电。零为idle，缺测unknown。默认过流用绝对值；只有显式 `current_mode=charge/discharge` 区分方向。

上限：5MiB、20,000样本、64传感器通道、100规则、256规则×通道求值、100,000事件。模型工具JSON最多32,000字符；摘要前12通道统计/前12事件组明确裁剪，完整结果仍在本地报告。

## 规则与质量配置

`rules/example.json` 兼容旧格式；`rules/multichannel-example.json` 演示充/放电、过温/过欠压、压差/温差及质量检查。

```json
{"rules":[{
  "rule_id":"discharge_oc","code":"DISCHARGE_OVERCURRENT",
  "field":"current_a","direction":"abs_high",
  "threshold":60,"recovery_threshold":50,"confirm_s":2,"recovery_s":2,
  "max_gap_s":2,"current_mode":"discharge"
}],"quality":{
  "bounds":{"cell_voltage_v":[0,6]},
  "stuck_s":{"cell_voltage_v":8},"stuck_epsilon":{"cell_voltage_v":0.001},
  "max_rate":{"temperature_c":20},"max_gap_s":2
}}
```

high/low/abs_high触发和恢复均包含相等边界。确认/恢复按时间差，不按采样数量；缺测或超过max_gap_s重启待确认计时。已确认故障在未知区间保持未恢复，直到观测到完整恢复条件。

电压/温度可用generic字段逐通道、channel_id限定一路，或直接指定cell_voltage_v_C01。cell_spread_v/temperature_spread_c要求至少两路且全部配置通道有效，否则该采样不可计算，不能从掉线推断恢复。

质量检查默认关闭，须按设备配置。bounds保留测量并标候选；stuck仅说明低变化，稳定工况也可能满足；spike只比较相邻有效采样，不跨缺口推断。它们不证明传感器损坏。单通道无法评估的spread在not_evaluated_rules明示。

## 可选Agent与BYOK

安装可选固定版本SDK：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[agent]"
```

网页Agent模式填本次自己的Key和有权限的模型ID；命令行隐藏输入：

```powershell
.\.venv\Scripts\python.exe -m bms_agent samples/demo.csv --mode agent --model YOUR_MODEL_ID --prompt-key
```

CLI也支持使用者自己的进程环境变量OPENAI_API_KEY/OPENAI_MODEL。不自动加载.env；.env.example只是空值格式。不要把Key写进参数/代码/日志/聊天。无法隐藏输入时停止，不回退明文。

只有显式Agent模式可能收费。问题、摘要、选取数值和证据会发给模型，真实敏感日志需先获得授权。模型入口/落盘前脱敏；独立客户端、固定官方地址、不读OPENAI_BASE_URL、store=False、远程追踪关闭。这不是API零保留承诺。

| 工具 | 作用 | 限制 |
|---|---|---|
| summarize_log | 摘要/指纹/全量事件统计 | 裁剪明确，不发整份CSV |
| list_protection_rules | 阈值/延时/滞回/质量 | 每页20规则，offset/next_offset |
| detect_faults | 事件和类型化facts | 每页最多40，按实际事件数分页 |
| inspect_window | 数值与保真信息 | 最多50条，关键点不足明确披露 |
| get_evidence | 事件与行号 | 不接受任意路径 |
| simulate_rule | 合成序列复核 | 最多128条，SIM不能替代日志证据 |

必须实际完成摘要/规则/故障工具才能结论；full要求全部所选日志事件取证并引用。大量事件可以明确partial、引用代表事件；完整规则扫描仍保留。问题不会自动重定义证据范围。

## 预算、失败、缓存与恢复

默认8轮、单轮1200输出tokens、整次120秒、20工具调用、24,000总tokens软件护栏。通过--max-turns/--max-output-tokens/--timeout/--max-total-tokens调整。

下一轮前检查累计已返回用量+输入字节近似预估+输出上限，返回后核对SDK实际用量。预估不是精确tokenization，未返回请求无法知道最终用量，不是绝对Token/金额上限。真正账单控制还需使用者自行设置API项目限制。

校验失败、超时、取消和轮次退出保留已返回用量；未决请求另列unresolved_requests，不能当零。认证、权限、额度、限速、网络、超时、预算、轮次和校验分开，内部响应不回显。不自动重试付费请求。

每次CLI创建独立报告目录，不覆盖旧文件：

```text
report.md          中文报告、事件、覆盖与配置
report.json        完整分析、事实、指纹、用量与状态
tool_calls.json    工具审计
checkpoint.json    仅Agent；无Key或原始模型文本
```

检查点保存本地只读工具参数与已知/未决用量。恢复核对源内容、实际列映射/单位/规则/质量/电流约定/pack/版本/问题/模型，再重算证据，不信任结果正文。

```powershell
.\.venv\Scripts\python.exe -m bms_agent samples/demo.csv --mode agent --model YOUR_MODEL_ID --prompt-key --resume reports/PREVIOUS_RUN/checkpoint.json --max-turns 12
```

原任务的自定义问题/规则/映射/电流约定必须相同。总轮次/已知用量含上次运行，未决计数保留。恢复是应用层本地证据重播，不是隐藏推理、远程会话或在途请求恢复；继续模型调用仍可能收费。网页暂不提供恢复入口，使用CLI。

缓存只存确定性分析，不缓存Key或模型回复。内容和实际导入/规则配置指纹、拷贝隔离、进程内最多4项且每项≤4MiB；大结果不缓存，重启失效。

## 测试与独立评测

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m evals.runner --output reports/my-rule-eval
.\.venv\Scripts\python.exe -m evals.runner --output reports/my-sdk-mock-eval --sdk-mock
```

评测使用新目录。evals从源码目录运行，不是wheel安装后的命令。默认stdlib；SDK-mock真实Runner+脚本模型阻断API客户端/网络，测试工具链而非模型能力。Windows限制asyncio本机socket的沙箱需允许本机测试进程，不能借此调用真实API。

17个版本化手工合成场景、22个手工预期事件；CSV/标签/加载器/graders/执行器/报告分离。规则、类型、通道、状态、时间（当前容差0）匹配，峰值另核对。required失败非零；未定义分母null，不从排除失败的场景冒称整体通过。

本次基线和SDK假模型均17/17，TP22/FP0/FN0、事实22/22、对抗57/57。这仅适用于小型合成回归集，不代表真实电池/真实模型准确率、通用提示注入安全、速度或费用。真实模型未测、真实API调用及费用为零。详见UPGRADE_NOTES.md和evals/README.md。

## 简历与限制

按自己能解释和复现的内容描述：

> 开发BYOK的BMS日志分析Agent及本地上传工作台，设计6类受限证据工具，实现多通道保护状态机、单位归一化、质量诊断、事实与覆盖校验、关键点波形、Token护栏及可恢复审计；构建17个独立标注合成场景，完成规则基线和真实SDK假模型回归测试。

没有CAN/DBC适配、真实数据/模型评测、生产级认证隔离、硬件控制或电池安全认证。演示阈值不能用于真实电池保护。

## 技术依据

保持现有代码先行的SDK架构，不迁移托管执行环境。[官方SDK指南](https://developers.openai.com/api/docs/guides/agents/sdk)；本地重播参考[运行指南](https://developers.openai.com/api/docs/guides/agents/running-agents)。依赖固定openai-agents==0.23.1，真实模型未调用。

