# GH-1 監査・回帰テスト記録

## ベースライン

- 対象: `origin/main`（作業開始時）。Python 3.13.5 / Linux。
- `python3 -m unittest discover -v`: 431 件成功、4 件スキップ。
- 現行パッケージバージョンは `pyproject.toml` と
  `agent_harness/__init__.py` の 0.1.0。

## 修正前の再現

`python3 -m unittest tests.test_robustness_contract -v` を実行し、
6 テスト中、サブテストを含め 99 failures / 3 errors を確認した。
この時点で本体コードは未変更。

1. **不正なグラフを読むと例外が漏れる**:
   `status: []` / `status: {}` が `_validate_node` の集合照合で
   `TypeError: unhashable type` になる。
   `failure_history: [null]` は検証を通り、`failures` の `.get()` で
   `AttributeError` になる。GraphStore、各 CLI 読取コマンド、
   doctor、Stop hook を通じた明確な検証エラーを回帰テストの条件にした。
2. **証拠 JSON の型が消える**:
   `_evidence` がインラインと `@file` の両方で全値を `str()` に変換する。
   `summary: null` が `"None"` となり submit / verify に受理される。
   criterion、kind、summary、および互換入力 value の不正型拒否と、
   拒否時のグラフファイル不変を確認する。
   省略された単一 criterion と文字列 value の互換性も検証する。
3. **廃止済みノードが再開する**:
   verified な上流ノードを別枝の FAIL で原因に指定すると、
   `superseded` の子孫も `_invalidate_descendants` が `invalidated` にする。
   廃止済みノードの全記録を維持し、その先の実作業には無効化と試行返却が
   届くことを確認する。無関係な枝も不変であることを条件にした。

## レビュー方針

既存の共通検証境界に修正を集約する。CLI ごとに広い例外を握り潰す対応は
行わない。形式バージョン、フラグ、設定、配置は維持する。
ユーザー指示によりサブエージェントは起動せず、独立 PASS を自己発行しない。
最終的なコード・セキュリティ観点の差分レビューと実行結果を追記する。
