# BMS 日志分析 Agent · 公开版

现有Python本地版的独立JavaScript/Workers部署，保留多通道规则、证据事实校验、关键波形和自带Key的Agent。原本地项目未修改。

## 使用与隐私

- 默认免费模型为 **Qwen3-1.7B**，WebLLM 0.2.85 在浏览器本地推理，无需 Key、没有付费模型 API 调用。CSV、问题、规则与证据在设备内处理，不发送到本站或模型下载站。
- 首次需勾选下载许可；模型公开文件从 Hugging Face / MLC 下载并可缓存。需要 WebGPU 及足够 GPU/共享内存：官方四位量化配置估算约 2–2.7 GB，另需浏览器和推理运行开销；这不是下载体积或电脑总内存承诺。设备算力、磁盘、流量和电量由使用者承担。
- 免费 SDK 路径由程序初始化三项工具读取，Qwen3-1.7B 实际只选择前 6 个事件中的证据 ID（问题前 500 字符），程序从可信事件复制事实并复核引用，丢弃模型自由文本。完整规则扫描与模型逐项审阅严格区分。不能据此声称完整 AI 根因诊断。
- 模型下载未授权、WebGPU 不支持、网络/显存错误、取消、6 分钟整体超时或结构化输出不合格：明确显示“免费 AI 未完成”，保留本地规则结果；绝不自动转付费模型。
- 可手动选择本站规则模式：不调用语言模型、不需要API Key，但 CSV 经HTTPS送至本站后端，不表示浏览器内处理。
- 高级付费 Agent 模式必须由每位使用者明确填写本次自己的OpenAI Key/模型。没有站点共享/作者Key、环境Key回退或自动配置Key；费用属于使用者对应API项目。免费浏览器构建排除了本应用的付费运行时，无法通过错误回退调用它。
- 本应用不主动保存CSV、Key、报告或跨用户日志缓存。免费模式只缓存公开模型文件，不缓存日志和问题；每次结束卸载推理 worker，不保留会话 KV 状态。报告由使用者自行导出。付费模式 Key 输入提交后清空；本站规则/付费模式的数据依然经过托管平台，不能视为平台零数据保留承诺。请勿上传敏感生产日志。
- 只读数值分析，不控制硬件；演示阈值不是真实电池标定，不证明电池安全或物理根因。

## 公开版边界

CSV512KiB、3,000采样、16传感器通道、20规则、64规则通道求值、5,000事件。付费模型8轮默认（4..12）、60秒（10..60）、单轮1,200输出tokens（256..2,000）、累计24,000软件预估护栏（2,000..48,000）。最多20次工具调用，32,000字符工具JSON。免费模型固定 4 轮 SDK 流程（3 次程序初始化工具 + 1 次本地模型选择）、最多 256 输出 tokens、10,000 UTF-8 字节模型输入预检；6 分钟整体限时，失败不自动重试。

软件预算不是精确tokenization或货币硬上限；失败保留已返回用量，未决请求另列未知；不自动重试。公网版本不提供持久检查点/CLI恢复，完整CLI和大日志仍使用原本地版。

API仅接受同源JSON及本站请求标识，拒绝重复/原型JSON字段、非法输入与超限工作；每个请求独立数据/Key。瞬时并发与限速是per-isolate内存护栏，不是分布式生产级限速或身份认证。

## 验证

```powershell
node scripts/build-browser.mjs
node --test tests/*.test.mjs
node scripts/run-framework.mjs dev --hostname 127.0.0.1 --port 5173
node scripts/run-framework.mjs build
```

验证包括17个独立手工标注场景与完整Python结果逐字段差分、延时/滞回/缺测/方向/质量、分页/波形、事实造假、BYOK隔离、JSON安全和真实SDK脚本模型。免费路径新增实际SDK+脚本本地引擎的无网络/无Key、覆盖边界、伪造拒绝、回退和本地波形测试。脚本引擎不证明真实GPU或模型质量；实际浏览器验收与是否完成真实推理见本次交付说明。未读取真实Key或调用付费API。

2026-10-20 实际浏览器模型验收：Qwen3-1.7B 完成公开合成 demo 的本地推理，31 个采样、3 个保护事件、引用 3/3，本地输入 426 / 输出 21 tokens，付费 API 请求为 0。仅是该演示样例的功能验收，不是模型效果基准，也不保证其他设备兼容。已处理 WebLLM `enable_thinking:false` 返回的空 `<think>` 模板前缀；非空推理、自由文本、伪造及重复引用仍拒绝。

Sites官方helpers用于初始化/来源同步/打包/部署。Windows npm.cmd解析问题使用明确npm JS入口运行同一install:ci，构建使用同一scripts/run-framework.mjs。Windows打包通过仅针对指定packager的bootstrap适配器，仍使用官方prepare-site-build.cjs检查和原元数据合并逻辑，再用原生tar；不修改插件或全局环境，不改变源码凭据/Git流程。

依赖固定@openai/agents0.20.0、openai7.32.0、zod4.3.6、@mlc-ai/web-llm0.2.85。应用持有受限工具，SDK持有模型循环，不使用托管Agent沙箱。[官方Agents SDK指南](https://developers.openai.com/api/docs/guides/agents/sdk)。浏览器包在预览/构建前自动生成于忽略目录 public/assets/local-built；模型参数不进入仓库。模型为 [Qwen3-1.7B（Apache-2.0）](https://huggingface.co/Qwen/Qwen3-1.7B)，推理库遵循其附带许可证；[WebLLM 文档](https://webllm.mlc.ai/docs/)。

正式站点身份由本地 .openai/hosting.json 管理；GitHub 仅提供无身份的 .openai/hosting.example.json。不要提交正式部署配置、.env、源码仓库凭据或上传/本地报告。D1/R2 未启用，没有共享 Key 秘密环境变量。

## 从 GitHub 源码运行网页版

此目录是已发布网页版的源码；它与仓库根目录的 Python 本地版独立。需要 Node.js >= 22.13.0，先进入 `website/`。

```powershell
cd website
npm ci
Copy-Item .openai/hosting.example.json .openai/hosting.json
node --test tests/*.test.mjs
npm run dev -- --hostname 127.0.0.1 --port 5173
```

macOS/Linux 将复制命令换成 `cp .openai/hosting.example.json .openai/hosting.json`。打开终端显示的本地地址。首次浏览器模型下载需要额外联网；测试使用脚本引擎，不下载权重，也不调用付费 API。`npm run build` 会先构建免费模型浏览器包，再构建 Workers 应用。

`.openai/hosting.example.json` 仅提供本地预览所需的空 D1/R2 绑定，不包含正式站点身份。`.openai/hosting.json` 是本地配置，不应提交；实际 Sites 发布必须使用你有权操作的站点配置，复制示例不代表拥有现有网站的发布权限。源码含构建用插件及其许可证，依赖由 `package-lock.json` 锁定；生成包、模型权重、密钥和工具缓存不进入 GitHub。

