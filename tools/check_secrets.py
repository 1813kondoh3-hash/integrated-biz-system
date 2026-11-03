#!/usr/bin/env python3
"""秘密情報・実データの混入チェック（コミット前フック／CI で使用）

  python tools/check_secrets.py --staged          # コミットしようとしているファイルを検査（pre-commit）
  python tools/check_secrets.py --all             # リポジトリ内の全ファイルを検査（CI）
  python tools/check_secrets.py FILE...           # 指定したファイルを検査
  --strict-names                                  # 会社名らしき文字列も、エラー扱いにする

止めるもの（BLOCK）
  ・DB・鍵・証明書・Excel・CSV などのファイル（実データや鍵が入っている可能性が高い）
  ・64桁の16進数（暗号鍵の形式）、復旧コード、秘密鍵、GitHubのトークン、AWSのキー
  ・実在しうるインボイス登録番号（T＋13桁。見本の T1234567890123・T9876543210987 だけ許可）
  ・クレジットカード番号らしき並び
注意（WARN）
  ・「株式会社○○」などの会社名らしき文字列（見本の会社名は許可）。--strict-names でエラー扱い
値そのものは表示しない（先頭の数文字だけを表示する）。
"""
import argparse, os, re, subprocess, sys

BLOCK_EXT = {".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".key", ".pem", ".pfx", ".p12", ".jks", ".keystore",
             ".xlsx", ".xlsm", ".xls", ".csv", ".tsv", ".bak"}
BLOCK_NAME_RE = re.compile(r"^\.env(\..+)?$")
# ハッシュ値（64桁の16進数）を含んでよい場所：アプリが作った匿名の検証データ、ロックファイルのチェックサム
HEX_ALLOW_PREFIXES = ("db/tests/fixtures/",)
HEX_ALLOW_FILES = {"Cargo.lock", "package-lock.json"}
DUMMY_REGNO = {"T1234567890123", "T9876543210987"}              # 見本データのダミー（桁が単純に並ぶ番号）
SAMPLE_NAME_PARTS = ("サンプル", "テスト", "旧社", "Ｂ建設", "Ｃ工業", "Ａ建設", "○○", "〇〇", "ＸＸ", "XX", "会社名", "見本", "例")

HEX64 = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])")
RECOVERY = re.compile(r"(?<![A-Z2-7])(?:[A-Z2-7]{5}-){10}[A-Z2-7]{5}(?![A-Z2-7])")
PRIVKEY = re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")
GH_TOKEN = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b")
AWS_KEY = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
REGNO = re.compile(r"(?<![0-9A-Za-z])T[0-9]{13}(?![0-9])")
CARD = re.compile(r"(?<![0-9])[0-9]{4}[- ][0-9]{4}[- ][0-9]{4}[- ][0-9]{4}(?![0-9])")
_NAME_CH = r"[^\s,，。、\"'<>()（）\[\]{}：:;；/\\|]"
COMPANY = re.compile(rf"(?:株式会社|有限会社|合同会社)({_NAME_CH}{{1,12}})|({_NAME_CH}{{1,12}})(?:株式会社|有限会社|合同会社)")


def mask(v):
    return v[:4] + "…（" + str(len(v)) + "文字）"


def check_path(path):
    """ファイル名・拡張子の検査。問題があれば説明を返す"""
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    if ext in BLOCK_EXT:
        return f"拡張子 {ext} のファイルは登録できません（実データ・鍵・DBの可能性）"
    if BLOCK_NAME_RE.match(name):
        return "環境設定ファイル（.env）は登録できません"
    return None


def scan_text(path, text):
    """内容の検査。[(レベル, ルール, 行番号, 表示用の値)] を返す"""
    norm = path.replace("\\", "/")
    out = []
    hex_ok = norm.startswith(HEX_ALLOW_PREFIXES) or os.path.basename(norm) in HEX_ALLOW_FILES
    for no, line in enumerate(text.splitlines(), 1):
        if "allow-secret" in line:
            continue
        if not hex_ok:
            for m in HEX64.finditer(line):
                if len(set(m.group().lower())) > 2:                      # 0000…や aaaa… は鍵ではない
                    out.append(("BLOCK", "64桁の16進数（暗号鍵・ハッシュの形式）", no, mask(m.group())))
        for m in RECOVERY.finditer(line):
            out.append(("BLOCK", "復旧コード", no, mask(m.group())))
        if PRIVKEY.search(line):
            out.append(("BLOCK", "秘密鍵", no, "-----BEGIN…"))
        for m in GH_TOKEN.finditer(line):
            out.append(("BLOCK", "GitHubのトークン", no, mask(m.group())))
        for m in AWS_KEY.finditer(line):
            out.append(("BLOCK", "AWSのアクセスキー", no, mask(m.group())))
        for m in REGNO.finditer(line):
            if m.group() not in DUMMY_REGNO:
                out.append(("BLOCK", "インボイス登録番号らしき文字列（実在の会社を特定しうる）", no, mask(m.group())))
        for m in CARD.finditer(line):
            out.append(("BLOCK", "カード番号らしき並び", no, mask(m.group())))
        for m in COMPANY.finditer(line):
            name = m.group(1) or m.group(2) or ""
            has_word = re.search(r"[0-9A-Za-zＡ-Ｚａ-ｚぁ-んァ-ヶー一-龥]", name)         # 記号だけの場合は会社名ではない
            if name and has_word and not any(p in name for p in SAMPLE_NAME_PARTS):
                out.append(("WARN", "会社名らしき文字列", no, mask(m.group())))
    return out


def read_text(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if b"\0" in data:
        return None                                                     # バイナリは内容を検査しない（拡張子のみ）
    return data.decode("utf-8", errors="ignore")


def git_files(mode):
    cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"] if mode == "staged" else ["git", "ls-files"]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        print("gitの実行に失敗しました:", r.stderr.strip(), file=sys.stderr)
        sys.exit(2)
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def main(argv=None):
    ap = argparse.ArgumentParser(description="秘密情報・実データの混入チェック")
    ap.add_argument("files", nargs="*")
    ap.add_argument("--staged", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--strict-names", action="store_true")
    a = ap.parse_args(argv)
    files = a.files or git_files("staged" if a.staged else "all") if (a.files or a.staged or a.all) else []
    if not files:
        print("検査するファイルがありません。")
        return 0
    blocks = warns = 0
    for path in files:
        problem = check_path(path)
        if problem:
            print(f"[BLOCK] {path}: {problem}")
            blocks += 1
            continue
        text = read_text(path)
        if text is None:
            continue
        for level, rule, no, shown in scan_text(path, text):
            if level == "WARN" and not a.strict_names:
                warns += 1
                print(f"[注意] {path}:{no}  {rule}（{shown}）")
            else:
                blocks += 1
                print(f"[BLOCK] {path}:{no}  {rule}（{shown}）")
    if blocks:
        print(f"\n{blocks}件の問題が見つかりました。実データ・鍵は、リポジトリに登録しないでください（docs/security-and-data.md）。")
        print("誤検知の場合は、該当行に allow-secret と書くと除外できます（理由をコメントに残してください）。")
        return 1
    print(f"問題は見つかりませんでした。（検査 {len(files)} ファイル" + (f"／注意 {warns} 件" if warns else "") + "）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
