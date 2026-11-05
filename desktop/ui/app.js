// 統合業務システム デスクトップアプリ（最小版）の画面。
// Rust 側のコマンドを呼ぶだけで、鍵そのもの（64桁の16進数）はこの画面に来ない。
"use strict";

const $ = (id) => document.getElementById(id);

// Tauri の外（ブラウザで開いた場合）でも、画面が壊れないようにする
function invoke(cmd, args) {
  const t = window.__TAURI__ && window.__TAURI__.core;
  if (!t) return Promise.reject("この画面は、デスクトップアプリの中で開いてください。");
  return t.invoke(cmd, args || {});
}

let status = null;
let current = "key";

function notice(text, isError) {
  const n = $("notice");
  n.textContent = text;
  n.className = isError ? "notice error" : "notice";
  n.hidden = !text;
  if (text) n.scrollIntoView({ block: "nearest" });
}

function errText(e) {
  return typeof e === "string" ? e : (e && e.message) || String(e);
}

// ボタンを押している間は二重に押せないようにし、結果を表示する
async function run(btn, fn) {
  btn.disabled = true;
  try {
    await fn();
  } catch (e) {
    notice(errText(e), true);
  } finally {
    btn.disabled = false;
  }
}

// 保存している時刻（UTC）を、このPCの時刻で表示する
function localTime(iso) {
  const d = iso ? new Date(iso) : null;
  return d && !isNaN(d) ? d.toLocaleString("ja-JP") : "不明";
}

function setFacts(el, rows) {
  el.replaceChildren();
  for (const [k, v, cls] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = k;
    const dd = document.createElement("dd");
    dd.textContent = v;
    if (cls) dd.className = cls;
    el.append(dt, dd);
  }
}

function show(name) {
  current = name;
  for (const p of document.querySelectorAll(".pane")) p.hidden = p.id !== "p-" + name;
  for (const b of document.querySelectorAll(".step")) {
    if (b.dataset.go === name) b.setAttribute("aria-current", "step");
    else b.removeAttribute("aria-current");
  }
}

function mark(id, state) {
  const m = $(id);
  if (m) m.className = "mark" + (state ? " " + state : "");
}

function render() {
  const s = status;
  $("ver").textContent = "版 " + s.app_version;

  // ---- 鍵
  const k = s.key;
  const keyRows = [["保管ファイル", s.key_path]];
  if (k.state === "ok") {
    keyRows.push(["状態", "鍵を正しく開けました", "good"], ["保管方式", k.method], ["鍵の指紋", k.fingerprint]);
  } else if (k.state === "missing") {
    keyRows.push(["状態", "まだ鍵がありません"]);
  } else {
    keyRows.push(["状態", "鍵を開けません", "bad"], ["理由", k.message]);
  }
  if (s.platform !== "windows") keyRows.push(["注意", "Windows以外では、鍵を保護せずに保存します（開発・検証用）", "bad"]);
  setFacts($("key-facts"), keyRows);
  const pendingOpen = !$("key-new").hidden;
  $("key-setup").hidden = k.state !== "missing" || pendingOpen;
  $("key-check").hidden = k.state !== "ok";
  mark("m-key", k.state === "ok" ? "done" : k.state === "error" ? "bad" : "");

  // ---- DB
  const d = s.db;
  const dbRows = [["場所", s.db_path]];
  let hint = "";
  if (d.state === "ok") {
    const m = d.summary;
    const healthy = m.problems.length === 0;
    dbRows.push(
      ["状態", healthy ? "開けました（検査に問題なし）" : "開けましたが、問題があります", healthy ? "good" : "bad"],
      ["作成日時", localTime(m.created_at)],
      ["作成元", m.source_name || "不明"],
      ["件数", `請求書 ${m.invoices} 件・取引先 ${m.clients} 件・操作履歴 ${m.audit_entries} 件`],
      ["テーブル", `${m.tables} 個`]
    );
    for (const p of m.problems) dbRows.push(["問題", p, "bad"]);
  } else if (d.state === "missing") {
    dbRows.push(["状態", "まだありません"]);
    hint = k.state === "ok" ? "" : "先に、手順1で暗号鍵を用意してください。";
  } else if (d.state === "needs_key") {
    dbRows.push(["状態", "鍵がないため開けません", "bad"]);
    hint = "手順1で、このデータベースを作ったときの鍵を、紙の復旧コードから復元してください。";
  } else {
    dbRows.push(["状態", "開けません", "bad"], ["理由", d.message]);
  }
  setFacts($("db-facts"), dbRows);
  $("db-actions").hidden = !(d.state === "missing" && k.state === "ok");
  $("db-hint").textContent = hint;
  mark("m-db", d.state === "ok" ? (d.summary.problems.length ? "bad" : "done") : d.state === "error" || d.state === "needs_key" ? "bad" : "");

  // ---- バックアップ
  $("btn-backup").disabled = d.state !== "ok";
}

