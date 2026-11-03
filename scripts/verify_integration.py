"""Run affected deletion/Pages integration with explicit, locally installed dependencies.

No download, dependency install, live credential binding, emulator or deployment.
Use Python 3.13.15. Node 24.22.1 and this checkout's node_modules are required.
"""
from pathlib import Path
import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
NAMES=['test_spark_sdk_connection','test_deletion_publication_integration',
       'test_publication_runtime','test_pages_runtime']
NODE_PREFLIGHT=r'''
const fs=require('node:fs'),path=require('node:path');
const root=fs.realpathSync(process.cwd()),lock=require(path.join(root,'package-lock.json'));
for(const name of ['typescript','react']){
  const base=fs.realpathSync(path.join(root,'node_modules',name));
  if(!base.startsWith(root+path.sep))throw Error('Dependency must be inside this checkout');
  const manifest=require(path.join(base,'package.json'));
  if(manifest.version!==lock.packages['node_modules/'+name].version)throw Error('Locked dependency version differs');
  const resolved=fs.realpathSync(require.resolve(name));
  if(!resolved.startsWith(base+path.sep))throw Error('External module resolution forbidden');
}
console.log(JSON.stringify({node:process.version,dependencies:'checkout-local',nodePathUsed:false}));
'''

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', default='node', help='Existing Node executable; never installed by this script')
    parser.add_argument('--preflight-only', action='store_true')
    args=parser.parse_args()
    if sys.version_info[:3]!=(3,13,15):
        raise SystemExit('This package was validated with Python 3.13.15; no installation attempted.')
    versions={}
    for line in (ROOT/'requirements-integration.lock').read_text().splitlines():
        match=re.match(r'([A-Za-z0-9_-]+)==([^ ]+)',line)
        if match:
            name,version=match.groups()
            if importlib.metadata.version(name)!=version:
                raise SystemExit('Installed dependency differs from requirements-integration.lock: '+name)
            versions[name]=version
    # Do not inherit SDK endpoints, credentials, NODE_PATH, Python module overrides
    # or arbitrary app settings. Only OS process essentials are passed to children.
    env={k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR','PATH','PATHEXT','TEMP','TMP'}}
    env.update({'PYTHONIOENCODING':'utf-8','PYTHONNOUSERSITE':'1','NODE_OPTIONS':'--no-global-search-paths'})
    node=subprocess.run([args.node,'--version'],env=env,cwd=ROOT,check=True,capture_output=True,text=True).stdout.strip()
    if node!='v24.22.1': raise SystemExit('Node 24.22.1 required for this recorded reproduction; no installation attempted.')
    subprocess.run([args.node,'-e',NODE_PREFLIGHT],env=env,cwd=ROOT,check=True)
    print(json.dumps({'python':'.'.join(map(str,sys.version_info[:3])),'packages':versions,'downloads':0}))
    if args.preflight_only: return 0
    evidence=ROOT/'local-review-results';evidence.mkdir(exist_ok=True)
    commands=[('server',[sys.executable,'-I',str(ROOT/'scripts/test_session_publication.py'),*NAMES]),
              ('app',[args.node,'--test','--test-concurrency=1',str(ROOT/'mobile/tests/deletion-batch-contract.test.cjs')])]
    results={}
    for label,command in commands:
        run=subprocess.run(command,cwd=ROOT,env=env,capture_output=True,text=True,encoding='utf-8',errors='strict')
        output=run.stdout+'\n'+run.stderr
        (evidence/(label+'.log')).write_text(output,encoding='utf-8')
        results[label]={'exitCode':run.returncode,'logSha256':hashlib.sha256(output.encode()).hexdigest()}
        if run.returncode:
            print(label+' failed; inspect local-review-results/'+label+'.log')
            (evidence/'result.json').write_text(json.dumps(results,indent=2)+'\n')
            return run.returncode
    summary=json.loads((ROOT/'publication-test-results.json').read_text())
    assert summary['testsRun']==57 and summary['success'] and summary['blockedExternalAttempts']==0
    app=(evidence/'app.log').read_text(encoding='utf-8')
    assert re.search(r'\btests 7\b',app) and re.search(r'\bpass 7\b',app) and re.search(r'\bfail 0\b',app)
    results.update({'serverCount':57,'appCount':7,'sameSuiteReproducedForPackaging':False,
                    'newServerTests':9,'updatedServerTests':2,'unchangedServerRegressionTests':46,
                    'unchangedAppContractTests':7,
                    'dependencies':'explicit locked packages; Node checkout-local',
                    'emulatorUsed':False,'productionServicesUsed':False})
    (evidence/'result.json').write_text(json.dumps(results,indent=2)+'\n')
    print(json.dumps(results));return 0

if __name__=='__main__':raise SystemExit(main())
