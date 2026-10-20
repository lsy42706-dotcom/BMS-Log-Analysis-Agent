import { analyzeLocally, inspectLocally } from './analysis.mjs';
import { CreateWebWorkerMLCEngine, prebuiltAppConfig } from '@mlc-ai/web-llm';

let worker=null, engine=null, running=false;
function problem(code) { return Object.assign(new Error(code),{code}); }
export function cancelLocal() {
  worker?.terminate();worker=null;engine=null;
}
async function loadEngine(consent,signal,onProgress) {
  if (!consent) throw problem('download_consent_required');
  if (!navigator.gpu || !globalThis.isSecureContext) throw problem('webgpu_unavailable');
  const adapter=await navigator.gpu.requestAdapter();
  if (!adapter) throw problem('webgpu_unavailable');
  signal?.throwIfAborted();
  if (engine) return engine;
  const precision=adapter.features.has('shader-f16')?'q4f16_1':'q4f32_1';
  const id=`Qwen3-1.7B-${precision}-MLC`;
  const record=prebuiltAppConfig.model_list.find(item=>item.model_id===id);
  if (!record || !record.model.startsWith('https://huggingface.co/mlc-ai/Qwen3-1.7B-')
      || !record.model_lib.startsWith('https://raw.githubusercontent.com/mlc-ai/')) throw problem('model_configuration_invalid');
  onProgress('正在加载 Qwen3-1.7B：下载公开模型文件或读取缓存；可取消。');
  worker=new Worker('/assets/local-built/engine-worker.js',{type:'module'});
  const owned=worker;
  const loaded=await CreateWebWorkerMLCEngine(worker,id,{appConfig:{...prebuiltAppConfig,model_list:[record]},
    initProgressCallback:progress=>{if(!signal?.aborted)onProgress(`免费模型加载：${Math.min(100,Math.max(0,Math.round(progress.progress*100)))}% · 可取消`);}});
  if(signal?.aborted || worker!==owned) { owned.terminate();throw problem('cancelled'); }
  engine=loaded;
  return engine;
}
export async function analyzeFree(payload,{consent=false,signal:outer,onProgress=()=>{}}={}) {
  if(running)throw problem('local_busy');running=true;
  const controller=new AbortController();
  const cancel=()=>{controller.abort();cancelLocal();};
  if(outer?.aborted)cancel();else outer?.addEventListener('abort',cancel,{once:true});
  let timedOut=false;
  const timer=setTimeout(()=>{timedOut=true;cancel();},360000);
  // Worker termination cannot settle pending worker RPCs, so race against cancellation explicitly.
  const aborted=new Promise((_,reject)=>{controller.signal.addEventListener('abort',()=>reject(problem(timedOut?'local_timeout':'cancelled')),{once:true});});
  aborted.catch(()=>{});
  try {
    return await analyzeLocally(payload,{signal:controller.signal,onProgress,
      getEngine:async()=>{
        const loaded=await Promise.race([loadEngine(consent,controller.signal,onProgress),aborted]);
        return {chat:{completions:{create:request=>Promise.race([loaded.chat.completions.create(request),aborted])}},
          resetChat:()=>Promise.race([loaded.resetChat(),aborted])};
      }});
  } finally {
    clearTimeout(timer);outer?.removeEventListener('abort',cancel);
    // Unload per run: weights remain in cache, but no KV state or request data is retained.
    cancelLocal();running=false;
  }
}
export { inspectLocally };
