---
name: consolidation
description: 記録（findings）をカテゴリごとに topics へ消化させるときの、司令塔側の手順。いつ行うか、結果をどう確定するか。
---

# 消化

- **きっかけ**: カテゴリを使う作業の前にだけ行う。自動では行わない。
- **依頼**: 格付け A 以上のワーカーに、`ac3ds-re` のスキル `consolidate-category` で作業させる。旧記録の消化は、「?」の記録の再解析と同時に行い、利用枠の3割までに収める。
- **判定**: topics の更新は深い検証の対象。統合・重複の提案（報告に書かれている）を確かめ、取り込みの仕上げで `state: consolidated`（`consolidated_into`）や `state: duplicate`（`duplicate_of`）を記入する。記録は消さない。
