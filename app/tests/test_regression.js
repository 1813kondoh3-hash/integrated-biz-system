const { JSDOM } = require("jsdom"); const fs=require("fs");
const html = fs.readFileSync(process.env.APP_HTML || require("path").join(__dirname, "..", "integrated-biz-system.html"), "utf-8");
const errors=[]; const res=[];
const ck=(l,a,e)=>{ const p=(typeof e==="number")?Math.abs(a-e)<0.51:a===e; res.push({l,a,e,p}); };
(async()=>{
  const dom=new JSDOM(html,{runScripts:"dangerously",pretendToBeVisual:true,url:"https://example.com/t.html"});
  const win=dom.window, doc=win.document;
  win.alert=()=>{}; win.confirm=()=>true; win.print=()=>{}; win.prompt=m=>/初期化/.test(m)?"初期化":/上書き/.test(m)?"上書き":"テスト条件";
  win.URL.createObjectURL=()=>"blob:fake"; win.URL.revokeObjectURL=()=>{};
  win.addEventListener("error",e=>errors.push("window error: "+e.message+" @"+e.lineno));
  await new Promise(r=>setTimeout(r,500));
  const click=id=>{const el=doc.getElementById(id); if(!el){errors.push("MISSING "+id);return;} try{el.click();}catch(e){errors.push(id+": "+e.message);}};
  const setVal=(id,v)=>{const el=doc.getElementById(id); if(!el){errors.push("MISSING "+id);return;} el.value=v; el.dispatchEvent(new win.Event("input",{bubbles:true})); el.dispatchEvent(new win.Event("change",{bubbles:true}));};
  const tab=t=>doc.querySelector(`nav button[data-tab="${t}"]`).click();

  click("hd-sample"); await new Promise(r=>setTimeout(r,100));
  ["manhours","invoice","label","master","backup","report","ledger"].forEach(tab);

  tab("manhours");
  click("mh-addrow"); click("mh-addexpense"); click("mh-newmonth");
  click("mh-list-open"); setVal("mh-list-search","現場"); setVal("mh-list-type","残業"); setVal("mh-list-type","経費"); click("mh-list-clear"); click("mh-list-close");
  click("mh-print"); click("mh-close-toggle"); click("mh-close-toggle");
  const kind = doc.querySelector(".mh-kind"); if(kind){ kind.value="overtime"; kind.dispatchEvent(new win.Event("change",{bubbles:true})); }

  tab("invoice");
  click("inv-new"); click("inv-additem"); click("inv-adddiscount"); click("inv-addexp"); click("inv-addpay");
  setVal("inv-client","Ｃ工業株式会社"); setVal("inv-date","2026-11-15"); setVal("inv-status","一部回収");
  click("inv-save-template"); click("inv-load-template"); click("inv-copyprev");
  click("inv-list-open"); setVal("inv-list-search","B"); setVal("inv-list-status","未収");
  click("inv-list-excel"); click("inv-list-csv"); click("inv-list-jdl"); click("inv-list-close");
  click("inv-print"); click("inv-pdf"); click("inv-close-toggle"); click("inv-close-toggle");
  // 月ピッカーの変更イベントが請求書一覧を切替える
  const invMonth = doc.getElementById("inv-month");
  invMonth.value = "2026-10";
  invMonth.querySelector("select").dispatchEvent(new win.Event("change",{bubbles:true}));
  ck("請求月ピッカーの change が伝播", invMonth.value.startsWith("2026-"), true);
  click("inv-del");

  tab("label"); click("lb-add"); click("lb-fromclient"); setVal("lb-format","24"); setVal("lb-format","custom"); click("lb-print");
  tab("master");
  setVal("m-company-name","テスト会社"); setVal("m-business-type","個人事業者"); setVal("m-business-type","法人");
  setVal("m-client-input","新規X"); click("m-client-add");
  setVal("m-site-input","新現場Y"); click("m-site-add"); setVal("m-site-search","新"); setVal("m-site-search","");
  setVal("m-exp-input","新経費Z"); click("m-exp-add");
  setVal("m-budget-site","現場A"); setVal("m-budget-days","100"); click("m-budget-add");
  click("m-addtax"); click("m-trans-add");
  tab("backup"); setVal("bk-interval","7"); click("bk-export"); click("snap-export");
  tab("report");
  click("rep-qf-unpaid"); click("rep-qf-thismonth"); click("rep-qf-overdue"); click("rep-qf-clear");
  setVal("rep-invlist-search","Ｂ"); click("rep-saved-filter-save"); click("rep-saved-filter-del");
  click("rep-invlist-excel"); click("rep-invlist-csv"); click("rep-mhlist-excel"); click("rep-mhlist-csv");
  click("tax-simplified-enabled"); click("rep-export-sales-journal"); click("rep-export-payment-journal");
  tab("ledger"); setVal("cost-site","現場A"); setVal("cost-date","2026-11-10"); setVal("cost-amount","50000"); click("cost-add");

  // ---- 請求書の金額（独立計算）----
  win.eval("loadSampleData(null)"); await new Promise(r=>setTimeout(r,100));
  const D=win.eval("DATA"); const keys=Object.keys(D.invoices).sort();
  const f1=win.eval(`getInvoiceFinancials(DATA.invoices["${keys[0]}"][0])`);
  const f2=win.eval(`getInvoiceFinancials(DATA.invoices["${keys[1]}"][0])`);
  const sub1=[[1130,2000],[60,2000],[220,2000],[170,2000]].reduce((s,[q,p])=>s+q*p,0)+404999+3273;
  const sub2=[[95,2100],[60,2000]].reduce((s,[q,p])=>s+q*p,0)+380000+5200;
  ck("請求1 総合計", f1.grand, sub1+Math.round(sub1*0.1));
  ck("請求2 総合計", f2.grand, sub2+Math.round(sub2*0.1));
  ck("請求2 残額", f2.remaining, sub2+Math.round(sub2*0.1)-300000);

  console.log("=== 回帰テスト ==="); res.forEach(r=>console.log((r.p?"PASS":"FAIL")+" | "+r.l+" | "+JSON.stringify(r.a)+" vs "+JSON.stringify(r.e)));
  console.log("ERRORS:", errors.length?errors.join("\n"):"NONE");
  process.exit((res.some(r=>!r.p)||errors.length)?1:0);
})();
