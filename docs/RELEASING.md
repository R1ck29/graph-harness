# リリース・アップグレード・ロールバック

対象はパッケージ `graph-engineering-agent-harness` と、Codex / Claude Code
向けの共通インストールです。会社環境に既存版が導入済みであることを前提に、
既存ワークスペースを保持して更新します。Python 3.10 以上が必要です。

## バージョンの決め方

番号は `MAJOR.MINOR.PATCH` です。本プロジェクトでは 0.x の間も以下の
互換性方針を適用します。

| 変更 | 更新する番号 | 必要な対応 |
| --- | --- | --- |
| 既存の正常な入力と操作を維持する不具合修正 | PATCH | 更新手順と回帰テスト |
| 後方互換な機能追加 | MINOR | 追加機能と既存利用者への影響を記載 |
| CLI、設定、ファイル形式、配置の破壊的変更、または根本的な再設計 | MAJOR | 日本語の `docs/MIGRATION.md` と実行可能な移行経路 |

破壊的変更では、現行版からの段階的なアップグレードを記載し、互換処理、
非推奨警告、またはデータ移行コマンドを提供します。会社環境で使う既存データを
捨てて再作成することを移行手順にしてはいけません。

パッケージ番号は次の 2 か所を同じ値にします。

- `pyproject.toml` の `[project].version`
- `agent_harness/__init__.py` の `__version__`

グラフの `version`、インストーラーの manifest の `version`、レポート等の
`schema_version` は独立したデータ形式番号です。パッケージ更新だけでは
増やしません。今回は `0.1.0` → `0.1.1` のパッチ更新で、移行は不要です。

## リリース前の確認

1. 最新のデフォルトブランチから作業ブランチを作り、差分・互換性・
   バージョンの分類を確認します。
2. 不具合を失敗するテストで再現してから修正し、既存テストも維持します。
3. バージョン 2 か所と日本語の `CHANGELOG.md` を更新します。
   自動公開が参照する見出しは必ず `## [X.Y.Z]` の形にします
   （今回なら `## [0.1.1]`）。修正・追加などの区分と
   「アップグレード方法」を記載します。
4. skill を変更した場合は正本の `skills/` を編集し、
   `python scripts/sync_adapters.py` を実行して生成物を同じコミットに含めます。
   `.agents/`、`.claude/`、`adapters/` のコピーを直接編集しません。
5. CI と同じ次のコマンドをクリーンなチェックアウトで実行します。
   以下の `python` は検証用仮想環境の Python です。

```bash
python -m pip install ".[dev]"
graphctl --graph examples/quickstart/task-graph.json validate
python -m compileall -q agent_harness scripts adapters evals tests
python -m unittest discover -v
python scripts/sync_adapters.py --check
python evals/runner/run.py
python -m black --check agent_harness scripts tests adapters evals
python -m mypy agent_harness scripts adapters evals tests
```

作業グラフを使った場合は、独立レビュー後にそのディレクトリから
`graphctl completion-check` も実行します。レビューを受けていないグラフに
自己承認の PASS を付けてはいけません。

6. PR に概要、変更点、互換性・アップグレード方法、実行コマンドと結果、
   調査した資料を記載し、CI の全 OS / Python 行列とレビューを確認します。
   ローカル Linux の成功だけで Windows / macOS の成功を断定しません。
7. 承認された PR のマージ後、バージョンと CHANGELOG の節を使う自動公開に
   任せます。作業者はタグ・リリースを手動作成しません。自動公開が止まった
   場合はログを調査し、既存版の成果物を上書きせず、管理者に対応を依頼します。
8. 公開された成果物の版と CHANGELOG の対応を確認し、別の仮想環境で
   インストールとサンプルの `validate` を確認します。

## 既存環境のアップグレード

### 共通の準備

1. Codex / Claude Code の作業と `graphctl` の書込処理を終了します。
2. 利用中の Python のパスと以下の版を記録し、旧版のソースまたは配布物を
   確保します。`graphctl --version` フラグは存在しないため使いません。

```bash
python -c "import sys, agent_harness; print(sys.executable); print(agent_harness.__version__)"
```

3. 各プロジェクトの `task-graph*.json` と参照する証拠ファイルをバックアップ
   します。独自配置のグラフも含め、更新中に書込がない状態でコピーします。
   PC 共通インストールでは利用者ホームの `.codex/AGENTS.md`、
   `.codex/hooks.json`、`.claude/CLAUDE.md`、`.claude/settings.json`、
   `.local/share/graph-engineering-agent-harness/manifest.json` も、存在するものを
   保管します。独自の `--home` / `--install-root` を使った場合はその配置を
   記録します。バックアップは機密情報を含み得るため共有リポジトリに入れません。

### 仮想環境への通常インストール・ダウンロード済みソース

