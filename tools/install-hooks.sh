#!/bin/sh
# コミット前チェック（秘密情報・実データの混入防止）を有効にする
cd "$(dirname "$0")/.." || exit 1
git config core.hooksPath .githooks && echo "有効にしました（core.hooksPath = .githooks）"
