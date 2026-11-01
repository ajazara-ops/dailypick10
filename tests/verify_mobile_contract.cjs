// Real app parsers, request loader and HistoryReport; native views/fetch are local substitutes.
// Usage: node verify_mobile_contract.cjs <StockApp-or-mobile> <export_weekly_contract-output>
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const Module=require('node:module'),ts=require('typescript');
const app=path.resolve(process.argv[2]),folder=path.resolve(process.argv[3]);
const appRequire=Module.createRequire(path.join(app,'package.json'));
const React=appRequire('react'),{renderToStaticMarkup}=appRequire('react-dom/server');
for(const ext of ['.ts','.tsx'])require.extensions[ext]=(module,file)=>module._compile(ts.transpileModule(fs.readFileSync(file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}}).outputText,file);
const domain=require(path.join(app,'src/domain/recommendations.ts'));
const {weeklySummary}=require(path.join(app,'src/domain/weekly.ts'));
const {loadJson}=require(path.join(app,'src/domain/requests.ts'));
const originalLoad=Module._load,primitive=({children})=>React.createElement('span',null,children);
Module._load=function(name,parent,isMain){
  if(name==='react-native')return {View:primitive,Text:primitive,TouchableOpacity:primitive,ScrollView:primitive,Modal:primitive,Alert:{alert(){}}};
  if(name==='react-native-safe-area-context')return {SafeAreaView:primitive};
  if(name==='expo-web-browser')return {};
  if(name==='./Icons')return {Icon:()=>null};
  if(name==='../styles')return {styles:{}};
  if(name==='../text')return {isKorean:true,t:{us:'미국',kr:'한국'}};
  return originalLoad.call(this,name,parent,isMain);
};
const {HistoryReport}=require(path.join(app,'src/components/RecommendationViews.tsx'));
Module._load=originalLoad;
const base='https://ajazara-ops.github.io/dailypick10/';
const appSource=fs.readFileSync(path.join(app,'app/index.tsx'),'utf8');
assert.ok(appSource.includes(`const DATA_BASE = "${base}"`));
assert.ok(appSource.includes('history_index.json'));
const cases=JSON.parse(fs.readFileSync(path.join(folder,'cases.json'),'utf8'));
const savedNow=Date.now,savedFetch=global.fetch;
let assertions=0;
function equal(a,b){assert.deepEqual(a,b);assertions++;}
async function main(){
 for(const [name,item] of Object.entries(cases)){
  const root=path.join(folder,name),now=Date.parse(item.now),requests=[];
  // Never delegate to the machine's fetch, including unexpected URL cases.
  global.fetch=async url=>{
   const parsed=new URL(url);equal(parsed.origin,'https://ajazara-ops.github.io');
   assert.ok(parsed.pathname.startsWith('/dailypick10/'));assertions++;
   const relative=parsed.pathname.slice('/dailypick10/'.length),local=path.resolve(root,relative);
   assert.ok(local.startsWith(root+path.sep));assertions++;
   requests.push(relative);
   const data=JSON.parse(fs.readFileSync(local,'utf8'));
   return {ok:true,json:async()=>data};
  };
  Date.now=()=>now;
  const indexRaw=await loadJson(base+'history_index.json',new AbortController().signal);
  equal(domain.parseHistoryIndex(indexRaw,new Date(now-1)),[]);
  const index=domain.parseHistoryIndex(indexRaw,new Date(now));equal(index.length,1);
  equal(index[0],{date:item.publication,file:item.file});
  const raw=await loadJson(domain.historyUrl(index[0].file,base),new AbortController().signal);
  const parsed=domain.parseRecommendation(raw,now);
  equal(parsed.invalidCount,0);
  for(const market of ['US','KR']){
   const result=weeklySummary(parsed,market,now),expected=raw.performance[market];
   equal(result.state,'ready');equal(result.totalCount,expected.totalCount);
   equal(result.observedCount,expected.observedCount);equal(result.missingCount,expected.missingCount);
   equal(result.evaluationSession,expected.evaluationQuoteAsOf);
   equal(result.winners.map(s=>s.recommendationId),raw.winnerRankings[market]);
   equal(weeklySummary(parsed,market,now-1).winners,[]);
  }
  if(name==='normal'){
   const a04=parsed.stocks.find(s=>s.id==='A04');equal(a04.buyPrice,100);equal(a04.returnRate,4);
   equal(parsed.stocks.find(s=>s.id==='A00').returnRate,0);
   equal(parsed.stocks.find(s=>s.id==='A12').returnRate,null);
   equal(parsed.stocks.find(s=>s.id==='A13').returnStatus,'corporate_action_requires_adjustment');
   equal(parsed.stocks.find(s=>s.id==='A11').recommendDate,'2026-11-06');
   equal(weeklySummary(parsed,'US',now).winners.length,10);
  }
  const html=renderToStaticMarkup(React.createElement(HistoryReport,{data:parsed}));
  for(const text of ['주간 상승률 TOP 10',raw.period.weekStart,raw.period.weekEnd,raw.period.evaluationDate,item.publication,'토요일 17:00 KST','첫 추천','평가 종가']){assert.ok(html.includes(text),text);assertions++;}
  fs.writeFileSync(path.join(root,'actual-HistoryReport-render.html'),html);
  Date.now=()=>now-1;
  const pending=renderToStaticMarkup(React.createElement(HistoryReport,{data:parsed}));
  assert.ok(pending.includes('공개 대기'));assertions++;
  equal(requests,['history_index.json',item.file]);
 }
 const result={app,cases:Object.keys(cases).length,assertions,passed:true,
  scope:'Actual app source parsers/loader/HistoryReport; synthetic producer files; substituted native views and fetch. No native device or live server call.'};
 fs.writeFileSync(path.join(folder,'mobile-contract-result.json'),JSON.stringify(result,null,2));
 console.log(JSON.stringify(result));
}
main().catch(error=>{console.error(error);process.exitCode=1;}).finally(()=>{Date.now=savedNow;global.fetch=savedFetch;});
