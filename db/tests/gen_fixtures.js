// 現行アプリ（統合業務システム.html）を実際に動かして、DB設計の検証用データを生成する。
// 使い方: NODE_PATH=../../app/node_modules node gen_fixtures.js  （app で npm ci 済みであること）
const { JSDOM } = require("jsdom"); const fs = require("fs"); const path = require("path");
const HTML = process.env.APP_HTML || path.join(__dirname, "..", "..", "app", "integrated-biz-system.html");
const OUT = path.join(__dirname, "fixtures");
const html = fs.readFileSync(HTML, "utf-8");
function boot(){
  const dom = new JSDOM(html, {runScripts:"dangerously", pretendToBeVisual:true, url:"https://example.com/t.html",
    beforeParse(w){ w.TextEncoder=TextEncoder; w.TextDecoder=TextDecoder; }});
  const w = dom.window; w.__noAutoBackup = true;
  w.alert=()=>{}; w.confirm=()=>true; w.prompt=()=>"テスト理由"; w.print=()=>{};
  return w;
}
const sleep = ms => new Promise(r=>setTimeout(r,ms));
(async()=>{
  const w = boot(); await sleep(400);
  const ev = c => w.eval(c);
  ev("loadSampleData(null)"); await sleep(100);

  // ---- 1) 盛り込み用データ：あらゆる項目を使った v2 データ ----
  ev(`(function(){
    const S = DATA.settings;
    S.taxRates.push({id:"t8", label:"軽減税率8%", rate:8, effectiveFrom:"2014-04-01"});
    S.invoiceTemplates = { [S.clients[0].id]: {items:[{site:"現場A",content:"型枠工事",qty:"10",unit:"m2x",price:"2100"}], expenses:[{name:"法定福利費",amount:"380000"}]} };
    S.savedInvoiceFilters = [{name:"未収のみ", search:"", status:"未収", closed:"", sort:"date-desc"}];
    S.siteBudgets = {"現場A":120, "現場B":40};
    S.backup.lastAutoAt = "2026-11-02T10:00:00.000Z";
    DATA.siteCosts = {"現場A":[{id:"sc1",date:"2026-10-15",category:"外注費",amount:"150000",memo:"テスト"},{id:"sc2",date:"2026-10-20",category:"材料費",amount:"48000",memo:""}], "現場B":[{id:"sc3",date:"2026-11-02",category:"その他経費",amount:"12000",memo:"高速"}]};
  })()`);
  const cur = ev("thisMonthKey()");
  // 男性：下書き→発行→取消（赤伝）／値引き行・小数・全角・テキストを含む請求書
  ev(`document.getElementById("inv-month").value="${cur}"; refreshInvoiceSelect();`);
  ev(`document.getElementById("inv-new").click()`);
  ev(`(function(){ const r=currentInvoiceRef(); const i=r.inv; i.clientId=DATA.settings.clients[0].id; i.date="${cur}-05"; i.taxRateId="t8";
     i.items.push({site:"現場A",content:"型枠工事（基礎）",qty:"1.5",unit:"m2x",price:"2100"},{site:"現場B",content:"値引き",qty:"1",unit:"式",price:"-300"},{site:"現場C",content:"全角数量",qty:"１２",unit:"人",price:"15000"});
     i.expenses.push({name:"高速代",amount:"5200"}); drawCurrentInvoice(); })()`);
  ev(`issueInvoice(currentInvoiceRef().inv, "${cur}", true)`);
  ev(`voidInvoice(currentInvoiceRef().inv, "${cur}")`);                         // 取消（赤伝）
  ev(`document.getElementById("inv-new").click()`);                      // もう1件：下書きのまま
  ev(`(function(){ const i=currentInvoiceRef().inv; i.clientId=DATA.settings.clients[1].id; i.date="${cur}-18"; i.items.push({site:"現場D",content:"下書き",qty:"3",unit:"m2x",price:"1000"}); })()`);
  // 発行済み請求書へ入金を複数
  ev(`(function(){ const k=Object.keys(DATA.invoices).sort()[0]; const inv=DATA.invoices[k][0]; inv.payments.push({date:"2026-10-25",amount:"1000000"},{date:"2026-10-31",amount:"2925099"}); })()`);
  // 作業人員：全角・テキスト・種別の明示
  ev(`(function(){ const k=Object.keys(DATA.manHours).sort().pop(); const m=DATA.manHours[k];
     m.rows.push({content:"応援（臨時）",daily:{1:"3",2:"２H",3:"1.5H"},sub:0,kind:"overtime"});
     m.rows.push({content:"雑工",daily:{1:"メモ",2:"２",3:1.5},sub:0});
     m.expenses.push({name:"駐車料金",amount:"3273"}); })()`);
  // 締め
  ev(`(function(){ const ks=Object.keys(DATA.manHours).sort(); DATA.closed.manHours[ks[0]]=true; const ik=Object.keys(DATA.invoices).sort(); DATA.closed.invoices[ik[0]]=true; })()`);
  ev("logAction('テスト用の操作')");
  const rich = JSON.parse(ev("JSON.stringify(DATA)"));
  fs.writeFileSync(path.join(OUT,"rich_v2.json"), JSON.stringify(rich,null,1));

  // ---- 2) 参照切れ（取引先を削除した後の請求書など）を含むデータ ----
  const dang = JSON.parse(JSON.stringify(rich));
  const ik = Object.keys(dang.invoices).sort();
  dang.settings.clients = dang.settings.clients.filter(c => c.id !== dang.invoices[ik[0]][0].clientId);   // 取引先をマスタから削除（請求書は残る）
  const usedTax = dang.invoices[ik[1]][0].taxRateId;
  dang.settings.taxRates = dang.settings.taxRates.filter(t => t.id !== usedTax);                       // 税率をマスタから削除
  dang.settings.invoiceTemplates["deleted-client-xyz"] = {items:[{site:"x",content:"y",qty:"1",unit:"式",price:"1"}], expenses:[]};  // 持ち主のいないひな形
  dang.invoices[ik[1]].push({id:"noclient", clientId:"", date:"2026-11-09", no:"", taxRateId:"", status:"未収", items:[{site:"x",content:"y",qty:"1",unit:"式",price:"100"}], expenses:[], payments:[]});
  fs.writeFileSync(path.join(OUT,"dangling_v2.json"), JSON.stringify(dang,null,1));

  // ---- 3) 旧形式(v1)のデータをアプリで v2 に移行したもの ----
  const legacy = {
    settings:{company:{name:"旧社"}, taxRates:[{id:"t1",rate:10,effectiveFrom:"2019-10-01",label:"標準"}], clients:[{id:"c1",name:"旧取引先"}], sites:["現場A"], expenseItems:["高速代"]},
    manHours:{"2026-09":{rows:[{content:"現場A",daily:{1:5,2:5}},{content:"残業",daily:{1:"2H",2:"1.5H"}}],carry:0},"2026-10":{rows:[{content:"現場A",daily:{1:3}},{content:"残業",daily:{1:"1H"}}],carry:12}},
    invoices:{"2026-10":[{id:"i1",clientId:"c1",date:"2026-10-20",no:"KD202610-001",taxRateId:"t1",status:"未収",items:[{site:"現場A",content:"x",qty:"1",unit:"m2x",price:"1000"}]}]},
    labels:[], auditLog:[{time:"2026/10/2 10:00:00",action:"新しい"},{time:"2026/10/1 09:00:00",action:"古い"}]
  };
  const migrated = JSON.parse(ev(`JSON.stringify(migrateData(${JSON.stringify(legacy)}).data)`));
  fs.writeFileSync(path.join(OUT,"legacy_migrated_v2.json"), JSON.stringify(migrated,null,1));
  fs.writeFileSync(path.join(OUT,"legacy_v1.json"), JSON.stringify(legacy,null,1));

  // ---- 4) 計算の一致検証用：ランダムな請求書 2,000 件の「アプリでの計算結果」 ----
  const rnd = ev(`(function(){
    let seed = 20261103; const R = () => (seed = (seed*1664525 + 1013904223) % 4294967296) / 4294967296;
    const pick = a => a[Math.floor(R()*a.length)];
    const nums = ["", "0", "1", "2", "10", "100", "1.5", "0.333", "12.5", "-1", "-300", "705", "2100", "3925099", "１２", "abc", "1,000", " 12", "5.", ".5", "1e3", "0x10", "12px"];
    const dates = ["2014-03-31","2014-04-01","2019-09-30","2019-10-01","2026-11-03","", "2030-01-01"];
    const rates = ["t1","t8","dangling",""];
    DATA.settings.taxRates = [{id:"t1",label:"標準10%",rate:10,effectiveFrom:"2019-10-01"},{id:"t8",label:"8%",rate:8,effectiveFrom:"2014-04-01"},{id:"t5",label:"5%",rate:5,effectiveFrom:"1997-04-01"}];
    const out = [];
    for(let n=0;n<2000;n++){
      const inv = {id:"r"+n, clientId:"", date:pick(dates), no:"R"+n, taxRateId:pick(rates), status:"未収", items:[], expenses:[], payments:[]};
      for(let k=0, c=Math.floor(R()*4); k<c; k++) inv.items.push({site:"s",content:"c",qty:pick(nums),unit:"u",price:pick(nums)});
      for(let k=0, c=Math.floor(R()*3); k<c; k++) inv.expenses.push({name:"e",amount:pick(nums)});
      for(let k=0, c=Math.floor(R()*3); k<c; k++) inv.payments.push({date:"2026-11-01",amount:pick(nums)});
      const f = getInvoiceFinancials(inv);
      out.push({inv, expect:{subtotal:f.subtotal, tax:f.tax, grand:f.grand, paid:f.paid, remaining:f.remaining}});
    }
    return {taxRates:DATA.settings.taxRates, cases:out};
  })()`);
  fs.writeFileSync(path.join(OUT,"random_invoices.json"), JSON.stringify(rnd));

  // ---- 5) 作業人員セルの数値化ルール（アプリの cellNumber）の一致検証用 ----
  const cell = ev(`(function(){
    const vals = ["", "0", "1", "3", "1.5", "-2", "２", "２H", "1.5H", "1H", "H", "abc", "メモ", " 4", "4 ", "1,000", "5.", ".5", "1e2", "0x10", "１．５", "2時間", "１２H", null, undefined, 3, 1.5, 0];
    const out = [];
    ["people","overtime",undefined].forEach(kind=>{
      [ "現場A", "残業", "残業(臨時)" ].forEach(content=>{
        vals.forEach(v=>{ const row = {content, daily:{}}; if(kind) row.kind = kind; out.push({kind:kind||null, content, value:(v===undefined?"__undef__":v), expect:cellNumber(row, v), overtime:isOvertimeRow(row)}); });
      });
    });
    return out;
  })()`);
  fs.writeFileSync(path.join(OUT,"cell_vectors.json"), JSON.stringify(cell));

  // ---- 6) ハッシュの一致検証用：アプリの SHA-256 と内容ハッシュ ----
  const hv = ev(`(function(){
    const strs = ["", "abc", "統合業務システム", "😀", "a".repeat(200)];
    const invs = DATA.invoices[Object.keys(DATA.invoices).sort()[0]].concat(DATA.invoices["${cur}"] || []).map(i=>({inv:i, hash:invoiceContentHash(i)}));
    return {sha:strs.map(s=>({s, h:sha256hex(s)})), invoices:invs};
  })()`);
  fs.writeFileSync(path.join(OUT,"hash_vectors.json"), JSON.stringify(hv));

  console.log("fixtures written:", fs.readdirSync(OUT).join(", "));
  process.exit(0);
})();
