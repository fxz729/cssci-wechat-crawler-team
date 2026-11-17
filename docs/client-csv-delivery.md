# CSV结果及附件目录

## 当前可查看的数据（不是全126刊交付）

1. `output/journal_validation_20260906/bundle/文章数据.csv`：175行、72列，对应原始单刊基线；媒体1039条。
2. `output/rollout/client_20260907/journals/001_当代世界社会主义问题/bundle/文章数据.csv`：171行、72列，对应第二次单刊采集；媒体1026条，缺少基线的4篇，不能认定全历史。
3. 两个目录均有`媒体索引.csv`，每行资源通过article_key、mid、idx、article_title关联文章。
4. 第2刊尚无正式采集结果，不存在已完成的第2刊CSV。

## 字段契约

CSV沿用`export/realtime_exporter.py:default_export_headers([])`全部50列，并追加22列便于定位文件/核验证据。

- `digest`：原始摘要，不从正文推断；空值表示来源没有返回。
- `content`、`content_text`：完整正文文本，保留段落，不截断，不以摘要替代。
- `content_html`、`content_fragment_html`：正文HTML。
- `account_name`、`author`、`title`、`publish_time`、`biz`、`mid`、`idx`、`sn`、`comment_id`：文章/公众号身份与元数据。
- `read_count`、`like_count`、`watching_count`、`share_count`、`favorite_count`、`comment_count`：采集时点的六项原始指标；零与空值严格区分。
- `metric_fields`、`metric_field_status`、`field_provenance`：字段路径及来源说明，复杂结构以JSON放在单元格。
- `local_html`、`local_text`、`local_readable_html`、`local_article_dir`：本地正文与文章文件夹。
- `media_local_paths`、`image_local_paths`、`audio_local_paths`、`video_local_paths`：已下载原文件的相对路径JSON列表。
- `media_downloaded`、`media_pending`、`media_manifest_status`：媒体状态；无媒体清单不冒充全部完成。

路径均相对于CSV所在bundle文件夹。迁移数据请连同`articles/`文件夹一起复制，不要单独移动CSV。

## 编码与打开方式

UTF-8 BOM，标准CSV引用规则。正文换行合法地保存在引用单元格中，文件物理行数不等于文章行数。
CSV本身无Excel单元格长度上限，不截断长文；用Python/R/Stata读取分析时可保留全文。用Excel查看超长字段可能受Excel自身单元格限制，不代表CSV丢失。
保留原文，不生成可执行超链接公式；用表格软件导入时将文本列按文本处理，特别是以`=`等字符开头的源文本。

## 单独重新导出（不启动微信采集、不联网）

```powershell
python -X utf8 -B main.py export-client-csv --bundle output/journal_validation_20260906/bundle
```

采集入口每处理10项及正常/异常退出时更新CSV；媒体完成后由整体验收阶段重新更新媒体路径。强制杀进程时CSV可能比原始数据稍旧，以上命令可从已保存metadata重建。

## 放行状态

开发硬化中。尚未完成跨刊连续运行与重启后的现场验收，暂不宣称稳定版已经交付。全量采集由用户在最终验收后手动启动。
