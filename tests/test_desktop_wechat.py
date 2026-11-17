import pytest
from crawl.desktop_wechat import DesktopSafetyError, validate_navigation, validate_window

BIZ='Mzk3NTE3NTU0Ng=='

def test_client_navigation_only_allows_public_target_wechat_pages():
    url=f'https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz={BIZ}'
    assert validate_navigation(url,BIZ)==url
    for bad in ['https://example.com/s',url.replace(BIZ,'wrong'),url+'&token=secret','javascript:bad()']:
        with pytest.raises(DesktopSafetyError): validate_navigation(bad,BIZ)

def test_wrong_process_or_chat_window_never_receives_input():
    for process,cls in [('Weixin.exe','Qt51514QWindowIcon'),('chrome.exe','Chrome_WidgetWin_1')]:
        with pytest.raises(DesktopSafetyError): validate_window(process,cls)
    validate_window('WeChatAppEx.exe','Chrome_WidgetWin_0')
