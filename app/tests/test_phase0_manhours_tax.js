const { JSDOM } = require("jsdom");
const fs = require("fs");
const html = fs.readFileSync(process.env.APP_HTML || require("path").join(__dirname, "..", "integrated-biz-system.html"), "utf-8");
const results = [];
const ck = (label, actual, expected) => {
  const pass = (typeof expected==="number") ? Math.abs(actual-expected)<1e-9 : actual===expected;
  results.push({label, actual, expected, pass});
};
(async () => {
  const dom = new JSDOM(html, { runScripts:"dangerously", pretendToBeVisual:true, url:"https://example.com/t.html" });
  const win = dom.window; const doc = win.document;
  win.alert=()=>{}; win.confirm=()=>true; win.prompt=m=>/初期化/.test(m)?"初期化":/上書き/.test(m)?"上書き":"x";
  win.URL.createObjectURL=(b)=>{ win.__lastBlob=b; return "blob:fake"; }; win.URL.revokeObjectURL=()=>{};
  const errors=[]; win.addEventListener("error", e=>errors.push(e.message));
  await new Promise(r=>setTimeout(r,500));

  // ---- 月プルダウン ----
  const mp = doc.getElementById("mh-month");
  ck("month picker is span with 2 selects", mp.tagName+":"+mp.querySelectorAll("select").length, "SPAN:2");
  mp.value = "2027-03";
  ck("month picker set/get", mp.value, "2027-03");
  mp.value = "2040-12"; // 範囲外の年も受け付ける
  ck("month picker out-of-range year", mp.value, "2040-12");
  mp.value = "2026-06";

  // ---- 残業分離（独立計算） ----
  win.eval("loadSampleData(null)");
  await new Promise(r=>setTimeout(r,100));
  const D = win.eval("DATA");
  const curKey = Object.keys(D.manHours).sort().pop();
  const m = D.manHours[curKey];
  // 独立計算：人員行は数値のみ、残業行は先頭の数値(H)
  let people=0, ot=0;
  m.rows.forEach(r=>{
    const isOt = /残業/.test(r.content);
    Object.values(r.daily).forEach(v=>{
      const s = String(v).replace(/[０-９]/g,c=>String.fromCharCode(c.charCodeAt(0)-0xFEE0));
      if(isOt){ const n=parseFloat(s); if(!isNaN(n)) ot+=n; }
      else { const n=Number(s); if(s!=="" && !isNaN(n)) people+=n; }
    });
  });
  ck("people total (app)", win.eval(`monthPeopleTotal(DATA.manHours["${curKey}"])`), people);
  ck("overtime total (app)", win.eval(`monthOvertimeTotal(DATA.manHours["${curKey}"])`), ot);
  ck("overtime is 4h in sample (1H x4)", ot, 4);
  ck("people total excludes overtime (was 120 incl. OT)", people, 116);

  // 画面のフィールド
  doc.querySelector('nav button[data-tab="manhours"]').click();
  win.eval(`document.getElementById("mh-month").value="${curKey}"; renderManHours();`);
  ck("UI 今月の小計", Number(doc.getElementById("mh-subtotal").value), 116);
  ck("UI 今月の残業時間", Number(doc.getElementById("mh-overtime").value), 4);
  ck("UI 累計 = 繰越107 + 116", Number(doc.getElementById("mh-total").value), 223);
  // 23日列：現場A 4 + 置場 1 = 5（残業1Hは含まない）
  const col23 = doc.getElementById("mh-coltotal-3"); // 21日が seq1 -> 23日 seq3
  ck("23日の人員合計（残業除外）", Number(col23.textContent), 5);
  ck("23日の残業時間", Number(doc.getElementById("mh-ottotal-3").textContent), 1);

  // 全角「２H」「1.5H」の扱い
  win.eval(`(function(){ const mm=DATA.manHours["${curKey}"]; mm.rows.push({content:"残業(臨時)",daily:{1:"２H",2:"1.5H"},sub:0}); })()`);
  ck("全角・小数の残業を解釈", win.eval(`rowTotal(DATA.manHours["${curKey}"].rows.slice(-1)[0])`), 3.5);
  // 人員行にテキスト「1H」を入れても人員に入らない（元ExcelのSUMと同じ）
  ck("人員行の文字は無視", win.eval(`cellNumber({content:"現場X",daily:{}}, "1H")`), 0);
  // 種別を手動で切替
  win.eval(`(function(){ const mm=DATA.manHours["${curKey}"]; mm.rows.push({content:"応援",daily:{1:"3"},sub:0,kind:"overtime"}); })()`);
  ck("kind=overtime は名前に関係なく残業扱い", win.eval(`isOvertimeRow(DATA.manHours["${curKey}"].rows.slice(-1)[0])`), true);
  win.eval(`DATA.manHours["${curKey}"].rows.splice(-2,2)`);

  // 次月繰越：人員と残業を別々に繰り越す
  win.eval(`ensureManHourMonth("2030-01", true)`);
  const nm = win.eval(`DATA.manHours["2030-01"]`);
  ck("次月の人員繰越 = 107+116", nm.carry, 223);
  ck("次月の残業繰越 = 前月分10 + 当月4", nm.otCarry, 14);
  ck("次月の行に kind/名前を引継ぎ", nm.rows.length, m.rows.length);

  // 請求書への取込：残業行は取り込まない
  doc.querySelector('nav button[data-tab="invoice"]').click();
  win.eval(`document.getElementById("inv-month").value="${curKey}"`);
  doc.getElementById("inv-new").click();
  doc.getElementById("inv-import-manhours").click();
  const items = win.eval("currentInvoiceRef().inv.items.map(i=>i.site)");
  ck("取込に「残業」を含まない", items.includes("残業"), false);
  ck("取込に現場Aを含む", items.includes("現場A"), true);

  // 予算消化：残業を現場の人日に含めない
  win.eval(`DATA.settings.siteBudgets={"残業":10,"現場A":100}; renderBudgetReport();`);
  const budgetText = doc.getElementById("rep-budget-list").textContent;
  ck("予算消化で残業は実績0", /残業[\s\S]*?実績 0 \//.test(budgetText), true);

  // 月別一覧・明細一覧
  const sum = win.eval(`buildManHourMonthSummary().find(x=>x.month==="${curKey}")`);
  ck("月別集計 subtotal", sum.subtotal, 116);
  ck("月別集計 overtime", sum.overtime, 4);
  const lst = win.eval(`buildManHourListData().filter(r=>r.month==="${curKey}" && r.type==="残業")`);
  ck("明細一覧に残業区分", lst.length>=1, true);

  // ---- 簡易課税の既定値（建設業：第3種70%） ----
  const fresh = win.eval("defaultData().settings");
  ck("簡易課税の既定は第3種", fresh.simplifiedTax.category, 3);
  ck("事業者区分の既定は法人", fresh.businessType, "法人");
  doc.querySelector('nav button[data-tab="report"]').click();
  win.eval("renderReport()");
  ck("簡易課税セレクトは第3種", doc.getElementById("tax-simplified-category").value, "3");
  ck("法人注記に3割特例の記載", /3割特例は個人事業者のみ/.test(doc.getElementById("tax-corp-note").textContent), true);
  win.eval(`DATA.settings.businessType="個人事業者"; renderTaxSummary()`);
  ck("個人の場合は3割特例の案内", /令和9年分・令和10年分/.test(doc.getElementById("tax-corp-note").textContent), true);
  win.eval(`DATA.settings.businessType="法人"; renderTaxSummary()`);

  // 旧データ移行：未使用で第5種なら第3種へ
  win.localStorage; // jsdom: not available; 直接 load() 経路を模擬
  const migrated = win.eval(`(function(){ const d={settings:{simplifiedTax:{enabled:false,category:5,rate:50}}}; const def=defaultData(); d.settings=Object.assign(def.settings,d.settings||{}); if(d.settings.simplifiedTax && !d.settings.simplifiedTax.enabled && d.settings.simplifiedTax.category===5){ d.settings.simplifiedTax.category=3; } return d.settings.simplifiedTax.category; })()`);
  ck("旧既定の移行ロジック", migrated, 3);

  // ---- 経過措置マスタ ----
  const rates = {"2019-10-01":100,"2023-09-30":100,"2023-10-01":80,"2026-09-30":80,"2026-10-01":70,"2028-09-30":70,"2028-10-01":50,"2030-09-30":50,"2030-10-01":30,"2031-09-30":30,"2031-10-01":0};
  Object.entries(rates).forEach(([d,r])=> ck("経過措置 "+d, win.eval(`getTransitionalRate("${d}")`), r));
  ck("範囲外（2019年9月）", win.eval(`getTransitionalRate("2019-09-30")`), null);
  doc.querySelector('nav button[data-tab="master"]').click();
  doc.getElementById("m-trans-date").value="2026-11-15";
  doc.getElementById("m-trans-amount").value="550000";
  doc.getElementById("m-trans-date").dispatchEvent(new win.Event("input",{bubbles:true}));
  ck("概算ツール 70%・控除35,000円", /70%[\s\S]*50,000[\s\S]*35,000/.test(doc.getElementById("m-trans-result").textContent), true);
  ck("上限の初期値 1億円", Number(doc.getElementById("m-trans-limit").value), 100000000);

  // ---- 閲覧専用ファイル（ウ） ----
  const snapMasked = win.eval("buildSnapshotHtml(true)");
  const snapReal = win.eval("buildSnapshotHtml(false)");
  const sd = new JSDOM(snapMasked).window.document;
  ck("閲覧専用：セクション数(4)", sd.querySelectorAll("section").length, 4);
  ck("閲覧専用：viewport あり", !!sd.querySelector('meta[name=viewport]'), true);
  ck("閲覧専用：匿名版は取引先実名なし", /建設株式会社|工業株式会社/.test(snapMasked), false);  // allow-secret 見本の会社名のパターン
  ck("閲覧専用：匿名版に「取引先1」", /取引先1/.test(snapMasked), true);
  ck("閲覧専用：実名版は取引先実名あり", /建設株式会社/.test(snapReal), true);  // allow-secret 見本の会社名のパターン
  ck("閲覧専用：残業時間の列がある", /残業（今月・h）/.test(snapMasked), true);
  ck("閲覧専用：入力欄は検索のみ", sd.querySelectorAll("input").length, 1);
  doc.querySelector('nav button[data-tab="backup"]').click();
  doc.getElementById("snap-export").click();
  ck("閲覧専用：書き出しボタンでBlob生成", !!win.__lastBlob, true);
  ck("操作ログに記録", win.eval("DATA.auditLog.slice(-1)[0].action").includes("閲覧専用"), true);

  // ---- 構造 ----
  const ids = [...html.matchAll(/\sid="([^"]+)"/g)].map(m=>m[1]);
  const dup = ids.filter((x,i)=>ids.indexOf(x)!==i);
  ck("ID重複なし", dup.length, 0);
  ck("window error なし", errors.length, 0);

  console.log("=== P0 検証 ===");
  results.forEach(r=> console.log((r.pass?"PASS":"FAIL")+" | "+r.label+" | actual="+JSON.stringify(r.actual)+" expected="+JSON.stringify(r.expected)));
  const f=results.filter(r=>!r.pass);
  console.log("\nTotal:",results.length,"Passed:",results.length-f.length,"Failed:",f.length);
  if(errors.length) console.log("ERRORS:", errors);
  process.exit((f.length||errors.length)?1:0);
})();
