# 公众号候选复核

发现阶段会为每个 CSSCI 期刊生成多个检索词，并对候选公众号评分。

- 高于阈值且无冲突的候选会标记为 `auto_accepted` 并进入 `accounts`。
- 低分、同分冲突或证据不足的候选会标记为 `uncertain`。
- 使用 `python main.py review-candidates --status uncertain` 查看待复核候选。

复核时优先检查：公众号名是否与期刊名一致、简介是否含编辑部/期刊/学报/本刊、历史文章是否包含目录/征稿/论文标题等证据。
