import {build} from 'esbuild';
import {mkdir,readFile,writeFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
const root=fileURLToPath(new URL('../',import.meta.url));
const outdir=fileURLToPath(new URL('../public/assets/local-built/',import.meta.url));
await mkdir(outdir,{recursive:true});
const result=await build({absWorkingDir:root,entryPoints:{entry:'browser/entry.mjs','engine-worker':'browser/engine-worker.mjs'},
  outdir,bundle:true,splitting:true,format:'esm',platform:'browser',target:'es2022',minify:true,legalComments:'eof',metafile:true,
  define:{'process.env.NODE_ENV':'"production"'},
  plugins:[{name:'no-paid-provider-in-browser',setup(b){b.onResolve({filter:/^\.\/runtime\.mjs$/},args=>
    args.resolveDir.replaceAll('\\','/').endsWith('/lib')?{path:root+'browser/paid-disabled.mjs'}:null);}}]});
// The SDK ships optional OpenAI providers in its public entrypoint; they must remain unreachable.
// Our application paid runtime is a stronger build-time exclusion, checked separately.
if(Object.keys(result.metafile.inputs).some(p=>/(^|\/)lib\/runtime\.mjs$/.test(p.replaceAll('\\','/'))))
  throw new Error('Paid application runtime leaked into browser bundle');
const license=await readFile(new URL('../node_modules/@mlc-ai/web-llm/LICENSE',import.meta.url),'utf8').catch(()=> 'See https://github.com/mlc-ai/web-llm/blob/main/LICENSE');
await writeFile(outdir+'WEBLLM-LICENSE.txt',license);
await writeFile(outdir+'build-info.json',JSON.stringify({model:'Qwen3-1.7B',webllm:'0.2.85',paid_application_runtime:false,
  entries:['entry.js','engine-worker.js'],sdk:'0.20.0'})+'\n');
console.log('Browser local inference bundles built; paid application runtime excluded.');
