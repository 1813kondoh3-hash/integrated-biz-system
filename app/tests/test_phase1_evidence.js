const { JSDOM } = require("jsdom"); const fs = require("fs"); const crypto = require("crypto");
const html = fs.readFileSync(process.env.APP_HTML || require("path").join(__dirname, "..", "integrated-biz-system.html"), "utf-8");
const res=[]; const ck=(l,a,e)=>{ const p=(typeof e==="number")?a===e:(JSON.stringify(a)===JSON.stringify(e)); res.push({l,a,e,p}); };
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
  const dom=new JSDOM(html,{runScripts:"dangerously",pretendToBeVisual:true,url:"https://example.com/t.html"});
  const win=dom.window, doc=win.document;
  let promptAnswer=null, alerts=[];
  win.alert=m=>alerts.push(String(m)); win.confirm=()=>true; win.print=()=>{};
  win.prompt=m=> promptAnswer!==null ? promptAnswer : (/初期化/.test(m)?"初期化":/上書き/.test(m)?"上書き":"テスト理由");
  win.URL.createObjectURL=()=>"blob:fake"; win.URL.revokeObjectURL=()=>{};
  const errors=[]; win.addEventListener("error",e=>errors.push(e.message+" @"+e.lineno));
  await sleep(500);
  const ev=c=>win.eval(c);
  const click=id=>doc.getElementById(id).click();
  const tab=t=>doc.querySelector(`nav button[data-tab="${t}"]`).click();
  const setv=(id,v)=>{const el=doc.getElementById(id); el.value=v; el.dispatchEvent(new win.Event("input",{bubbles:true})); el.dispatchEvent(new win.Event("change",{bubbles:true}));};

  // ---------- A. SHA-256 を別実装（Node crypto）と照合 ----------
  const samples=["","abc","統合業務システム","請求書 No.KD202611-001 を発行（確定）／総合計：￥775,170","a".repeat(55),"a".repeat(56),"a".repeat(63),"a".repeat(64),"a".repeat(65),"あ".repeat(500),"😀 emoji"];
  let allSha=true; samples.forEach(t=>{ const mine=ev(`sha256hex(${JSON.stringify(t)})`); const ref=crypto.createHash("sha256").update(t,"utf8").digest("hex"); if(mine!==ref){allSha=false; console.log("SHA MISMATCH for",JSON.stringify(t).slice(0,30));} });
  ck("SHA-256 が11パターンでNode標準と一致", allSha, true);

  // ---------- B. ハッシュ連鎖ログ ----------
  click("hd-sample"); await sleep(120);
  ck("サンプル後：連鎖は正常", ev("verifyAuditChain().ok"), true);
  ck("サンプル登録＋発行2件のログ(>=3)", ev("DATA.auditLog.length")>=3, true);
  ev("window.__snap = JSON.stringify(DATA.auditLog)");
  ev("DATA.auditLog[1].action += 'x'");               ck("行の改ざんを検知", ev("verifyAuditChain().ok"), false);
  ev("DATA.auditLog = JSON.parse(__snap)");
  ev("DATA.auditLog.splice(1,1)");                    ck("中間行の削除を検知", ev("verifyAuditChain().ok"), false);
  ev("DATA.auditLog = JSON.parse(__snap)");
  ev("DATA.auditLog[0].hash = '0'.repeat(64)");       ck("ハッシュ書換えを検知", ev("verifyAuditChain().ok"), false);
  ev("DATA.auditLog = JSON.parse(__snap)");
  ck("復元後は正常", ev("verifyAuditChain().ok"), true);
  // 旧形式（新しい順・ハッシュ無し）の移行
  ev(`window.__mig = migrateAuditLog([{time:"2026/1/2",action:"旧B"},{time:"2026/1/1",action:"旧A"}])`);
  ck("移行後：古い順に並び替え", [ev("__mig[0].action"), ev("__mig[1].action")], ["旧A","旧B"]);
  ck("移行後：末尾に移行記録", ev("__mig[2].action").includes("移行"), true);
  ev("(function(){ const keep=DATA.auditLog; DATA.auditLog=__mig; window.__migOk=verifyAuditChain().ok; DATA.auditLog=keep; })()");
  ck("移行したログの連鎖が正常", ev("__migOk"), true);

  // ---------- C. 発行確定とロック ----------
  const D=ev("DATA"); const keys=Object.keys(D.invoices).sort(); const k1=keys[0], k2=keys[1];
  ck("サンプル請求書は発行済み・ハッシュ付き", ev(`DATA.invoices["${k2}"][0].issued && !!DATA.invoices["${k2}"][0].issueHash`), true);
  const vi=ev("verifyIssuedInvoices()"); ck("発行済み検査：2件・問題なし", [vi.n, vi.problems.length], [2,0]);
  ev(`DATA.invoices["${k2}"][0].items[0].price="9999"`);
  ck("発行後に価格を書換え→検知", ev("verifyIssuedInvoices().problems.length"), 1);
  ev(`DATA.invoices["${k2}"][0].items[0].price="2100"`);
  ck("元に戻すと検知なし", ev("verifyIssuedInvoices().problems.length"), 0);

  tab("invoice"); ev(`document.getElementById("inv-month").value="${k2}"; refreshInvoiceSelect(); document.getElementById("inv-select").value="0"; drawCurrentInvoice();`);
  ck("発行済み：取引先欄は編集不可", doc.getElementById("inv-client").disabled, true);
  ck("発行済み：明細追加ボタンは無効", doc.getElementById("inv-additem").disabled, true);
  ck("発行済み：状態は変更可（入金管理のため）", doc.getElementById("inv-status").disabled, false);
  ck("発行済み：発行ボタンは無効／取消は有効", [doc.getElementById("inv-issue").disabled, doc.getElementById("inv-void").disabled], [true,false]);
  ck("発行済み：削除ボタンは無効", doc.getElementById("inv-del").disabled, true);
  ck("状態表示：発行済み", doc.getElementById("inv-lock-state").textContent.includes("発行済み"), true);
  alerts=[]; ck("発行済み：内容変更ガード", ev(`guardInv(DATA.invoices["${k2}"][0],"${k2}","content")`), true);
  ck("発行済み：入金変更は可", ev(`guardInv(DATA.invoices["${k2}"][0],"${k2}","payment")`), false);
  alerts=[]; const nIss=ev(`DATA.invoices["${k2}"].length`); doc.getElementById("inv-del").disabled=false; click("inv-del"); ev("drawCurrentInvoice()");
  ck("発行済みの削除は（ボタンを有効化しても）処理側で拒否される", [alerts.some(a=>a.includes("削除できません")), ev(`DATA.invoices["${k2}"].length`)], [true,nIss]);
  alerts=[]; click("inv-void"); ck("入金記録がある請求書は取消不可", alerts.some(a=>a.includes("入金記録があるため")), true);

  // ---------- D. 下書き→発行→取消（赤伝） ----------
  const cur=ev("thisMonthKey()");
  ev(`document.getElementById("inv-month").value="${cur}"; refreshInvoiceSelect();`);
  const balBefore=ev(`getClientOutstandingBalance(DATA.settings.clients[0].id)`);
  const taxBefore=ev(`Object.values(DATA.invoices).flat().reduce((s,i)=>s+getInvoiceFinancials(i).tax,0)`);
  click("inv-new");
  ev(`(function(){ const r=currentInvoiceRef(); r.inv.clientId=DATA.settings.clients[0].id; r.inv.date=todayStr(); r.inv.items.push({site:"現場A",content:"型枠工事",qty:"1",unit:"m2x",price:"705"}); drawCurrentInvoice(); })()`);
  ck("下書き：状態表示", doc.getElementById("inv-lock-state").textContent, "下書き（未発行）");
  ck("下書き：発行は有効／取消は無効／削除は有効", [doc.getElementById("inv-issue").disabled, doc.getElementById("inv-void").disabled, doc.getElementById("inv-del").disabled],[false,true,false]);
  ev("window.__X = currentInvoiceRef().inv");
  click("inv-issue");
  ck("発行（確定）できた", ev("!!__X.issued && !!__X.issueHash"), true);
  ck("発行がログに記録（内容ハッシュ入り）", ev("DATA.auditLog.slice(-1)[0].action.includes(__X.issueHash)"), true);
  ck("発行後は内容ロック", ev(`guardInv(__X,"${cur}","content")`), true);
  const grandX=ev("getInvoiceFinancials(__X).grand");
  ck("705円×10%=70.5→71円（四捨五入）総合計776", grandX, 776);
  ck("発行後の未収残高は＋776", ev(`getClientOutstandingBalance(DATA.settings.clients[0].id)`)-balBefore, 776);

  click("inv-void");
  ck("取消：元の請求書に取消印", ev("!!__X.voided && __X.status==='取消'"), true);
  ev(`window.__C = Object.values(DATA.invoices).flat().find(i=>i.creditOf===__X.id)`);
  ck("赤伝が作成された", ev("!!__C"), true);
  ck("赤伝の小計は元の符号反転(-705)", ev("getInvoiceFinancials(__C).subtotal"), -705);
  ck("赤伝の消費税は元と同額で反転(-71：非対称丸めなし)", ev("getInvoiceFinancials(__C).tax"), -71);
  ck("元＋赤伝の総合計は0", ev("getInvoiceFinancials(__X).grand+getInvoiceFinancials(__C).grand"), 0);
  ck("赤伝は発行済み・ハッシュ付き・取消状態", ev("!!__C.issued && !!__C.issueHash && __C.status==='取消'"), true);
  ck("赤伝は元の伝票No.を参照", ev("__C.creditOfNo===__X.no"), true);
  ck("取消後の未収残高は取消前に戻る", ev(`getClientOutstandingBalance(DATA.settings.clients[0].id)`), balBefore);
  ck("消費税合計は取消前と同じ（相殺）", ev(`Object.values(DATA.invoices).flat().reduce((s,i)=>s+getInvoiceFinancials(i).tax,0)`), taxBefore);
  ck("取消がログに記録（理由・赤伝No.入り）", /取消（理由：テスト理由）[\s\S]*赤伝 No\./.test(ev("DATA.auditLog.slice(-1)[0].action")), true);
  const list=ev("buildInvoiceListData()");
  ck("一覧：赤伝に〔赤伝〕表示", list.some(r=>r.no.includes("〔赤伝〕")), true);
  ck("未収金一覧に取消分が出ない", ev(`buildInvoiceListData().filter(r=>r.status==="未収"||r.status==="一部回収"||r.status==="請求済み").some(r=>r.no.includes(__X.no))`), false);
  alerts=[]; ck("赤伝は編集不可", ev(`guardInv(__C,"${cur}","payment")`), true);
  alerts=[]; ev(`voidInvoice(__X,"${cur}")`); ck("二重取消は拒否", alerts.some(a=>a.includes("すでに取消済み")), true);
  // 売掛金元帳・仕訳
  tab("report"); ev(`document.getElementById("ledger-ar-client").value=DATA.settings.clients[0].id; renderArLedger();`);
  ck("売掛金元帳に赤伝行(減少)", /赤伝[\s\S]*￥776/.test(doc.getElementById("ledger-ar-table").textContent), true);
  let cap=null; win.downloadCSV=(aoa)=>{cap=aoa;};
  click("rep-export-sales-journal");
  const rev=cap.filter(r=>String(r[7]||"").includes("取消（赤伝）"));
  ck("売上仕訳：赤伝は貸借反転(借方 売上高)", rev.length>=1 && rev[0][2]==="売上高" && rev[0][4]==="売掛金" && rev[0][3]===705, true);
  const revTax=cap.filter(r=>String(r[7]||"").includes("消費税の取消"));
  ck("売上仕訳：消費税も反転(借方 仮受消費税71)", revTax.length===1 && revTax[0][2]==="仮受消費税" && revTax[0][3]===71, true);

  // 複製元に赤伝・取消済みを選ばない
  tab("invoice"); ev(`document.getElementById("inv-month").value="${cur}"; refreshInvoiceSelect();`);
  click("inv-new"); ev(`(function(){ const r=currentInvoiceRef(); r.inv.clientId=DATA.settings.clients[0].id; })()`);
  click("inv-copyprev");
  ck("複製：赤伝・取消済みを複製元にしない", ev(`currentInvoiceRef().inv.items.every(i=>parseFloat(i.price)>=0) && !currentInvoiceRef().inv.creditOf && !currentInvoiceRef().inv.voided && !currentInvoiceRef().inv.issued`), true);

  // ---------- E. 下書き削除／印刷時の自動発行確認 ----------
  click("inv-new"); const nBefore=ev(`DATA.invoices["${cur}"].length`);
  alerts=[]; click("inv-del");
  ck("下書きは削除できる", ev(`DATA.invoices["${cur}"].length`), nBefore-1);
  ck("下書き削除がログに記録", ev("DATA.auditLog.slice(-1)[0].action").includes("下書きの請求書を削除"), true);
  click("inv-new"); ev(`(function(){ const r=currentInvoiceRef(); r.inv.clientId=DATA.settings.clients[0].id; r.inv.date=todayStr(); r.inv.items.push({site:"現場B",content:"x",qty:"2",unit:"m2x",price:"100"}); drawCurrentInvoice(); })()`);
  click("inv-print");
  ck("下書きを印刷すると発行（確定）される", ev("!!currentInvoiceRef().inv.issued"), true);
  ck("印刷がログに記録", ev("DATA.auditLog.slice(-1)[0].action").includes("を印刷"), true);

  // ---------- F. 締め：取消は当月に赤伝／解除は理由必須 ----------
  click("inv-new"); ev(`(function(){ const r=currentInvoiceRef(); r.inv.clientId=DATA.settings.clients[0].id; r.inv.date=todayStr(); r.inv.items.push({site:"現場C",content:"y",qty:"1",unit:"式",price:"1000"}); })()`);
  click("inv-issue"); ev("window.__Y = currentInvoiceRef().inv");
  ev(`DATA.closed.invoices["${cur}"]=true`);
  const nCur=ev(`DATA.invoices["${cur}"].length`);
  ev(`voidInvoice(__Y,"${cur}")`);  // 締め済み月→当月が締め済みなので拒否
  ck("締め済み月に赤伝は作れない（解除を促す）", [ev(`DATA.invoices["${cur}"].length`), alerts.some(a=>a.includes("締め処理済み"))], [nCur,true]);
  ev(`delete DATA.closed.invoices["${cur}"]`);
  promptAnswer=""; ev(`DATA.closed.manHours["${cur}"]=true`); ev(`document.getElementById("mh-month").value="${cur}"`);
  tab("manhours"); click("mh-close-toggle");
  ck("締め解除：理由が空だと解除されない", ev(`!!DATA.closed.manHours["${cur}"]`), true);
  promptAnswer="残業の入力漏れ訂正のため"; click("mh-close-toggle");
  ck("締め解除：理由ありで解除", ev(`!!DATA.closed.manHours["${cur}"]`), false);
  ck("解除理由がログに記録", ev("DATA.auditLog.slice(-1)[0].action").includes("残業の入力漏れ訂正のため"), true);
  promptAnswer=null;

  // ---------- G. 入金修正の履歴 ----------
  tab("invoice"); ev(`document.getElementById("inv-month").value="${k2}"; refreshInvoiceSelect(); document.getElementById("inv-select").value="0"; drawCurrentInvoice();`);
  const amt=doc.querySelector(".ip-amt"); amt.value="400000"; amt.dispatchEvent(new win.Event("input",{bubbles:true})); amt.dispatchEvent(new win.Event("change",{bubbles:true}));
  ck("入金額の修正（前→後）がログに記録", /入金額を修正：300000 → 400000/.test(ev("DATA.auditLog.slice(-1)[0].action")), true);
  const st=doc.getElementById("inv-status"); st.value="未収"; st.dispatchEvent(new win.Event("change",{bubbles:true}));
  ck("状態変更がログに記録", /の状態を変更：/.test(ev("DATA.auditLog.slice(-1)[0].action")), true);

  // ---------- H. 初期化・上書き・復元でもログ保持 ----------
  const lenBefore=ev("DATA.auditLog.length");
  promptAnswer="だめ"; alerts=[]; tab("backup"); click("bk-reset");
  ck("初期化：確認入力が違うと中止", [ev("Object.keys(DATA.invoices).length>0"), alerts.some(a=>a.includes("中止"))], [true,true]);
  promptAnswer="初期化"; click("bk-reset");
  ck("初期化：データは空に", ev("Object.values(DATA.invoices).flat().length"), 0);
  ck("初期化：ログは保持（件数が増える）", ev("DATA.auditLog.length")>lenBefore, true);
  ck("初期化後も連鎖は正常", ev("verifyAuditChain().ok"), true);
  ck("初期化がログに記録", ev("DATA.auditLog.slice(-1)[0].action").includes("全データを初期化"), true);
  promptAnswer=null; ev("loadSampleData(null)"); await sleep(80);
  const exported=JSON.parse(ev("JSON.stringify(DATA)"));
  const lenMid=ev("DATA.auditLog.length");
  // 復元（別ログを持つファイルを読み込んでも、現行ログを保持）
  exported.auditLog=[{seq:1,time:"x",action:"他PCのログ",prev:"0".repeat(64),hash:"abc"}];
  const input=doc.getElementById("bk-file"); const file=new win.File([JSON.stringify(exported)],"b.json",{type:"application/json"});
  Object.defineProperty(input,"files",{value:[file],configurable:true}); input.dispatchEvent(new win.Event("change")); await sleep(300);
  ck("復元：現行ログを保持し復元を追記", ev("DATA.auditLog.length")===lenMid+1 && ev("DATA.auditLog.slice(-1)[0].action").includes("バックアップから復元"), true);
  ck("復元後も連鎖は正常", ev("verifyAuditChain().ok"), true);

  // ---------- I. 規程の雛形 ----------
  tab("backup");
  setv("reg-effective","2027-01-04"); setv("reg-manager","山田 太郎"); setv("reg-manager-title","代表取締役"); setv("reg-location","事務所PCと外付けHDD");
  const reg=ev("buildRegulationHtml()"); const rd=new JSDOM(reg).window.document;
  ck("規程：会社名が入る", reg.includes("株式会社サンプル工業"), true);
  ck("規程：施行日・管理責任者・保存場所が入る", reg.includes("2027年1月4日") && reg.includes("山田 太郎") && reg.includes("事務所PCと外付けHDD"), true);
  ck("規程：保存期間は7年", reg.includes("７年間とする"), true);
  ck("規程：発行確定・取消・追記専用ログ・締めの条文", ["発行（確定）","取消（赤伝）","追記専用","締めを解除する場合は、その理由"].every(t=>reg.includes(t)), true);
  ck("規程：訂正削除申請書の記載8項目", [...rd.querySelectorAll("ol")].some(o=>o.querySelectorAll("li").length===8), true);
  ck("規程：別紙様式の行(9)", rd.querySelectorAll("table tr").length, 9);
  ck("規程：条文は第12条まで＋附則", /第12条/.test(reg) && reg.includes("附則"), true);
  click("reg-export"); ck("規程出力がログに記録", ev("DATA.auditLog.slice(-1)[0].action").includes("事務処理規程"), true);

  // ---------- J. UI ----------
  ck("ログのクリアボタンは存在しない", !!doc.getElementById("log-clear"), false);
  click("log-verify"); ck("ログ検証ボタンで結果表示", /✅ ログの連鎖：正常/.test(doc.getElementById("log-verify-result").textContent), true);
  const ids=[...html.matchAll(/\sid="([^"]+)"/g)].map(m=>m[1]); ck("ID重複なし", ids.filter((x,i)=>ids.indexOf(x)!==i).length, 0);
  ck("window error なし", errors.length, 0); if(errors.length) console.log(errors);

  console.log("=== P1 検証 ==="); res.forEach(r=>console.log((r.p?"PASS":"FAIL")+" | "+r.l+(r.p?"":" | actual="+JSON.stringify(r.a)+" expected="+JSON.stringify(r.e))));
  const f=res.filter(r=>!r.p); console.log("\nTotal:",res.length,"Passed:",res.length-f.length,"Failed:",f.length);
  process.exit(f.length?1:0);
})();