0.1.1 の信頼できるソースを別ディレクトリへ展開し、既存の仮想環境を
有効にして、そのソースのルートで以下を実行します。既存ワークスペースを
ソースの展開先にして上書きしないでください。

```bash
python -m pip install --upgrade .
python -c "from importlib.metadata import version; import agent_harness; print(version('graph-engineering-agent-harness')); print(agent_harness.__version__)"
graphctl --graph examples/quickstart/task-graph.json validate
```

両方の版が `0.1.1` であることを確認します。Windows でも既存仮想環境の
`Scripts/python.exe` を使い、`graphctl.exe` が同じ環境にあることを確認します。
企業内ミラー・承認済み配布物を使う環境では、組織の配布経路を使って同じ版を
導入します。外部レジストリにこのパッケージが公開済みとは仮定しません。

### PC 共通インストール

0.1.1 のソースのルートで、これまでと同じ利用者・配置を指定して実行します。
POSIX の標準配置では以下のとおりです。Windows では対応する Python 3.10
以上の実行ファイル（例: `py -3.10`）に置き換えます。

```bash
python3 scripts/install_pc.py --dry-run
python3 scripts/install_pc.py
python3 scripts/install_pc.py --check
```

独自配置なら各コマンドに同じ `--home` / `--install-root` を指定します。
通常は `--runtime-bin-dir` を指定しません。この引数は既存ランタイムを
利用するためのもので、パッケージの再インストールを省略するためです。
管理対象外の同名ファイルとの衝突が報告されたら、配置を確認してから対応します。

インストーラーは既存ランタイムへ再インストールし、管理する skill・reviewer・
hook・指示ブロックを更新します。設定が変わる場合のバックアップは
`<install-root>/backups/<timestamp>/` にありますが、これは旧パッケージの
完全なバックアップではありません。

標準 POSIX 配置の実行版は次で確認します。Windows では `venv/Scripts/python.exe`、
独自配置ではそのランタイムを指定してください。

```bash
~/.local/share/graph-engineering-agent-harness/venv/bin/python -c "import agent_harness; print(agent_harness.__version__)"
```

最後に既存プロジェクトのグラフがあるディレクトリから `graphctl validate` と
`graphctl doctor` を実行します。未完了のグラフの `completion-check` は失敗が
正常なので、アップグレード成功を判定する条件にしません。

### 0.1.1 の入力の注意点

証拠の `criterion`、`kind`、`summary` は空でない文字列で指定します。
`summary` を省略した場合の文字列 `value` と、単一 criterion の自動補完は
引き続き使えます。必須値として送っていた `null` 等は送信元で正しい文字列に
修正してください。追加メタデータの JSON 型は保持されます。

すでに旧版が保存した `"None"` は通常の文字列と区別できないため自動修復
しません。`status` の不正型や失敗履歴中の非オブジェクトが見つかった場合は、
エラーのノード・フィールドとバックアップを確認し、正しい記録を復元します。
履歴を消したりグラフを再作成して試行回数をリセットしたりしないでください。

## ロールバック

1. 作業セッションと書込を止め、不具合時点のグラフ・証拠・設定も別に保管します。
2. 通常の仮想環境では、確保しておいた 0.1.0 のソースのルートから、同じ
   Python で `python -m pip install --force-reinstall --no-deps .` を実行します。
3. PC 共通インストールでは、0.1.0 のソースから同じ利用者・配置へ
   `python3 scripts/install_pc.py` を再実行し、旧版のコードと管理対象を
   一緒に戻します。ランタイムだけを戻して新しい payload を残さないでください。
4. 実行版が 0.1.0 に戻ったこと、旧版の `install_pc.py --check` と既存
   グラフの `validate` が成功することを確認します。既存グラフは v1 のまま
   なので、このパッチ更新から戻す際のデータ逆変換は不要です。
5. データ破損が確認された場合だけ、書込停止中にグラフと対応する証拠を
   同じ時点のバックアップから復元します。正常な更新後の作業を不用意に
   古いバックアップで上書きしないでください。設定を手動復元する場合も、
   導入後に追加された組織固有の設定との差分を確認してから反映します。

`--uninstall` は管理する連携を外しますが、ランタイムを保持します。
それだけでは旧バージョンに戻りません。公開済み版の成果物を差し替えたり、
タグを移動したりせず、修正は次のパッチ番号の PR で提供します。

## 参考資料

- [Semantic Versioning 日本語版](https://semver.org/lang/ja/)
- [Keep a Changelog 日本語版](https://keepachangelog.com/ja/1.1.0/)
- [CI の検証定義](../.github/workflows/ci.yml)
- [インストーラー実装](../scripts/install_pc.py)
- [グラフの検証手順](../core/protocol.md)
