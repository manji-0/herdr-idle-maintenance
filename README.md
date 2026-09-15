# herdr-idle-maintenance

[Herdr](https://herdr.dev) のペインで動いている Claude Code / Cursor のセッションが 30 分放置されたら、引き継ぎ用のサマリを自動で書き出させる常駐ワーカーです。

## 何を解決するか

長く走らせたエージェントセッションは、放置している間に価値が失われていきます。コンテキストが自動圧縮されて経緯が消える、ターミナルを閉じてしまう、マシンを再起動する、といった形で「何をどこまでやったか」が失われます。翌朝に再開しようとしても、前提を思い出すところからやり直しになります。

この仕組みは、セッションが一定時間アイドルになった時点で、エージェント自身に引き継ぎサマリを Markdown で書かせてディスクに残します。残っていれば、新しいセッションにそのファイルを読ませるだけで再開できます。

Claude Code には「圧縮も破棄もせず、指定した絶対パスにハンドオフサマリを書け」というプロンプトを送ります。Cursor には `/summarize` を送ります。

## 仕組み

2 つの部品で構成されています。

**記録側（フック）** — エージェントが応答を終えるたびに、Claude Code の `Stop` フックと Cursor の `stop` フックから `record-stop.py` が呼ばれ、「いつ応答が終わったか」をペインごとの state ファイルに書きます。

**発火側（launchd）** — `run-maintenance.py` が 60 秒ごとに起動し、state を走査して 30 分以上応答が無いペインを探します。見つかったら `herdr agent prompt` でサマリ作成を依頼します。

```
エージェントが応答を終える
  └─ Stop フック → record-stop.py
       └─ state/<pane_idのhash>.json に last_response_at と generation を記録

launchd（60秒ごと）
  └─ run-maintenance.py
       ├─ last_response_at から 30 分未満 → 何もしない
       ├─ この generation は処理済み → 何もしない
       ├─ herdr agent get で idle/done を確認 → 違えば見送り
       └─ herdr agent prompt でサマリ作成を依頼（--wait）
            └─ claude-summaries/<session_id>-<timestamp>.md
```

対象は Herdr のペイン内で起動したセッションだけです。`record-stop.py` は `HERDR_ENV=1` と `HERDR_PANE_ID` が無ければ即座に終了するため、通常のターミナルで起動したセッションには何も起きません。

## 前提

- macOS（launchd を使います）
- [Herdr](https://herdr.dev) がインストール済みで、`herdr integration install claude` / `herdr integration install cursor` が済んでいること
- `python3`（標準ライブラリのみ使用。macOS 同梱の `/usr/bin/python3` で動きます）
- `jq`（フック設定の自動追記に使用。無い場合は手で貼る手順を表示します）

## インストール

```sh
git clone https://github.com/manji-0/herdr-idle-maintenance.git
cd herdr-idle-maintenance
./install.sh
```

インストーラは次の 4 つを行います。

1. `lib/*.py` を `~/.local/lib/herdr-idle-maintenance/` へ配置
2. launchd plist を `~/Library/LaunchAgents/dev.herdr.idle-maintenance.plist` に生成して `launchctl bootstrap`
3. `~/.claude/settings.json` に `Stop` フックと、サマリ出力先への `Write` 許可を追記
4. `~/.cursor/hooks.json` に `stop` フックを追記

設定ファイルは書き換える前に `*.bak.<timestamp>` へバックアップします。同じフックが既にあれば入れ替えるので、再実行しても重複しません。

インストール後、起動中のセッションはフックを読み込んでいないため、一度再起動してください。

### オプション

```sh
./install.sh --idle-seconds 900    # アイドル判定を 15 分に
./install.sh --interval 30         # 30 秒ごとに監視
./install.sh --no-hooks            # ワーカーだけ入れ、設定ファイルは触らない
```

`~/.claude` や `~/.cursor` が無い場合、その側の設定は自動でスキップします。片方だけ使っている環境でもそのまま実行できます。

## 動作確認

launchd に登録されたかを確認します。

```sh
launchctl list | grep dev.herdr.idle-maintenance
```

ワーカーのログを見ます。プロンプトを送ったときに 1 行出ます。

```sh
tail -f ~/.local/share/herdr-idle-maintenance/launchd.log
```

記録側が動いているかは、Herdr ペイン内のセッションで 1 往復したあとに state を見れば分かります。

```sh
cat ~/.local/share/herdr-idle-maintenance/state/*.json
```

すぐ試したい場合は、アイドル判定を短くして入れ直すのが手軽です。

```sh
./install.sh --idle-seconds 60 --interval 10
```

## 設定

環境変数はワーカーとフックの両方が読みます。ワーカーへ恒久的に渡すには plist の `EnvironmentVariables` を編集するか、`install.sh` のオプションを使ってください。

| 変数 | 既定値 | 内容 |
| --- | --- | --- |
| `HERDR_IDLE_SECONDS` | `1800` | この秒数だけ応答が無ければサマリを依頼する |
| `HERDR_IDLE_COMMAND_TIMEOUT_SECONDS` | `600` | サマリ作成の完了を待つ上限 |
| `HERDR_BIN` | `~/.local/bin/herdr` | herdr バイナリの場所 |
| `HERDR_IDLE_MAINTENANCE_STATE_DIR` | `~/.local/share/herdr-idle-maintenance/state` | state の出力先 |
| `HERDR_IDLE_MAINTENANCE_SUMMARY_DIR` | `~/.local/share/herdr-idle-maintenance/claude-summaries` | サマリの出力先 |

`HERDR_IDLE_SECONDS` に `0` 以下を渡すとワーカーは何もせず終了します。一時的に止めたいときに使えます。

## 生成されるサマリ

`~/.local/share/herdr-idle-maintenance/claude-summaries/<session_id>-<timestamp>.md` に出力されます。Claude Code へ送るプロンプトでは、次の内容を残すよう指示しています。

目的、現在の状態、決定とその理由、変更したファイル、実行したコマンドとテストの結果、未解決の問題、次の具体的な手順です。新しいセッションに読ませて続きから作業できることを狙っています。

Cursor 側は `/summarize` を送るだけなので、出力先と形式は Cursor の挙動に従います。このディレクトリには出ません。

## 二重送信をどう防いでいるか

サマリ依頼が重複すると、エージェントに無駄なターンを踏ませることになります。次の 3 段で抑えています。

**generation** — 応答のたびに連番が増えます。ワーカーは送信前に `handled_generation` を立てるので、同じ応答に対して 2 回送りません。

**ロック** — state ごとの `.lock` と、ワーカー全体の `watcher.lock` を `flock` で押さえます。launchd の起動が重なっても 1 つしか進みません。

**送信前の実機確認** — `herdr agent get` の結果に期待するエージェント名と `idle` または `done` が含まれ、かつ session_id が一致することを確認します。ペインの中身が別のものに入れ替わっていた場合は送りません。

送信が失敗したときは `handled_generation` を戻すので、次の巡回で再試行します。Herdr は working / blocked のエージェントへの送信を拒否するため、作業中に割り込むことはありません。

記録側にも除外条件があります。サブエージェント（`agent_id` あり）の Stop、バックグラウンドタスクやセッション cron が残っている Stop は無視します。後者は本当に落ち着いたあとに改めて Stop が飛ぶためです。

## アンインストール

```sh
./uninstall.sh
```

launchd agent の解除、plist と `~/.local/lib/herdr-idle-maintenance/` の削除、フック設定の除去を行います。記録済みの state と生成済みサマリは残します。まとめて消す場合は `--purge` を付けてください。

## 既知の制約

launchd 前提なので macOS 専用です。Linux で使う場合は systemd timer などに置き換えてください。`run-maintenance.py` 自体は Linux でも動きます。

サマリ作成はエージェントのターンを 1 回消費します。トークンを消費し、レート制限も消費します。アイドル判定を短くしすぎると効いてきます。

サマリにはその時点の作業内容が平文で書かれます。出力先はローカルのホームディレクトリ配下ですが、扱う対象が機密であれば出力先の管理に注意してください。

Cursor への `/summarize` は Cursor 側のコマンドに依存します。使えない環境ではプロンプトがそのまま送られます。

## 関連

Herdr は AI コーディングエージェント向けのターミナルワークスペースマネージャです。本リポジトリはその CLI（`herdr agent get` / `herdr agent prompt`）を利用する非公式の拡張で、Herdr 本体のコードは含みません。Herdr プロジェクトとは無関係です。

## ライセンス

MIT