async function refresh() {
  status = await invoke("get_status");
  render();
}

// 最初に開く手順：鍵がなければ1、DBがなければ2、そろっていれば4
function firstStep() {
  if (status.key.state !== "ok") return "key";
  if (status.db.state !== "ok") return "db";
  return "backup";
}

function bind() {
  for (const b of document.querySelectorAll(".step")) {
    b.addEventListener("click", () => { notice(""); show(b.dataset.go); });
  }

  $("btn-import").addEventListener("click", (ev) => run(ev.currentTarget, async () => {
    const path = await invoke("pick_key_file");
    if (!path) return;
    const fp = await invoke("key_import", { path });
    await refresh();
    notice(`鍵ファイルを取り込み、保管しました（鍵の指紋：${fp}）。下の「紙の復旧コードと照合する」で、紙のコードと一致するか確かめてください。確かめたあと、元の鍵ファイル（key.txt）は削除し、ごみ箱も空にしてください。`);
  }));

  $("btn-restore").addEventListener("click", (ev) => run(ev.currentTarget, async () => {
    const code = $("restore-code").value.trim();
    if (!code) throw "復旧コードを入力してください。";
    const fp = await invoke("key_restore", { code });
    $("restore-code").value = "";
    await refresh();
    notice(`復旧コードから鍵を復元し、保管しました（鍵の指紋：${fp}）。`);
  }));

  $("btn-new").addEventListener("click", (ev) => run(ev.currentTarget, async () => {
    const r = await invoke("key_new_begin");
    // 5文字ずつのまとまりの途中で改行しないように、まとまりごとに並べる
    const box = $("new-code");
    const groups = r.recovery_code.split("-");
    box.replaceChildren(...groups.map((g, i) => {
      const span = document.createElement("span");
      span.className = "grp";
      span.textContent = g + (i < groups.length - 1 ? "-" : "");
      return span;
    }));
    $("new-fp").textContent = r.fingerprint;
    $("new-retype").value = "";
    $("key-new").hidden = false;
    $("key-setup").hidden = true;
    notice("");
    $("new-retype").focus();
  }));

  $("btn-new-save").addEventListener("click", (ev) => run(ev.currentTarget, async () => {
    const code = $("new-retype").value.trim();
    if (!code) throw "紙に書き写したコードを入力してください。";
    const fp = await invoke("key_new_confirm", { code });
    $("new-code").textContent = "";
    $("new-retype").value = "";
    $("key-new").hidden = true;
    await refresh();
    notice(`一致しました。鍵を保管しました（鍵の指紋：${fp}）。紙の控えは、このPCとは別の安全な場所に保管してください。`);
  }));

  $("btn-new-cancel").addEventListener("click", (ev) => run(ev.currentTarget, async () => {
    await invoke("key_new_cancel");
    $("new-code").textContent = "";
    $("new-retype").value = "";
    $("key-new").hidden = true;
    await refresh();
    notice("新しい鍵の作成をやめました。何も保存していません。書き写した紙は破棄してください。");
  }));

  $("btn-check").addEventListener("click", (ev) => run(ev.currentTarget, async () => {
    const code = $("check-code").value.trim();
    if (!code) throw "復旧コードを入力してください。";
    const ok = await invoke("key_check_code", { code });
    $("check-code").value = "";
    if (ok) notice("一致しました。このコードで鍵を復元できます。");
    else notice("一致しません。紙のコードの書き写しを確認してください。", true);
  }));

  $("btn-create").addEventListener("click", (ev) => run(ev.currentTarget, async () => {
    const s = await invoke("db_create");
    await refresh();
    notice(`データベースを作りました（テーブル ${s.tables} 個、検査に問題なし）。作った記録は操作履歴に残っています。`);
  }));

  $("btn-backup").addEventListener("click", (ev) => run(ev.currentTarget, async () => {
    const dir = await invoke("pick_folder");
    if (!dir) return;
    const file = await invoke("db_backup", { dir });
    const r = $("backup-result");
    r.textContent = "保存して、開けることを確かめました：" + file;
    r.hidden = false;
    notice("");
  }));
}

window.addEventListener("DOMContentLoaded", async () => {
  bind();
  try {
    await refresh();
    show(firstStep());
  } catch (e) {
    show("key");
    notice(errText(e), true);
  }
});
