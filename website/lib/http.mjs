/** Public request boundary. No uploads, credentials or provider errors logged. */

export const PUBLIC_ORIGIN = "https://bms-log-agent.lsy42706.chatgpt.site";
export const MAX_BODY_BYTES = 768 * 1024;
export const CSP = "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self'; connect-src 'self' https://huggingface.co https://*.huggingface.co https://*.hf.co https://raw.githubusercontent.com; worker-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'";
const HEADERS = {"Content-Type":"application/json; charset=utf-8", "Cache-Control":"no-store", "Content-Security-Policy":CSP,
  "X-Content-Type-Options":"nosniff", "Referrer-Policy":"no-referrer", "X-Frame-Options":"DENY"};
const ANALYZE_FIELDS = new Set(["csv_text","source_name","column_map","units","profile","current_convention","pack_id","mode","question","api_key","model","max_turns","timeout_s","max_output_tokens","max_total_tokens"]);
const WINDOW_FIELDS = new Set(["csv_text","source_name","column_map","units","profile","current_convention","pack_id","start_s","end_s","max_samples"]);
const buckets = new Map();
let activeRequests = 0;

export class RequestError extends Error { constructor(status, code, message) { super(message); this.status=status; this.code=code; } }
function response(status, value) { return new Response(JSON.stringify(value), {status,headers:HEADERS}); }
function fail(status, code, message) { throw new RequestError(status,code,message); }

export function parseJsonStrict(text) {
  // Bound nesting/nodes and reject duplicate/prototype keys before schema validation.
  let position=0, nodes=0;
  const whitespace=()=>{while(position<text.length&&/[ \t\r\n]/.test(text[position]))position++;};
  const quoted=()=>{
    const start=position++;
    while(position<text.length){if(text[position]==="\\"){position+=2;continue;}if(text[position++]==='"')return JSON.parse(text.slice(start,position));}
    throw new Error("Unterminated JSON string");
  };
  const value=(depth)=>{
    if(depth>20||++nodes>10000)throw new Error("JSON complexity limit");
    whitespace();const char=text[position];
    if(char==='"')return quoted();
    if(char==='{'||char==='['){
      position++;whitespace(); const object=char==='{'?Object.create(null):[]; const end=char==='{'?'}':']'; const keys=new Set();
      if(text[position]===end){position++;return object;}
      while(position<text.length){
        whitespace(); if(char==='{'){
          if(text[position]!=='"')throw new Error("JSON key required");
          const key=quoted(); if(keys.has(key)||["__proto__","constructor","prototype"].includes(key))throw new Error("Invalid or duplicate JSON key");keys.add(key);
          whitespace();if(text[position++]!==':')throw new Error("JSON colon required");object[key]=value(depth+1);
        }else object.push(value(depth+1));
        whitespace();const delimiter=text[position++];if(delimiter===end)return object;if(delimiter!==',')throw new Error("JSON delimiter required");
      }throw new Error("Unterminated JSON container");
    }
    const match=/^(?:true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)/.exec(text.slice(position));
    if(!match)throw new Error("Invalid JSON value");position+=match[0].length;const result=JSON.parse(match[0]);
    if(typeof result==='number'&&!Number.isFinite(result))throw new Error("Nonfinite JSON number");return result;
  };
  const result=value(0);whitespace();if(position!==text.length||!result||Array.isArray(result)||typeof result!=="object")throw new Error("JSON object required");return result;
}

export function validateRequest(request, operation) {
  const url=new URL(request.url);
  if(url.search)fail(400,"query_denied","API路径不能携带查询参数。");
  if(request.method!=="POST")fail(405,"method_denied","仅允许POST请求。");
  const local=["127.0.0.1","localhost"].includes(url.hostname);
  const expected=local?url.origin:PUBLIC_ORIGIN;
  if(request.headers.get("origin")!==expected)fail(403,"origin_denied","仅接受本站页面发起的请求。");
  if(request.headers.get("x-bms-request")!=="1")fail(403,"header_required","缺少本站请求标识。");
  if(request.headers.get("content-type")?.split(";",1)[0].trim().toLowerCase()!=="application/json")fail(415,"json_required","仅接受JSON请求。");
  const length=request.headers.get("content-length");
  if(length!==null&&(!/^\d+$/.test(length)||Number(length)>MAX_BODY_BYTES))fail(413,"body_too_large","请求超过公开版大小限制。");
  if(!["analyze","window"].includes(operation))fail(404,"not_found","接口不存在。");
}

