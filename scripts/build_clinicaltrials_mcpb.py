"""Build a standalone native ClinicalTrials candidate with its own Node 24."""
from pathlib import Path
import argparse,base64,hashlib,io,json,shutil,subprocess,tarfile,urllib.request,zipfile

VERSION='2.11.3'
NODE='24.22.3'
NODE_INTEGRITY='sha512-KHsob+Fv9YerATvYujMbiyJqnRCvLtrcr/eTNqRqiOSGp97EOiRP1yP47+knGgQ/xnc3XY8xzkW587dlZzNqRA=='

def build(lock_dir, output, staging):
    staging.mkdir(parents=True,exist_ok=True)
    server=staging/'server';server.mkdir(exist_ok=True)
    for name in ['package.json','package-lock.json']:shutil.copy2(lock_dir/name,server/name)
    subprocess.run(['npm','ci','--prefix',str(server),'--omit=dev','--ignore-scripts','--no-audit','--no-fund'],check=True)
    archive=urllib.request.urlopen(f'https://registry.npmjs.org/node-linux-x64/-/node-linux-x64-{NODE}.tgz',timeout=60).read()
    assert 'sha512-'+base64.b64encode(hashlib.sha512(archive).digest()).decode()==NODE_INTEGRITY
    with tarfile.open(fileobj=io.BytesIO(archive),mode='r:gz') as tar:binary=tar.extractfile('package/bin/node').read()
    binpath=staging/'bin/node';binpath.parent.mkdir(exist_ok=True);binpath.write_bytes(binary);binpath.chmod(0o755)
    (staging/'launch.cjs').write_text(''''use strict';
const {join}=require('node:path');
const {chmodSync}=require('node:fs');
const {spawn}=require('node:child_process');
const executable=join(__dirname,'bin/node');
chmodSync(executable,0o755);
const child=spawn(executable,[join(__dirname,'server/node_modules/clinicaltrialsgov-mcp-server/dist/index.js'),...process.argv.slice(2)],{
 stdio:'inherit',env:{...process.env,MCP_TRANSPORT_TYPE:'stdio'}
});
child.on('error',()=>{process.stderr.write('ClinicalTrials native runtime could not start\\n');process.exitCode=1});
for(const signal of ['SIGINT','SIGTERM'])process.on(signal,()=>child.kill(signal));
child.on('exit',(code,signal)=>{if(signal)process.kill(process.pid,signal);else process.exitCode=code??1});
''')
    runtime={'kind':'node','source':'clinicaltrialsgov-mcp-server@'+VERSION,'entrypoint':'dist/index.js','nodeVersion':NODE,'executables':['bin/node']}
    manifest={'manifest_version':'0.3','name':'clinicaltrialsgov-mcp-server','version':VERSION+'+node.'+NODE,
      'description':'Native ClinicalTrials.gov 2.11.3 candidate, standalone Linux x86_64 with Node 24. No formatter or schema patches.',
      'author':{'name':'MCP-Atlas'},'server':{'type':'node','entry_point':'launch.cjs','mcp_config':{
       'command':'node','args':['${__dirname}/launch.cjs'],'env':{'MCP_TRANSPORT_TYPE':'stdio','MCP_LOG_LEVEL':'info'}}}}
    provenance={'platform':'linux-x86_64','server':'clinicaltrialsgov-mcp-server','runtime':runtime,
      'upstreamVersion':VERSION,'nativePackageUnmodified':True,'nodeDistributionIntegrity':NODE_INTEGRITY,
      'nodeBinarySha256':hashlib.sha256(binary).hexdigest(),
      'packageLockSha256':hashlib.sha256((server/'package-lock.json').read_bytes()).hexdigest()}
    for name,value in [('manifest.json',manifest),('runtime.json',runtime),('SOURCE.json',provenance)]:
      (staging/name).write_text(json.dumps(value,indent=2)+'\n')
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
      for path in sorted(staging.rglob('*')):
        if path.is_file() and '.bin' not in path.parts:z.write(path,path.relative_to(staging))
    with zipfile.ZipFile(output) as z:assert z.testzip() is None
    print(output,output.stat().st_size)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--lock-dir',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--staging',type=Path,required=True);a=p.parse_args();build(a.lock_dir,a.output,a.staging)
