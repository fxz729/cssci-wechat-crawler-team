"""Portable Excel index for a journal's articles and downloaded original media."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ARTICLE_COLUMNS=(('title','文章标题'),('publish_time','发布时间（北京时间）'),('account_name','公众号'),
  ('biz','biz'),('mid','mid'),('idx','idx'),('sn','sn'),('author','原始作者'),('digest','原始摘要'),
  ('article_url','文章URL'),('cover_url','封面图URL'),('content_origin','正文来源'),
  ('read_count','阅读数'),('like_count','点赞数'),('watching_count','在看数'),('share_count','分享数'),
  ('favorite_count','收藏数'),('comment_count','评论数'),('comment_id','comment_id'),
  ('metrics_source','指标来源'),('business_ret','业务返回码'),('http_status','HTTP状态'),
  ('fiddler_session_id','Fiddler会话'),('metrics_captured_at','指标时间（北京时间）'),
  ('local_html','正文HTML'),('local_text','正文文本'),('local_article_dir','文章文件夹'),('capture_evidence','采集证据'),
  ('image_count','图片数'),('audio_count','音频数'),('video_count','视频数'),
  ('media_downloaded','已下载原文件'),('media_pending','待处理媒体'),('metrics_status','指标状态'),
  ('canonical_url','微信原文'),('content_status','正文状态'))
MEDIA_COLUMNS=(('mid','mid'),('idx','idx'),('kind','媒体类型'),('local_path','原文件'),
  ('status','下载状态'),('bytes','文件字节数'),('sha256','SHA-256'),('source_url','来源URL'),('error_code','失败原因'))
LOCAL_KEYS={'local_html','local_text','local_article_dir','local_path','capture_evidence'}



def _local_target(value, root, workbook_dir):
    p=Path(value)
    if not p.is_absolute():p=root/p
    p=p.resolve()
    if not p.is_relative_to(root):raise ValueError('local_link_outside_bundle')
    if not p.exists():return None
    # Workbook is at the bundle root; relative paths keep a copied bundle portable.
    import os
    return Path(os.path.relpath(p,workbook_dir)).as_posix()


def _date(value):
    if not value:return None
    try:
        dt=value if isinstance(value,datetime) else datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if dt.tzinfo is None:return str(value)  # Do not invent the timezone of unknown data.
        return dt.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
    except ValueError:return str(value)


def _sheet(wb,name,columns,rows,root,destination):
    ws=wb.create_sheet(name)
    ws.append([label for _,label in columns])
    for row in rows:
        row_index=ws.max_row+1
        for col,(key,_) in enumerate(columns,1):
            value=row.get(key)
            if key in {'publish_time','metrics_captured_at'}:value=_date(value)
            if value is not None and key in {'mid','biz','sn'}:value=str(value)
            if isinstance(value,(list,dict)):
                import json
                value=json.dumps(value,ensure_ascii=False)
            cell=ws.cell(row_index,col,value)
            if isinstance(value,str):cell.data_type='s'
            if key in LOCAL_KEYS and value:
                target=_local_target(value,root,destination.parent)
                if target:cell.hyperlink=target;cell.style='Hyperlink'
            if isinstance(value,datetime):cell.number_format='yyyy-mm-dd hh:mm:ss'
            elif isinstance(value,(int,float)):cell.number_format='#,##0'
            cell.alignment=Alignment(vertical='top',wrap_text=True)
        ws.row_dimensions[row_index].height=42
    for cell in ws[1]:
        cell.font=Font(name='微软雅黑',bold=True,color='FFFFFF')
        cell.fill=PatternFill('solid',fgColor='24476A')
        cell.alignment=Alignment(vertical='center',wrap_text=True)
    ws.row_dimensions[1].height=30;ws.freeze_panes='A2';ws.auto_filter.ref=ws.dimensions
    for i,(key,_) in enumerate(columns,1):
        width=44 if key in {'title','digest','canonical_url','source_url','article_url','cover_url'} else 26 if key in LOCAL_KEYS or key in {'publish_time','metrics_captured_at'} else 18
        ws.column_dimensions[get_column_letter(i)].width=width
    return ws


def write_journal_workbook(articles,media,path,*,bundle_root):
    root=Path(bundle_root).resolve();destination=Path(path).resolve()
    if not destination.is_relative_to(root):raise ValueError('workbook_outside_bundle')
    destination.parent.mkdir(parents=True,exist_ok=True)
    wb=Workbook();wb.remove(wb.active)
    _sheet(wb,'文章索引',ARTICLE_COLUMNS,articles,root,destination)
    _sheet(wb,'媒体清单',MEDIA_COLUMNS,media,root,destination)
    ws=wb.create_sheet('说明')
    notes=[('使用方式','点击正文HTML、正文文本、文章文件夹或媒体原文件列即可打开对应本地资源。移动时请复制整个数据目录。'),
      ('原文件口径','保存公众号公开提供的媒体原始响应字节，不截图、不转码；不保证是作者上传前的未压缩母版。'),
      ('空值与零','空白表示未取得；数字0仅在源字段明确返回0时填入。'),
      ('指标时间','阅读等为采集时返回的累计值，不是文章发表时的历史快照。'),
      ('全文保存','全文HTML和文本保存在本地文件中，避免Excel单元格长度限制。'),
      ('接口证据','采集证据列链接到每篇文章的安全事件摘要；其中业务返回码和指标原始路径按响应实际返回记录。'),
      ('媒体失败','下载失败或只有播放器地址的资源保留状态和原因，不生成假的媒体文件。'),
      ('作者与摘要','仅保存原始来源字段，不从标题人名或正文截断推测。')]
    for row in notes:ws.append(row)
    ws.column_dimensions['A'].width=18;ws.column_dimensions['B'].width=100
    for row in ws:
        for cell in row:cell.alignment=Alignment(wrap_text=True,vertical='top');cell.data_type='s'
        ws.row_dimensions[row[0].row].height=42
    wb.save(destination)
    # Reopen validation is deliberately left to caller/tests with the actual bundle.
    return destination
