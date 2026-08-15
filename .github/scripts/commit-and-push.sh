#!/usr/bin/env bash
# 生成物を含むコミットを、他のワークフローと競合しても必ず着地させる。
#
# 使い方: commit-and-push.sh <ブランチ名> <コミットメッセージ> <対象パス...>
#
# なぜ rebase を使わないか:
#   out/ は articles/ から決定的に作り直せる生成物なので、リモートの古い out/ と
#   衝突しても「作り直した側」が常に正しい。それでも rebase すると衝突で止まり、
#   以降のリトライが全部「unmerged files」で失敗する（実際に踏んだ）。
#   HEAD と index をリモートに合わせ直してから、自分が作ったファイルだけを
#   載せ直す方式にすれば、衝突そのものが起こらない。

set -uo pipefail

branch="$1"; shift
message="$1"; shift
paths=("$@")

git config user.name  "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

for attempt in 1 2 3 4 5; do
  git fetch origin "$branch" || true
  # HEAD と index だけをリモートに合わせる（作業ツリーの生成物はそのまま残る）
  git reset --mixed "origin/$branch" >/dev/null

  git add -- "${paths[@]}"
  if git diff --staged --quiet; then
    echo "変更なし"
    exit 0
  fi

  count=$(git diff --staged --name-only -- "${paths[@]}" | wc -l | tr -d ' ')
  git commit -q -m "${message//<COUNT>/$count}"

  if git push origin "HEAD:$branch"; then
    echo "push 成功（試行 $attempt / 変更 $count ファイル）"
    exit 0
  fi
  echo "push 失敗。リモートを取り込み直して再試行（$attempt）"
  sleep $((2 ** attempt))
done

echo "::error::5回試しても push できませんでした"
exit 1
