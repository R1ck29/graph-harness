# GH-1 最終検証と差分レビュー

検証日: 2026-11-17。Linux / Python 3.13.5。
対象コード: `1c785a6`。検証環境はコミットから作成した独立 worktree。
`python` は Black 26.5.1 / mypy 2.3.1 を導入した検証用仮想環境を指す。

## 再現と回帰

ベースラインは 431 テスト成功（4 skips）。修正前に追加したテストの失敗は
`gh-1-audit.md` とテスト先行コミット `fa0ce0a` に記録済み。
さらに、最終版の回帰テストを `origin/main` (`cdad835`) の別 worktree に
コピーして実行し、7 テスト中 99 failures / 3 errors を再確認した。
不正 status の TypeError、失敗履歴の AttributeError、null 等の証拠受理、
superseded の再開が修正前に再現する。有効な履歴オブジェクトは旧版でも読める。

再開後の最初の全件実行では、新規テストの共通入力一覧に `{}` が含まれ、
オブジェクトを要求する失敗履歴でも拒否を期待する誤りがあった。
非オブジェクトの拒否ケースをすべて維持し、有効なオブジェクトの
保持・読取を別テストにした。既存 431 テストの変更・削除・弱体化はない。

## ローカル検証結果

| コマンド | 結果 |
| --- | --- |
| `python -m pip install .` | 0.1.1 のビルド・導入成功 |
| `graphctl --graph examples/quickstart/task-graph.json validate` | `valid: true` |
| `python -m compileall -q agent_harness scripts adapters evals tests` | 成功 |
| `python -m unittest discover -v` | 438 テスト、OK（4 skips）、19.065 秒 |
| `python -m unittest tests.test_robustness_contract -v` | 回帰・互換テスト 7 件成功 |
| `python -m black --check agent_harness scripts tests adapters evals` | 38 ファイル変更不要 |
| `python -m mypy agent_harness scripts adapters evals tests` | 38 ファイル、エラーなし |
| `python scripts/sync_adapters.py --check` | 成功、生成物変更なし |
| `python evals/runner/run.py` | 5 ケースすべてで3つの protocol checks が true |
| `git diff --check origin/main...HEAD` | 成功 |

4 skips は既存の条件付きケース（チェックアウトに過去グラフがない、
対象 locale がない、Windows 専用 junction 検証2件）。CI と同様に
コミット内容だけの checkout で測定した。作業元には gitignore 対象の
今回の作業グラフがあり、過去の独立 FAIL を要求する既存の
`test_this_repository_records_a_defect_review_caught` は成立しない。
テストやレビュー履歴を書き換えて成功させてはいない。

パッケージの全バージョン宣言（`pyproject.toml` と
`agent_harness/__init__.py`）およびインストール済み metadata は 0.1.1。
データ形式・CLI・設定・配置は据え置き。追加ライブラリなし。

## 更新・ロールバックの実行確認

実ユーザーのホームとは別の一時ホームで、実インストーラーと実ランタイムを
使い、`0.1.0 → 0.1.1 → 0.1.0` の順に再インストールした。
旧版ソースは `cdad835`、新版ソースは `1c785a6`。

- 新版の `install_pc.py --dry-run`、各版のインストールと `--check` が成功。
- 各段階でランタイムの版と既存グラフの `validate` 成功を確認。
- 全段階でグラフの SHA-256 は同一:
  `e29a2cf49f8440d1423959844b11eae25123f94114ef309675b309f88ed9e223`。
- 管理する Codex / Claude Code の連携とランタイムを同時に戻せた。

## 自己レビュー

`origin/main...HEAD` の全差分と呼出元を確認した。

- 読取入力は GraphStore → Graph の共通検証で拒否し、CLI・doctor・Stop hook
  それぞれの既存エラー経路へ渡す。包括的な例外の握り潰しは追加していない。
- 証拠 JSON の型を保持し、既存の必須文字列検証を submit / verify の両方で
  適用する。インラインと @file の不正入力でグラフのバイト列不変を確認。
  文字列 value の互換入力・criterion 補完・追加メタデータの保持を確認。
- descendants は先に全子孫を列挙するため、superseded を飛ばしてもその先の
  子孫の無効化と試行返却は継続する。廃止ノードの全記録と無関係な枝は不変。
- 新しい外部通信・コマンド実行・資格情報処理は追加しない。既存の証拠ファイル
  のパス制約・サイズ制限・通常ファイル限定・アトミック保存は維持する。
- 機能追加や再設計はなく、正常入力の互換性を保つパッチ更新と判断した。

自己レビューで未解決の CRITICAL / HIGH 指摘はない。これは独立レビューの
代わりではない。ユーザーの明示指示に従いサブエージェントは起動せず、
グラフへ独立 PASS を自己発行しない。作業グラフは証拠提出後も独立レビュー待ち。
GitHub CI の全 OS / Python 行列の結果は PR のチェックを参照する。

## 参照資料

- https://github.com/R1ck29/graph-harness/blob/main/core/protocol.md
- https://github.com/R1ck29/graph-harness/blob/main/core/task-graph.schema.json
- https://github.com/R1ck29/graph-harness/blob/main/.github/workflows/ci.yml
- https://github.com/R1ck29/graph-harness/blob/main/scripts/install_pc.py
- https://semver.org/lang/ja/
- https://keepachangelog.com/ja/1.1.0/

## 最終ゲートの状態

`dc17a93` の GitHub CI は push / pull_request の両方で成功。
Ubuntu / macOS / Windows × Python 3.10 / 3.12 / 3.13 の9行列と
静的チェックがすべて成功した。

- https://github.com/R1ck29/graph-harness/actions/runs/43990331708
- https://github.com/R1ck29/graph-harness/actions/runs/43990339875

作業グラフの3 criterion に証拠を関連付け、`graphctl submit gh-1` を実行済み。
レビュー用の `graphctl review-packet gh-1` 出力は
`tasks/artifacts/gh-1-review-packet.json` に保存した。
`graphctl completion-check` は exit 2（`unverified nodes: ['gh-1']`）。
独立レビュー待ちによる未完了であり、自己承認・独立PASSの代筆はしていない。
