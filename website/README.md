# BMS 日志分析 Agent · 公开版

现有Python本地版的独立JavaScript/Workers部署，保留多通道规则、证据事实校验、关键波形和自带Key的Agent。原本地项目未修改。

## 使用与隐私

- 默认离线规则模式：不调用语言模型、不需要API Key。CSV仍经HTTPS送至本站后端；离线不表示浏览器内处理。
- Agent模式必须由每位使用者明确填写本次自己的OpenAI Key/模型。没有站点共享/作者Key、环境Key回退或自动配置Key；费用属于使用者对应API项目。
- 本应用不主动保存CSV、Key、报告或跨用户缓存。报告由使用者自行导出。Key输入提交后清空；数据依然经过托管平台，不能视为平台零数据保留承诺。请勿上传敏感生产日志。
- 只读数值分析，不控制硬件；演示阈值不是真实电池标定，不证明电池安全或物理根因。

## 公开版边界

CSV512KiB、3,000采样、16传感器通道、20规则、64规则通道求值、5,000事件。模型8轮默认（4..12）、60秒（10..60）、单轮1,200输出tokens（256..2,000）、累计24,000软件预估护栏（2,000..48,000）。最多20次工具调用，32,000字符工具JSON。

软件预算不是精确tokenization或货币硬上限；失败保留已返回用量，未决请求另列未知；不自动重试。公网版本不提供持久检查点/CLI恢复，完整CLI和大日志仍使用原本地版。

API仅接受同源JSON及本站请求标识，拒绝重复/原型JSON字段、非法输入与超限工作；每个请求独立数据/Key。瞬时并发与限速是per-isolate内存护栏，不是分布式生产级限速或身份认证。

## 验证

```powershell
node --test tests/*.test.mjs
node scripts/run-framework.mjs dev --hostname 127.0.0.1 --port 5173
node scripts/run-framework.mjs build
```

79项Node测试通过：17个独立手工标注场景与完整Python结果逐字段差分、延时/滞回/缺测/方向/质量、分页/波形、事实造假、BYOK隔离、JSON安全和真实SDK脚本模型。另覆盖实际SDK的不完整响应用量及缺用量停止。真实模型效果/时延/费用未测；没有读取Key或调用真实API。

Sites官方helpers用于初始化/来源同步/打包/部署。Windows npm.cmd解析问题使用明确npm JS入口运行同一install:ci，构建使用同一scripts/run-framework.mjs。Windows打包通过仅针对指定packager的bootstrap适配器，仍使用官方prepare-site-build.cjs检查和原元数据合并逻辑，再用原生tar；不修改插件或全局环境，不改变源码凭据/Git流程。

新依赖固定@openai/agents0.20.0、openai7.32.0、zod4.3.6。应用持有受限工具，SDK持有模型循环，不使用托管Agent沙箱。[官方Agents SDK指南](https://developers.openai.com/api/docs/guides/agents/sdk)。

站点身份记录在.openai/hosting.json；不要编辑project_id、提交.env、持久化源码仓库凭据或把上传/本地报告加入源码。D1/R2未启用，没有共享Key秘密环境变量。
