const { JSDOM } = require("jsdom"); const fs = require("fs");
const { indexedDB } = require("fake-indexeddb");
const html = fs.readFileSync(process.env.APP_HTML || require("path").join(__dirname, "..", "integrated-biz-system.html"), "utf-8");
const KEY = "integratedBizSystem_v1";
const res=[]; const ck=(l,a,e)=>{ const p=(typeof e==="number")?a===e:(JSON.stringify(a)===JSON.stringify(e)); res.push({l,a,e,p}); };
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
function boot(preload){
  const dom=new JSDOM(html,{runScripts:"dangerously",pretendToBeVisual:true,url:"https://example.com/t.html",
    beforeParse(w){ w.indexedDB=indexedDB; w.TextEncoder=TextEncoder; w.TextDecoder=TextDecoder; w.Response=Response; w.CompressionStream=CompressionStream; w.DecompressionStream=DecompressionStream; w.structuredClone=structuredClone; if(preload) preload(w.localStorage); }});
  const win=dom.window; win.__noAutoBackup=true;
  const ctx={win, doc:win.document, alerts:[], confirmAnswer:true, promptAnswer:"テスト理由", errors:[]};
  win.alert=m=>ctx.alerts.push(String(m)); win.confirm=()=>ctx.confirmAnswer; win.print=()=>{}; win.prompt=()=>ctx.promptAnswer;
  win.addEventListener("error",e=>ctx.errors.push(e.message+" @"+e.lineno));
  ctx.ev=c=>win.eval(c);
  return ctx;
}
(async()=>{
  // =============== S1. 旧形式(v1)の読込時移行 ===============
  const legacy = {
    settings:{company:{name:"旧社"}, taxRates:[{id:"t1",rate:10,effectiveFrom:"2019-10-01",label:"標準"}], clients:[{id:"c1",name:"旧取引先"}], sites:[], expenseItems:[]},
    manHours:{
      "2026-09":{rows:[{content:"現場A",daily:{1:5,2:5}},{content:"残業",daily:{1:"2H",2:"1.5H"}}], carry:0},
      "2026-10":{rows:[{content:"現場A",daily:{1:3}},{content:"残業",daily:{1:"1H"}}], carry:12}
    },
    invoices:{"2026-10":[{id:"i1",clientId:"c1",date:"2026-10-20",no:"KD202610-001",taxRateId:"t1",status:"未収",items:[{site:"現場A",content:"x",qty:"1",unit:"m2x",price:"1000"}]}]},
    labels:[], auditLog:[{time:"2026/10/2 10:00:00",action:"新しい"},{time:"2026/10/1 09:00:00",action:"古い"}]
  };
  const legacyRaw = JSON.stringify(legacy);
  let c = boot(ls=>ls.setItem(KEY, legacyRaw)); await sleep(400);
  ck("v1→v2：データ形式が2になる", c.ev("DATA.schemaVersion"), 2);
  ck("v1→v2：残業行に種別が付く", c.ev(`DATA.manHours["2026-09"].rows[1].kind+","+DATA.manHours["2026-10"].rows[1].kind`), "overtime,overtime");
  ck("v1→v2：残業累計を連鎖して補う（0→3.5）", c.ev(`DATA.manHours["2026-09"].otCarry+","+DATA.manHours["2026-10"].otCarry`), "0,3.5");
  ck("v1→v2：旧ログは古い順に並び連鎖が正常", c.ev(`DATA.auditLog[0].action+","+verifyAuditChain().ok`), "古い,true");
  ck("v1→v2：移行がログに記録", c.ev("DATA.auditLog.slice(-1)[0].action").includes("データ形式を v1 → v2 に移行"), true);
  ck("v1→v2：請求書は補完される（支払・発行状態）", c.ev(`DATA.invoices["2026-10"][0].payments.length===0 && !DATA.invoices["2026-10"][0].issued`), true);
  ck("v1→v2：移行履歴が残る", c.ev("DATA.migrationHistory.length"), 1);
  ck("v1→v2：移行前データの控えが保存される", c.win.localStorage.getItem(KEY+"_premigration_v1"), legacyRaw);
  ck("v1→v2：移行後データが保存される", JSON.parse(c.win.localStorage.getItem(KEY)).schemaVersion, 2);
  const warn = c.ev(`(function(){ const d=JSON.parse(${JSON.stringify(legacyRaw)}); return migrateData(d).report.warnings.length; })()`);
  ck("繰越の不一致（12 vs 10）を警告", warn, 1);
  // 冪等
  const again = c.ev("migrateData(JSON.parse(JSON.stringify(DATA))).report.steps.length");
  ck("最新形式に再度移行しても手順なし（冪等）", again, 0);
  ck("スキーマ画面にv2表示", c.ev(`renderSchemaPanel(), document.getElementById("sc-info").textContent`).includes("v2"), true);
  ck("エラーなし(S1)", c.errors.length, 0);

  // =============== S3. 新しすぎる形式は上書きしない ===============
  const newer = JSON.stringify({schemaVersion:99, settings:{}, note:"future"});
  c = boot(ls=>ls.setItem(KEY, newer)); await sleep(300);
  c.ev("save()"); c.ev("DATA.settings.sites.push('x'); save()");
  ck("新しい形式：保存を停止し、元データは無傷", c.win.localStorage.getItem(KEY), newer);
  ck("新しい形式：警告バナーを表示", c.doc.querySelector("main .note")?.textContent.includes("より新しいバージョン"), true);
  ck("新しい形式：自動バックアップも動かない", await c.ev("autoBackupTick('t',{force:true})").then(r=>r.skipped), "blocked");

  // =============== S4. 壊れたJSONは控えを残す ===============
  const broken = "{ これはJSONではない";
  c = boot(ls=>ls.setItem(KEY, broken)); await sleep(300);
  ck("壊れたデータ：控えが _corrupt に残る", c.win.localStorage.getItem(KEY+"_corrupt"), broken);
  ck("壊れたデータ：新規データで起動", c.ev("DATA.schemaVersion"), 2);

  // =============== S5/S6. 検証・チェックサム ===============
  c = boot(); await sleep(300);
  c.ev("loadSampleData(null)"); await sleep(100);
  const env = JSON.parse(c.ev("JSON.stringify(buildExportEnvelope())"));
  ck("エンベロープ：形式・版・チェックサム・ログ先頭", [env.format, env.schemaVersion, env.checksum.length, !!env.logHead&&env.logHead.hash.length], ["integratedBizSystem",2,64,64]);
  let p = JSON.parse(c.ev(`JSON.stringify((function(){ const r=parseBackupText(${JSON.stringify(JSON.stringify(env))}); return {ok:r.ok,mm:r.checksumMismatch,w:r.warnings.length,e:r.errors}; })())`));
  ck("正常なバックアップは読込可・チェックサム一致", [p.ok,p.mm], [true,false]);
  const tam = JSON.parse(JSON.stringify(env)); tam.data.settings.company.name="改ざん";
  p = JSON.parse(c.ev(`JSON.stringify((function(){ const r=parseBackupText(${JSON.stringify(JSON.stringify(tam))}); return {ok:r.ok,mm:r.checksumMismatch}; })())`));
  ck("内容を書き換えるとチェックサム不一致を検出", p.mm, true);
  p = JSON.parse(c.ev(`JSON.stringify((function(){ const r=parseBackupText(JSON.stringify(DATA)); return {ok:r.ok,legacy:!!r.meta.legacy,w:r.warnings.length}; })())`));
  ck("旧形式(生のJSON)も読込可・警告つき", [p.ok,p.legacy,p.w>0], [true,true,true]);
  const bad = (mut)=>JSON.parse(c.ev(`JSON.stringify((function(){ const d=JSON.parse(JSON.stringify(DATA)); (${mut})(d); const r=parseBackupText(JSON.stringify(d)); return {ok:r.ok,e:r.errors}; })())`));
  ck("不正：請求書の月キー", bad("d=>{ d.invoices['abc']=[]; }").ok, false);
  ck("不正：税率マスタが空", bad("d=>{ d.settings.taxRates=[]; }").ok, false);
  ck("不正：取引先の形式", bad("d=>{ d.settings.clients=[{id:1,name:2}]; }").ok, false);
  ck("不正：請求書にidがない", bad("d=>{ const k=Object.keys(d.invoices)[0]; d.invoices[k]=[{foo:1}]; }").ok, false);
  ck("不正：請求書が多すぎる", bad("d=>{ const k=Object.keys(d.invoices)[0]; d.invoices[k]=Array.from({length:5001},(_,i)=>({id:'x'+i})); }").ok, false);
  p = JSON.parse(c.ev(`JSON.stringify((function(){ const r=parseBackupText(JSON.stringify({schemaVersion:99,settings:{}})); return {ok:r.ok,e:r.errors}; })())`));
  ck("より新しい形式のバックアップは拒否", [p.ok, p.e[0].includes("より新しい")], [false,true]);
  p = JSON.parse(c.ev(`JSON.stringify((function(){ const r=parseBackupText("これはJSONではない"); return {ok:r.ok,e:r.errors.length}; })())`));
  ck("JSONでないファイルは拒否", p.ok, false);

  // =============== S8. スナップショット（IndexedDB・gzip） ===============
  const rec = await c.ev(`makeSnapshot("daily").then(r=>({id:r.id,compressed:r.compressed,size:r.size,bytes:r.bytes.length,checksum:r.checksum}))`);
  ck("スナップショット：gzip圧縮され小さくなる", [rec.compressed, rec.bytes < rec.size], [true,true]);
  ck("スナップショット：一覧に載る", (await c.ev("idbAllSnaps().then(a=>a.length)")), 1);
  ck("スナップショット：検証OK", (await c.ev(`idbGetSnap(${JSON.stringify(rec.id)}).then(r=>verifySnapshotRec(r)).then(v=>v.ok)`)), true);
  // 壊れたスナップショットを検出
  const badOk = await c.ev(`(async()=>{ const env=buildExportEnvelope(); env.data.settings.company.name="改ざん"; const {bytes,compressed}=await packText(JSON.stringify(env)); await idbPutSnap({id:"bad_1",createdAt:new Date().toISOString(),kind:"manual",bytes,compressed,size:1,checksum:env.checksum}); const r=await idbGetSnap("bad_1"); return (await verifySnapshotRec(r)).ok; })()`);
  ck("スナップショット：内容と合わないものを検出", badOk, false);
  await c.ev(`idbDelSnap("bad_1")`);

  // =============== S9. 保持ルール（純粋関数・独立計算で照合） ===============
  const now = new Date("2026-11-03T12:00:00"); const daily=[];
  for(let t=new Date("2024-05-01T12:00:00"); t<=now; t=new Date(t.getTime()+86400000)) daily.push({key:t.toISOString(), iso:t.toISOString()});
  const run = (items, polIn)=>JSON.parse(c.ev(`JSON.stringify((function(){ const r=computeBackupRetention(${JSON.stringify(items)}.map(x=>({key:x.key,date:new Date(x.iso)})), new Date(${JSON.stringify(now.toISOString())}), ${JSON.stringify(polIn)}); return {keep:r.keep.map(x=>x.key), remove:r.remove.map(x=>x.key), expired:r.expired.map(x=>x.key)}; })())`));
  const pol={dailyKeepDays:31,retentionYears:7};
  const r1=run(daily,pol);
  const cut=now.getTime()-31*86400000;
  const recent=daily.filter(x=>new Date(x.iso).getTime()>=cut).length;
  const olderByMonth={}; daily.filter(x=>new Date(x.iso).getTime()<cut).forEach(x=>{ const d=new Date(x.iso); const m=d.getFullYear()+"-"+d.getMonth(); if(!olderByMonth[m]||x.iso>olderByMonth[m]) olderByMonth[m]=x.iso; });
  const expectedKeep = recent + Object.keys(olderByMonth).length;
  ck("保持：直近31日は全件＋古い月は各月1件", r1.keep.length, expectedKeep);
  ck("保持：各月に残るのは月内で最後の1件", Object.values(olderByMonth).every(k=>r1.keep.includes(k)), true);
  ck("保持：保持+削除=全件（取りこぼしなし）", r1.keep.length+r1.remove.length+r1.expired.length, daily.length);
  ck("保持：直近の日次は削除されない", daily.filter(x=>new Date(x.iso).getTime()>=cut).every(x=>!r1.remove.includes(x.key)), true);
  ck("保持：7年以内は期限切れなし", r1.expired.length, 0);
  const oldItems=[]; for(let t=new Date("2018-01-01T12:00:00"); t<new Date("2019-06-01T12:00:00"); t=new Date(t.getTime()+86400000*3)) oldItems.push({key:t.toISOString(), iso:t.toISOString()});
  const r2=run(oldItems.concat(daily.slice(-5)),pol);
  ck("保持：7年超の月末分は「期限切れ」で報告のみ（自動削除なし）", r2.expired.length>0 && r2.expired.every(k=>!r2.remove.includes(k)), true);
  ck("保持：7年超でも月末以外の日次は整理対象", r2.remove.length>0, true);
  const r3=run(oldItems.concat(daily.slice(-5)),{dailyKeepDays:31,retentionYears:10});
  ck("保持年数を10年にすると期限切れは0", r3.expired.length, 0);

  // =============== S10. IndexedDB上での整理 ===============
  await c.ev(`(async()=>{ const all=await idbAllSnaps(); for(const r of all) await idbDelSnap(r.id); })()`);
  await c.ev(`(async()=>{ for(let i=0;i<70;i++){ const d=new Date(Date.now()-i*86400000); await idbPutSnap({id:d.toISOString()+"_daily",createdAt:d.toISOString(),kind:"daily",bytes:new Uint8Array([1]),compressed:false,size:1}); } })()`);
  const before=await c.ev("idbAllSnaps().then(a=>a.length)");
  const rr=await c.ev("applyIdbRetention()");
  const after=await c.ev("idbAllSnaps().then(a=>a.map(r=>r.createdAt))");
  const cutNow=Date.now()-31*86400000;
  ck("IDB整理：直近31日はすべて残る", after.filter(t=>Date.parse(t)>=cutNow).length, 32>=0?before-(rr.removed)-after.filter(t=>Date.parse(t)<cutNow).length:0);
  const olderMonths=new Set(after.filter(t=>Date.parse(t)<cutNow).map(t=>{const d=new Date(t);return d.getFullYear()+"-"+d.getMonth();}));
  ck("IDB整理：古い分は各月1件に集約", after.filter(t=>Date.parse(t)<cutNow).length, olderMonths.size);
  ck("IDB整理：70件→整理で減る", after.length<before && rr.removed>0, true);
  // 復元前の退避は1年は残る
  await c.ev(`idbPutSnap({id:"pre1",createdAt:new Date(Date.now()-100*86400000).toISOString(),kind:"pre-restore",bytes:new Uint8Array([1]),compressed:false,size:1})`);
  await c.ev("applyIdbRetention()");
  ck("復元前の退避は100日経っても残る", await c.ev(`idbGetSnap("pre1").then(r=>!!r)`), true);

  // =============== S11. 自動実行の制御 ===============
  await c.ev(`(async()=>{ const all=await idbAllSnaps(); for(const r of all) await idbDelSnap(r.id); })()`);
  c.ev("DATA.settings.backup.lastAutoAt=''; DATA.settings.backup.auto=true;");
  let t1 = await c.ev("autoBackupTick('テスト')");
  ck("自動実行：初回は実行（ブラウザ内保存・フォルダ未設定）", [!!t1.idb, t1.folderState], [true,"nofolder"]);
  ck("自動実行：最終実行時刻が記録", !!c.ev("DATA.settings.backup.lastAutoAt"), true);
  ck("自動実行：20時間以内は再実行しない", (await c.ev("autoBackupTick('テスト2')")).skipped, "interval");
  ck("自動実行：ログに記録（フォルダ未設定の旨）", c.ev("DATA.auditLog.slice(-1)[0].action").includes("フォルダ 未設定"), true);
  c.ev("DATA.settings.backup.auto=false");
  ck("自動実行：OFFなら動かない", (await c.ev("autoBackupTick('テスト3')")).skipped, "off");
  const forced = await c.ev("autoBackupTick('手動',{force:true})");
  ck("手動実行：OFFでも間隔に関係なく実行", !!forced.idb, true);
  c.ev("DATA.settings.backup.auto=true; DATA.settings.backup.lastAutoAt=new Date(Date.now()-21*3600000).toISOString()");
  ck("自動実行：21時間経過後は実行", !!(await c.ev("autoBackupTick('21h')")).idb, true);
  ck("ログの連鎖は正常のまま", c.ev("verifyAuditChain().ok"), true);

  // =============== S12. 一覧・検証・復元（画面） ===============
  c.doc.querySelector('nav button[data-tab="backup"]').click(); await sleep(150);
  ck("自動バックアップ画面：未設定の警告", c.doc.getElementById("ab-status").textContent.includes("未設定"), true);
  c.doc.getElementById("ab-list-toggle").click(); await sleep(250);
  ck("一覧に行が表示される", c.doc.querySelectorAll("#ab-list tr").length>=2, true);
  c.alerts.length=0; c.doc.querySelector("#ab-list .ab-verify").click(); await sleep(250);
  ck("一覧の「検証」でOK表示", c.alerts.some(a=>a.includes("検証OK")), true);
  // 復元：現在のデータを変えてから、最新のスナップショットで戻す
  const snapId = await c.ev("idbAllSnaps().then(a=>a.filter(r=>r.kind!=='pre-restore').sort((x,y)=>x.createdAt<y.createdAt?1:-1)[0].id)");
  const logBefore = c.ev("DATA.auditLog.length");
  c.ev("DATA.settings.sites.push('復元テストで消えるはず'); save();");
  c.alerts.length=0;
  const restoreBtn=[...c.doc.querySelectorAll("#ab-list .ab-restore")].find(b=>b.dataset.id===snapId);
  restoreBtn.click(); await sleep(400);
  ck("復元：スナップショットの内容に戻る", c.ev("DATA.settings.sites.includes('復元テストで消えるはず')"), false);
  ck("復元：操作ログは保持され、復元が追記される", c.ev("DATA.auditLog.length")>logBefore && c.ev("DATA.auditLog.slice(-1)[0].action").includes("バックアップから復元"), true);
  ck("復元：復元前の状態が自動退避される", await c.ev("idbAllSnaps().then(a=>a.some(r=>r.kind==='pre-restore'))"), true);
  ck("復元後もログ連鎖は正常", c.ev("verifyAuditChain().ok"), true);
  // チェックサム不一致のファイルは、確認で拒否できる
  const envBad = JSON.parse(c.ev("JSON.stringify(buildExportEnvelope())")); envBad.data.settings.company.name="書換え";
  c.confirmAnswer=false; const nm=c.ev("DATA.settings.company.name");
  const okBad = await c.ev(`restoreFromText(${JSON.stringify(JSON.stringify(envBad))}, "テストファイル")`);
  ck("不一致ファイル：確認で中止すると取り込まれない", [okBad, c.ev("DATA.settings.company.name")], [false, nm]);
  c.confirmAnswer=true;
  c.alerts.length=0; const okInvalid = await c.ev(`restoreFromText('{"settings":{}}', "不正")`);
  ck("不正なファイル：エラー表示で取り込まれない", [okInvalid, c.alerts.some(a=>a.includes("読み込めませんでした"))], [false,true]);

  // =============== S13. 繰越の点検・修正 ===============
  c.ev("loadSampleData(null)"); await sleep(100);
  ck("サンプルは繰越が整合している", c.ev("checkCarryConsistency().length"), 0);
  const ks=c.ev("Object.keys(DATA.manHours).sort()"); const lastK=ks[ks.length-1];
  c.ev(`DATA.manHours["${lastK}"].carry = 999`);
  const chk=c.ev("checkCarryConsistency()"); ck("繰越の不一致を検出", [chk.length, chk[0].stored], [1,999]);
  c.doc.getElementById("sc-carry-check").click();
  ck("点検画面に修正ボタンが出る", c.doc.getElementById("sc-carry-fix").style.display, "");
  c.ev(`DATA.closed.manHours["${lastK}"]=true`); const sk=c.ev("fixCarryConsistency()");
  ck("締め済みの月は修正しない", [sk.fixed, sk.skipped, c.ev(`DATA.manHours["${lastK}"].carry`)], [0,1,999]);
  c.ev(`delete DATA.closed.manHours["${lastK}"]`); const fx=c.ev("fixCarryConsistency()");
  ck("修正すると整合する", [fx.fixed, c.ev("checkCarryConsistency().length")], [1,0]);
  ck("修正内容がログに記録", c.ev("DATA.auditLog.slice(-1)[0].action").includes("繰越を修正"), true);

  // =============== S14/S15. 書き出し・読み込みの画面操作 ===============
  let dl=null; c.win.downloadText=(name,text)=>{ dl={name,text}; };
  c.doc.getElementById("bk-export").click();
  const exp=JSON.parse(dl.text);
  ck("書き出し：エンベロープ形式・チェックサム付き", [exp.format, exp.checksum.length, dl.name.endsWith(".json")], ["integratedBizSystem",64,true]);
  ck("書き出し：手動バックアップがログに記録", c.ev("DATA.auditLog.slice(-1)[0].action").includes("手動バックアップ"), true);
  const input=c.doc.getElementById("bk-file");
  const upload=async(txt)=>{ const f=new c.win.File([txt],"b.json",{type:"application/json"}); Object.defineProperty(input,"files",{value:[f],configurable:true}); input.dispatchEvent(new c.win.Event("change")); await sleep(500); };
  c.ev("DATA.settings.sites.push('ファイル復元で消えるはず')");
  await upload(dl.text);
  ck("ファイルから復元（エンベロープ）できる", c.ev("DATA.settings.sites.includes('ファイル復元で消えるはず')"), false);
  c.alerts.length=0; const keepName=c.ev("DATA.settings.company.name");
  await upload('{"settings":{"taxRates":[]},"invoices":{"zzz":1}}');
  ck("不正ファイル：取り込まれずエラー表示", [c.ev("DATA.settings.company.name"), c.alerts.some(a=>a.includes("読み込めませんでした"))], [keepName,true]);

  // =============== 構造 ===============
  const ids=[...html.matchAll(/\sid="([^"]+)"/g)].map(m=>m[1]);
  ck("ID重複なし", ids.filter((x,i)=>ids.indexOf(x)!==i).length, 0);
  ck("エラーなし(全体)", c.errors.length, 0); if(c.errors.length) console.log(c.errors);

  console.log("=== P1(残り) 検証 ==="); res.forEach(r=>console.log((r.p?"PASS":"FAIL")+" | "+r.l+(r.p?"":" | actual="+JSON.stringify(r.a)+" expected="+JSON.stringify(r.e))));
  const f=res.filter(r=>!r.p); console.log("\nTotal:",res.length,"Passed:",res.length-f.length,"Failed:",f.length);
  process.exit(f.length?1:0);
})();
