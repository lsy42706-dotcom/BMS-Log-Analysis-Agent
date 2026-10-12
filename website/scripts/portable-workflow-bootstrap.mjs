/** Apply only the identified Windows packager compatibility repair in this process. */
import childProcess from 'node:child_process';
import {syncBuiltinESMExports} from 'node:module';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const nativeSpawn=childProcess.spawn;
const original='C:/Users/l/.codex/plugins/cache/openai-curated-remote/sites/1.0.1/scripts/package-site.mjs';
const adapter=fileURLToPath(new URL('./package-portable.mjs',import.meta.url));
childProcess.spawn=function(executable,args,options){
  if(process.platform==='win32'&&Array.isArray(args)&&typeof args[0]==='string'&&path.resolve(args[0])===path.resolve(original)){
    return nativeSpawn.call(this,executable,[adapter,...args.slice(1)],options);
  }
  return nativeSpawn.call(this,executable,args,options);
};
syncBuiltinESMExports();
