# ds4meter

ds4-server のトークン使用量を記録して、ターミナル（TUI）で日別に見るためのツール。
Python 3 標準ライブラリのみ・依存なし。

## 起動

いつもの ds4-server の起動コマンドの頭に `python3 serve.py` を付けるだけ。

```sh
python3 serve.py ./ds4-server --ctx 100000 --host 0.0.0.0 --port 8001
```

- `--host` / `--port` はプロキシが引き取り、ds4-server 本体は `127.0.0.1:(port+10000)` に下げて起動する
- **クライアントの設定は変えなくていい**（今まで通り :8001 につなぐだけで記録される）
- Ctrl-C で両方止まる。DB の場所は `USAGE_DB` で変更可（デフォルト `./usage.db`）

## 見る

```sh
python3 tui.py           # 別ターミナルで
python3 tui.py --once    # 1回だけテキストで出力
```

キー: `1`〜`5` 期間（1/7/30/90/365日。1日は1時間ごとの表） / `tab` 下段切替（最近のリクエスト ⇔ クライアント・モデル別） / `j` `k`（`↑` `↓`）下段スクロール / `r` 更新 / `q` 終了。5秒ごとに自動更新。

## 仕組み

- 計測対象: `/v1/chat/completions`, `/v1/completions`, `/v1/responses`, `/v1/messages`（ストリーミング含む）。それ以外は素通し
- Chat/Completions のストリームには `stream_options.include_usage` を自動注入し、追加された usage チャンクはクライアントに返さない
- クライアント名は User-Agent の先頭（バージョン抜き）
- プロキシ単体で使うなら `python3 proxy.py --listen 8002 --upstream http://127.0.0.1:8001`
