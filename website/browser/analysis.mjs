import { analyzeRequest, inspectRequest, markdownReport } from '../lib/service.mjs';
import { runFreeAgent, FREE_MODEL, MAX_REVIEW_EVENTS, zeroApiUsage } from './free-agent.mjs';

const allowed=new Set(['csv_text','source_name','column_map','units','profile','current_convention','pack_id','question']);
export function localPayload(payload) {
  if (!payload || typeof payload!=='object' || Array.isArray(payload)
      || Object.keys(payload).some(key=>!allowed.has(key))) throw new Error('Unexpected fields in local analysis');
  return {...payload};
}

export async function analyzeLocally(payload, {getEngine,signal,onProgress=()=>{}}) {
  const clean=localPayload(payload);
  const rules=await analyzeRequest({...clean,mode:'offline'});
  let result=rules, failure=null, failedUsage={};
  try {
    signal?.throwIfAborted();
    const engine=await getEngine();
    signal?.throwIfAborted();
    onProgress('Qwen3-1.7B 正在本地审阅证据；无付费 API 调用…');
    let localError;
    const ai=await analyzeRequest({...clean,mode:'agent',model:FREE_MODEL,api_key:'browser-local-not-a-key'},
      {runAgent:async box=>{try{return await runFreeAgent(box,clean.question||'分析保护事件与数据质量',engine,signal);}
        catch(error){localError=error;throw error;}}});
    if (ai.report.status!=='complete') throw localError||new Error('local_inference_failed');
    result=ai;
  } catch(error) { failure=error?.name==='AbortError'?'cancelled':error?.code||error?.message||'local_inference_failed';
    failedUsage=error?.localInferenceUsage||{}; }
  const report=result.report;
  report.mode='browser';report.deployment='browser_local';report.provider='webllm';report.model=FREE_MODEL;
  report.billing='No paid model API requests; local device compute only';
  report.privacy='CSV, question and evidence processed on this device only. Public model artifacts may be downloaded and cached; log content is not persisted.';
  report.usage=failure?zeroApiUsage(failedUsage):report.usage;
  report.local_ai={model:FREE_MODEL,status:failure?'unavailable':'complete',fallback:failure?'local_rules':null,
    review_event_limit:MAX_REVIEW_EVENTS,bootstrap_tools:'application_driven',question_chars_used:Math.min(500,(clean.question||'').length)};
  if (failure) {
    report.status='fallback';
    const reasons={webgpu_unavailable:'当前浏览器或设备不支持 WebGPU。',download_consent_required:'尚未允许首次模型下载。',
      cancelled:'已取消本次模型运行。',local_timeout:'本地模型加载或推理超时。',
      invalid_local_output:'模型输出未通过证据校验。',local_generation_incomplete:'本地模型生成未完整结束。',
      local_context_limit:'免费模型的本次上下文超过限制。'};
    report.local_ai.reason=reasons[failure]||'模型加载或本地推理未完成，可能是网络、缓存、显存或模型兼容问题。';
    report.local_ai.model_used=false;
    report.conclusion.limitations.push('本次免费 AI 未完成：'+report.local_ai.reason+' 当前显示确定性规则结果，没有自动转用付费 API。');
  } else {
    report.local_ai.model_used=true;
    report.conclusion.coverage.consumer='local_model_selected_ids_application_copied_facts';
    report.conclusion.limitations.push(`免费模型仅审阅前 ${MAX_REVIEW_EVENTS} 个事件、问题前 500 字符。三项初始工具由程序固定读取；引用事实由程序复制和验证，不是模型重新计算。`);
  }
  result.report_markdown=markdownReport(report)+'\n## 免费本地模型\n\n'+
    `- 模型：${FREE_MODEL}；AI 状态：${report.local_ai.status}；付费 API 调用数：0\n`+
    '- 推理在浏览器设备运行；实际本地 token 用量与 API 计费是不同指标。\n'+
    (failure?'- AI 未完成，已保留本地规则结果；没有转用付费 API。\n':'');
  return result;
}
export async function inspectLocally(payload) {
  const {start_s,end_s,max_samples,...rest}=payload;
  return inspectRequest({...localPayload(rest),start_s,end_s,max_samples});
}
