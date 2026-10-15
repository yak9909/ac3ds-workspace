---
name: consolidation
description: 記録（findings）をカテゴリごとに topics へ消化させるときの、オーケストラ側の手順。いつ消化の Issue を作るか、結果をどう確定するか。
---

# 消化

- **きっかけ**: `findings/INDEX.md` で、あるカテゴリの未消化（`state: active` で topics に入っていない記録）が10件を超えたとき。そのカテゴリを使う作業を始める前にも作る。
- **依頼**: 格付け A 以上のワーカーに、`ac3ds-re` のスキル `consolidate-category` で作業させる。旧記録の消化は、「?」の記録の再解析と同時に行い、利用枠の3割までに収める。
- **判定**: topics の更新は深い検証の対象。統合・重複の提案（報告に書かれている）を確かめ、取り込みの仕上げで `state: consolidated`（`consolidated_into`）や `state: duplicate`（`duplicate_of`）を記入する。記録は消さない。
