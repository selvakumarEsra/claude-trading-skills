---
description: "ニュースヘッドラインから18ヶ月シナリオを分析。1次/2次/3次影響、推奨銘柄、セカンドオピニオンを含む包括的レポートを日本語で生成。"
argument-hint: "<headline>"
---

# Scenario Analyzer

ニュースヘッドラインから18ヶ月シナリオを分析し、セクター・銘柄への影響を評価します。

## 引数

```
$ARGUMENTS
```

**引数の解釈**:
- ヘッドラインが含まれる場合: そのヘッドラインを分析対象とする
- 引数が空の場合: ユーザーにヘッドラインの入力を求める

**使用例**:
- `/scenario-analyzer Fed raises rates by 50bp` → Fed利上げシナリオを分析
- `/scenario-analyzer China announces new tariffs on US semiconductors` → 関税シナリオを分析
- `/scenario-analyzer OPEC+ agrees to cut oil production` → 原油減産シナリオを分析
- `/scenario-analyzer` → ヘッドライン入力を求めてから分析

## 分析内容

| 項目 | 説明 |
|------|------|
| **関連ニュース** | WebSearchで過去2週間の関連記事を収集 |
| **シナリオ** | Base/Bull/Bear の3シナリオ（確率付き） |
| **影響分析** | 1次・2次・3次のセクター影響 |
| **銘柄選定** | ポジティブ/ネガティブ各3-5銘柄（米国市場） |
| **レビュー** | セカンドオピニオン（見落とし・バイアス指摘） |

## 実行手順

1. **ヘッドライン解析**:
   - 引数からヘッドラインを抽出
   - 引数が空の場合はユーザーに入力を求める
   - イベントタイプを分類（金融政策/地政学/規制/テクノロジー/コモディティ/企業）

2. **リファレンス読み込み**:
   ```
   Read skills/scenario-analyzer/references/headline_event_patterns.md
   Read skills/scenario-analyzer/references/sector_sensitivity_matrix.md
   Read skills/scenario-analyzer/references/scenario_playbooks.md
   ```

3. **メイン分析（scenario-analyst エージェント）**:
   ```
   Agent tool:
   - subagent_type: "scenario-analyst"
   - prompt: ヘッドライン + イベントタイプ + リファレンス情報
   ```

   出力:
   - 関連ニュース記事リスト
   - 3シナリオ（Base/Bull/Bear）
   - セクター影響分析（1次/2次/3次）
   - 銘柄推奨リスト

4. **セカンドオピニオン（strategy-reviewer エージェント）**:
   ```
   Agent tool:
   - subagent_type: "strategy-reviewer"
   - prompt: Step 3の分析結果全文
   ```

   出力:
   - 見落としの指摘
   - シナリオ確率への意見
   - バイアスの検出
   - 代替シナリオの提案

5. **レポート生成**:
   - 両エージェントの結果を統合
   - 最終投資判断を追記
   - `reports/scenario_analysis_<topic>_YYYYMMDD.md` に保存

## 参照リソース

- `skills/scenario-analyzer/references/headline_event_patterns.md` - イベントパターン
- `skills/scenario-analyzer/references/sector_sensitivity_matrix.md` - セクター感応度
- `skills/scenario-analyzer/references/scenario_playbooks.md` - シナリオテンプレート

## 重要な指示

- **言語**: 全ての分析・出力は**日本語**で行う
- **対象市場**: 銘柄選定は**米国市場上場銘柄のみ**
- **時間軸**: シナリオは**18ヶ月**を対象
- **確率**: Base + Bull + Bear = **100%**
- **セカンドオピニオン**: **必須**で実行（常にstrategy-reviewerを呼び出す）

## 出力

最終的に `ヘッドライン・シナリオ分析レポート` を生成し、以下を含める：
- 関連ニュース記事
- 想定シナリオ概要（18ヶ月後まで）
- セクター・業種への影響（1次/2次/3次）
- ポジティブ影響銘柄（3-5銘柄）
- ネガティブ影響銘柄（3-5銘柄）
- セカンドオピニオン・レビュー
- 最終投資判断・示唆
