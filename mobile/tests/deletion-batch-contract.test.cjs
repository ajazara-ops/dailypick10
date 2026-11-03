// Real TS client/parser/UI copy + synthetic server outputs. No SDK/HTTP operation.
const {test,after}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),ts=require('typescript'),Module=require('node:module');
let networkAttempts=0;
const forbidden=()=>{networkAttempts++;throw Error('External connection forbidden');};
global.fetch=forbidden;
require('node:net').Socket.prototype.connect=forbidden;
require('node:http').request=forbidden;require('node:https').request=forbidden;
require.extensions['.ts']=(m,f)=>m._compile(ts.transpileModule(fs.readFileSync(f,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,f);
require.extensions['.tsx']=(m,f)=>m._compile(ts.transpileModule(fs.readFileSync(f,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.React}}).outputText,f);
const oldLoad=Module._load;
Module._load=function(name,...rest){
  if(name==='firebase/firestore/lite')return {doc:forbidden,getDoc:forbidden,runTransaction:forbidden,serverTimestamp:forbidden};
  if(name==='react-native')return {View:'View',Text:'Text',TouchableOpacity:'Button'};
  return oldLoad.call(this,name,...rest);
};
const {sparkReceiptFromBatchGet,createPreparedSparkDeletionTransport}=require('../src/services/sparkDeletionTransport.ts');
const {createDeletionClient}=require('../src/domain/deletion.ts');
const {deletionMessage}=require('../src/components/DeletionStatus.tsx');
Module._load=oldLoad;
const data=JSON.parse(fs.readFileSync(path.join(__dirname,'../../synthetic-output/deletion-contract.json'),'utf8'));
assert.equal(data.syntheticOnly,true);
const receipt=name=>{const s=data.scenarios[name];return sparkReceiptFromBatchGet(s.response,s.documentName);};
const uid='synthetic-member',id='00000000-0000-4000-8000-000000000001';
function transport(current){return {mode:'mock',supportsRecoveryStatus:false,load:async()=>({uid,requestId:id,recoveryKey:'a'.repeat(64)}),
  current:async()=>current(),status:forbidden,submit:forbidden,save:forbidden,prepare:forbidden,cancel:forbidden};}
test('actual batch outputs map to all six client states with an unchanged seven-day server deadline',()=>{
  const saved=Date.now;Date.now=()=>{throw Error('Device clock must not determine deadline');};
  try{for(const name of ['grace_period','queued','retry_wait','completed','cancel_requested','cancelled']){
    const r=receipt(name);assert.equal(r.status,name);assert.equal(r.deleteAfter-r.requestedAt,604800);
    assert.equal(r.subscriptionCancelled,false);
    assert.equal(r.cancelAllowed,name==='grace_period');
  }}finally{Date.now=saved;}
});
test('relogin and status refresh never cancel or extend an accepted request',async()=>{
  const t=transport(()=>receipt('grace_period'));const c=createDeletionClient(t,()=>uid);await c.initialize();
  const initial=c.snapshot().receipt;await c.refresh();assert.deepEqual(c.snapshot().receipt,initial);
  assert.equal(c.canUseService(uid),false);
});
test('pending cancellation stays blocked until matching authenticated server confirmation',async()=>{
  let status='grace_period';const t=transport(()=>receipt(status));t.cancel=async()=>{status='cancel_requested';return receipt(status);};
  const c=createDeletionClient(t,()=>uid);await c.initialize();await c.cancel(async()=>true);
  assert.equal(c.snapshot().status,'cancel_requested');assert.equal(c.canUseService(uid),false);
  await c.refresh();assert.equal(c.canUseService(uid),false);
  status='cancelled';await c.refresh();assert.equal(c.canUseService(uid),true);
});
test('reauthentication cancellation and failure never submit an unintended delete request',async()=>{
  let submissions=0;const t=transport(()=>null);t.load=async()=>null;t.submit=async()=>{submissions++;throw Error('not called');};
  const c=createDeletionClient(t,()=>uid);await c.initialize();
  await c.request(async()=>false);assert.equal(c.snapshot().status,'idle');
  await c.request(async()=>{throw Error('synthetic revoked');});assert.equal(submissions,0);assert.equal(c.snapshot().status,'idle');
});
test('deadline race becomes blocked unknown then authoritative queued, never cancelled',async()=>{
  let phase='grace_period';const t=transport(()=>receipt(phase));t.cancel=async()=>{phase='queued';throw Error('cutoff reached');};
  const c=createDeletionClient(t,()=>uid);await c.initialize();await c.cancel(async()=>true);
  assert.equal(c.snapshot().status,'unknown');assert.equal(c.canUseService(uid),false);
  await c.refresh();assert.equal(c.snapshot().status,'queued');assert.equal(c.canUseService(uid),false);
});
test('partial cleanup receipt is retry_wait while unavailable authentication remains unknown after deletion',async()=>{
  assert.equal(data.scenarios.retry_wait.authExists,false);assert.equal(data.scenarios.completed.authExists,false);
  assert.match(deletionMessage({mode:'mock',status:receipt('retry_wait').status}),/재시도/);
  for(const error of ['offline','revoked','deleted']){
    const c=createDeletionClient(transport(()=>{throw Error(error);}),()=>uid);await c.initialize();
    assert.equal(c.snapshot().status,'unknown');assert.equal(c.canUseService(uid),false);
    assert.match(deletionMessage(c.snapshot()),/완료.*판단하지/);
  }
});
test('completion needs server evidence; configured app still uses disabled default',async()=>{
  const r=receipt('completed');assert.equal(r.phase,'complete');assert.equal(r.completedScope.length,2);
  const broken=structuredClone(data.scenarios.completed);delete broken.response[0].found.fields.completedScope;
  assert.throws(()=>sparkReceiptFromBatchGet(broken.response,broken.documentName));
  const t=createPreparedSparkDeletionTransport({get auth(){throw Error('No SDK access');},get db(){throw Error('No SDK access');}});
  assert.equal(t.mode,'disabled');assert.equal(await t.load(),null);
});
after(()=>assert.equal(networkAttempts,0));
