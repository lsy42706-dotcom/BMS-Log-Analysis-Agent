/** Windows adapter for the supplied Sites packager, preserving its preparation checks. */
import fs from 'node:fs';
import path from 'node:path';
import {execFileSync} from 'node:child_process';
import {isDeepStrictEqual} from 'node:util';
const [projectArg,archiveArg]=process.argv.slice(2);
if(!projectArg||!archiveArg||!path.isAbsolute(projectArg)||!path.isAbsolute(archiveArg))throw new Error('Absolute project and archive required');
const project=fs.realpathSync(projectArg);const archive=path.resolve(archiveArg);
const runtime=path.join(project,'.sites-runtime');fs.mkdirSync(runtime,{recursive:true});
if(!archive.startsWith(runtime+path.sep))throw new Error('Archive must stay in the ignored local runtime directory');
const stage=fs.mkdtempSync(path.join(runtime,'package-stage-'));
const pluginPrepare='C:/Users/l/.codex/plugins/cache/openai-curated-remote/sites/1.0.1/skills/sites/scripts/prepare-site-build.cjs';
try{
  const kind=execFileSync(process.execPath,[pluginPrepare,project,path.join(stage,'dist')],{encoding:'utf8'}).trim();
  const source=JSON.parse(fs.readFileSync(path.join(project,'.openai/hosting.json'),'utf8'));
  const builtFile=path.join(project,'dist/.openai/hosting.json');
  const built=fs.existsSync(builtFile)?JSON.parse(fs.readFileSync(builtFile,'utf8')):{};
  if(source.artifact_metadata!=null&&built.artifact_metadata!=null&&!isDeepStrictEqual(source.artifact_metadata,built.artifact_metadata))throw new Error('Conflicting build attribution');
  const metadata=path.join(stage,'dist/.openai');fs.mkdirSync(metadata,{recursive:true});
  const staged=kind==='worker'?source:JSON.parse(fs.readFileSync(path.join(metadata,'hosting.json'),'utf8'));
  const attribution=built.artifact_metadata??source.artifact_metadata;if(attribution!=null)staged.artifact_metadata=attribution;
  fs.writeFileSync(path.join(metadata,'hosting.json'),JSON.stringify(staged,null,2)+'\n');
  if(fs.existsSync(path.join(project,'drizzle')))fs.cpSync(path.join(project,'drizzle'),path.join(metadata,'drizzle'),{recursive:true});
  execFileSync('C:/Windows/System32/tar.exe',['-C',stage,'-czf',archive,'dist'],{stdio:'inherit'});
  const entries=execFileSync('C:/Windows/System32/tar.exe',['-tzf',archive],{encoding:'utf8'}).split(/\r?\n/);
  if(!entries.includes('dist/.openai/hosting.json')||(kind==='worker'&&!entries.includes('dist/server/index.js')))throw new Error('Archive entry verification failed');
  console.log(archive);
}finally{
  const exact=fs.realpathSync(stage);
  if(exact.startsWith(runtime+path.sep)&&path.basename(exact).startsWith('package-stage-'))fs.rmSync(exact,{recursive:true,force:true});
}
