"""Small, guarded driver for the existing WeChat *browser* window on Windows.

It deliberately cannot type into the main chat window. Navigation clicks must be
chosen from a just-inspected page; this module does not guess screen coordinates.
"""
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


class DesktopSafetyError(RuntimeError):
    pass


def validate_window(process_name, class_name):
    if process_name.lower() != 'wechatappex.exe' or class_name != 'Chrome_WidgetWin_0':
        raise DesktopSafetyError('not_the_wechat_browser_window')


def validate_navigation(url, target_biz):
    try:
        u=urlsplit(url); q=parse_qs(u.query)
        if u.scheme!='https' or u.hostname!='mp.weixin.qq.com' or u.username or u.password or u.port:
            raise ValueError()
        if u.path!='/mp/profile_ext' and u.path!='/s' and not u.path.startswith('/s/'):
            raise ValueError()
        if q.get('__biz',[''])[0] != target_biz: raise ValueError()
        if set(q)-{'__biz','mid','idx','sn','action','scene'}: raise ValueError()
    except ValueError:
        raise DesktopSafetyError('unapproved_navigation') from None
    return url


class DesktopWeChatDriver:
    def __init__(self, hwnd):
        try:
            import win32gui, win32process, win32api, win32con
        except ImportError:
            raise DesktopSafetyError('windows_pywin32_not_available') from None
        self.gui, self.proc, self.api, self.con = win32gui, win32process, win32api, win32con
        self.hwnd=int(hwnd)
        self.pid=self.proc.GetWindowThreadProcessId(self.hwnd)[1]
        self._check()

    def _check(self):
        import ctypes
        from ctypes import wintypes
        if not self.gui.IsWindow(self.hwnd) or self.proc.GetWindowThreadProcessId(self.hwnd)[1]!=self.pid:
            raise DesktopSafetyError('window_changed')
        kernel=ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        kernel.QueryFullProcessImageNameW.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
        handle=kernel.OpenProcess(0x1000,False,self.pid)
        if not handle: raise DesktopSafetyError('process_unavailable')
        try:
            size=wintypes.DWORD(32768); buf=ctypes.create_unicode_buffer(size.value)
            if not kernel.QueryFullProcessImageNameW(handle,0,buf,ctypes.byref(size)):
                raise DesktopSafetyError('process_identity_unavailable')
            validate_window(Path(buf.value).name,self.gui.GetClassName(self.hwnd))
        finally: kernel.CloseHandle(handle)

    def focus(self):
        self._check()
        self.gui.ShowWindow(self.hwnd,self.con.SW_RESTORE)
        if self.gui.GetForegroundWindow()!=self.hwnd:
            self.api.keybd_event(self.con.VK_MENU,0,0,0)
            self.api.keybd_event(self.con.VK_MENU,0,self.con.KEYEVENTF_KEYUP,0)
            try: self.gui.SetForegroundWindow(self.hwnd)
            except Exception: pass
            time.sleep(.25)
        self._assert_foreground()

    def _assert_foreground(self):
        self._check()
        if self.gui.GetForegroundWindow()!=self.hwnd:
            raise DesktopSafetyError('focus_mismatch_no_input_sent')

    def click_verified(self, x, y, *, action):
        if action not in {'open_account','open_article','back','history_tab','reload'}:
            raise DesktopSafetyError('unapproved_click_action')
        self.focus()
        left,top,right,bottom=self.gui.GetWindowRect(self.hwnd)
        minimum_y = 10 if action == 'history_tab' else 35
        if not 8<=x<right-left-100 or not minimum_y<=y<bottom-top-8:
            raise DesktopSafetyError('point_outside_content')
        self._assert_foreground()
        self.api.SetCursorPos((left+int(x),top+int(y)))
        self.api.mouse_event(self.con.MOUSEEVENTF_LEFTDOWN,0,0,0,0)
        self.api.mouse_event(self.con.MOUSEEVENTF_LEFTUP,0,0,0,0)

    def scroll(self, notches=-3):
        if not -5<=notches<=5: raise DesktopSafetyError('scroll_out_of_bounds')
        self.focus(); left,top,right,bottom=self.gui.GetWindowRect(self.hwnd)
        self.api.SetCursorPos(((left+right)//2,(top+bottom)//2))
        self._assert_foreground()
        self.api.mouse_event(self.con.MOUSEEVENTF_WHEEL,0,0,int(notches)*120,0)

    def screenshot(self, path):
        # Optional operator evidence; never screenshots the main chat or other apps.
        from PIL import ImageGrab
        self.focus(); self._assert_foreground()
        ImageGrab.grab(bbox=self.gui.GetWindowRect(self.hwnd)).save(path)

    def observe(self, output_path):
        import subprocess
        self.focus()
        result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive',
            '-File',str(Path(__file__).with_name('desktop_observe.ps1')),
            '-WindowHandle',str(self.hwnd),'-OutputPath',str(Path(output_path).resolve())],
            capture_output=True,timeout=30)
        if result.returncode != 0:
            raise DesktopSafetyError('observation_failed_no_error_text_exported')
        return Path(output_path)

    def previous_tab(self):
        self.focus(); self._assert_foreground()
        self.api.keybd_event(self.con.VK_CONTROL,0,0,0)
        self.api.keybd_event(self.con.VK_SHIFT,0,0,0)
        self.api.keybd_event(self.con.VK_TAB,0,0,0)
        self.api.keybd_event(self.con.VK_TAB,0,self.con.KEYEVENTF_KEYUP,0)
        self.api.keybd_event(self.con.VK_SHIFT,0,self.con.KEYEVENTF_KEYUP,0)
        self.api.keybd_event(self.con.VK_CONTROL,0,self.con.KEYEVENTF_KEYUP,0)

