# ds4meter

ds4-server の前に挟むトークン計測プロキシと、その記録を見る TUI。

- `serve.py` は ds4-server を起動し、元のポートでプロキシを待ち受ける。
- `proxy.py` は usage を SQLite（`usage.db`）に記録する。
- `tui.py` は `usage.db` を直接読んで表示する。

## テスト

```sh
cd tests && python3 -m unittest
```

画面やプロキシの挙動を意図して変えたときだけ、`UPDATE_GOLDEN=1` を付けて `tests/golden/` を記録し直す。差分はコミット前に必ず読む。

## ルール表

| ルール | 強制しているもの |
|---|---|
| 今の選択: 実行時のコードは Python 標準ライブラリだけで書く。運用者に決められたルールではなく、Rust が入っているので見直してよい | `tests/test_repo_rules.py` の `test_stdlib_only`（選択を変えるときはこのテストも変える） |
| CLI フラグを足す前に運用者に聞く。調整値はまずコード内の定数にする。`APPROVED_FLAGS` は運用者の承認なしに変えない | `tests/test_repo_rules.py` の `test_no_unapproved_cli_flags` |
| リファクタは色付き出力と両方の下段タブを含めて挙動を変えない | `tests/test_dashboard.py` と `tests/test_proxy.py` の golden |
| 画面の不具合を直すときは、先に不具合で失敗するテストを書く。`UPDATE_GOLDEN=1` での記録し直しは修正の確認にならない | `tests/test_dashboard.py` の `Layout`（幅 60〜140 の全幅で文言の欠けと重なりを検査）。新しい種類の不具合は判断 |
| クライアントの設定変更を要求しない。プロキシは `serve.py` 経由で元のポートを引き継ぐ | なし（判断） |
| 画面の数値には単位を出す。段階分けは固定のしきい値にし、相対値にしない。空の画面では次にやることを出す | なし（判断） |
| UI は TUI だけ。Web UI は作らない | なし（判断） |
| 利用者に渡すコマンドは絶対パスで書く | なし（判断） |
| ツールが無いと言う前に、名前の挙がったツールそのものを調べる。候補を自分で選んで調べた結果は、無いことの根拠にならない | なし（判断） |