export async function readPayload(request, operation) {
  const reader=request.body?.getReader();if(!reader)fail(400,"empty_body","需要上传CSV日志。");
  let size=0; const chunks=[]; let timer;
  try{
    await Promise.race([ (async()=>{while(true){const next=await reader.read();if(next.done)break;size+=next.value.byteLength;
      if(size>MAX_BODY_BYTES)fail(413,"body_too_large","请求超过公开版大小限制。");chunks.push(next.value);}})(),
      new Promise((_,reject)=>{timer=setTimeout(()=>reject(new RequestError(408,"body_timeout","上传超时，请重试。")),10000);}) ]);
  }finally{clearTimeout(timer);await reader.cancel().catch(()=>{});}
  const bytes=new Uint8Array(size);let offset=0;for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}
  let payload;try{payload=parseJsonStrict(new TextDecoder("utf-8",{fatal:true}).decode(bytes));}catch{fail(422,"invalid_json","需要有效、无重复字段的UTF-8 JSON对象。");}
  const allowed=operation==="analyze"?ANALYZE_FIELDS:WINDOW_FIELDS;
  if(Object.keys(payload).some(key=>!allowed.has(key)))fail(422,"invalid_fields","请求包含不支持的字段。");
  if(typeof payload.csv_text!=="string"||!payload.csv_text)fail(422,"csv_required","需要UTF-8 CSV日志。");
  if(new TextEncoder().encode(payload.csv_text).length>512*1024)fail(413,"csv_too_large","公开版CSV上限512KiB；大日志请用本地版。");
  if(operation==="analyze"){
    payload.mode??="offline";
    if(!["offline","agent"].includes(payload.mode))fail(422,"invalid_mode","分析模式无效。");
    if(payload.mode==="offline"){delete payload.api_key;delete payload.model;}
    else if(typeof payload.api_key!=="string"||!payload.api_key.trim()||payload.api_key.length>1024||typeof payload.model!=="string"||!payload.model.trim()||payload.model.length>256)
      fail(422,"byok_required","Agent模式必须显式提供本次自己的Key和模型ID。");
  }
  return payload;
}

export function acquireSlot(request, now=Date.now()) {
  // Best-effort, per isolate only: no claim of global or durable rate limiting.
  const key=request.headers.get("cf-connecting-ip")||"anonymous";
  for(const [id,bucket] of buckets)if(now-bucket.start>=60000)buckets.delete(id);
  if(!buckets.has(key)){if(buckets.size>=1024)buckets.delete(buckets.keys().next().value);buckets.set(key,{start:now,count:0});}
  const bucket=buckets.get(key);
  if(bucket.count>=20||activeRequests>=2)fail(429,"busy","请求过于频繁或当前繁忙，请稍后重试。");
  bucket.count++;activeRequests++;let released=false;return()=>{if(!released){activeRequests--;released=true;}};
}

export async function handleRequest(request, operation, callbacks={}) {
  let release=()=>{};
  try{
    validateRequest(request,operation);release=acquireSlot(request);
    const payload=await readPayload(request,operation);
    const service=callbacks.analyze&&callbacks.window?callbacks:await import("./service.mjs");
    const callback=operation==="analyze"?(callbacks.analyze||service.analyzeRequest):(callbacks.window||service.inspectRequest);
    const result=await callback(payload);
    const serialized=JSON.stringify(result,(key,value)=>{
      if(["api_key","authorization","openai_api_key"].includes(key.toLowerCase()))return undefined;
      if(typeof value==="number"&&!Number.isFinite(value))throw new Error("Nonfinite response");
      if(typeof value==="string"){if(payload.api_key)value=value.split(payload.api_key).join("[REDACTED]");return value.replace(/sk-[A-Za-z0-9_-]{8,}/g,"[REDACTED]");}return value;});
    return new Response(serialized,{status:200,headers:HEADERS});
  }catch(error){
    if(error instanceof RequestError)return response(error.status,{error:{code:error.code,message:error.message}});
    return response(error instanceof TypeError||error?.name==="ValueError"?422:500,{error:{code:"analysis_failed",message:"分析未完成；请检查CSV、列映射、单位、规则及公开版限制。内部异常不会回显。"}});
  }finally{release();}
}

export function healthResponse(){return response(200,{status:"ok",public:true,byok:true,default_mode:"offline",default_ui_mode:"browser",free_model:"Qwen3-1.7B",free_inference:"browser_only",data_persistence:false});}
