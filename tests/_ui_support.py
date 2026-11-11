"""界面测试的公共准备：没有 tkinter 时装替身，并把 window.json 改道到临时目录。

为什么要有替身：界面冒烟测试要抓的是渲染函数里的 NameError / AttributeError
（引用了不存在的变量、空列表时变量未定义），这类错误跟像素无关。精简版 Python
（没带 `_tkinter`）或 CI 容器里 `import tkinter` 就失败，测试只能整组跳过 ——
等于把最容易出错的一层放空了。

替身只记控件树和配置，不画像素、不跑事件循环：after/after_idle 只登记不执行，
否则 _tick 会把自己排进下一拍，一路递归下去。
"""

from __future__ import annotations

import re
import sys
import tempfile
import types
from pathlib import Path

_STUB_MARK = "IS_FAKE_TK"
_TMP: tempfile.TemporaryDirectory | None = None

def _tmp_dir() -> Path:
    global _TMP
    if _TMP is None:
        _TMP = tempfile.TemporaryDirectory(prefix="hsai-ui-test-")
    return Path(_TMP.name)

class TclError(Exception):
    """对应 tkinter.TclError：界面代码靠它做降级（透明色不支持之类）。"""

class _Base:
    """控件替身：记住配置、子控件和 pack 状态，够界面代码跑完整一遍渲染。"""

    def __init__(self, master: object = None, **opts: object) -> None:
        self._master = master
        self._opts = dict(opts)
        self._layout: dict = {}
        self._binds: dict = {}
        self._children: list[_Base] = []
        self._packed = False
        self._alive = True
        self._attrs: dict = {}
        self._req = (260, 200)      # 请求尺寸：_apply_geometry 会读它
        if isinstance(master, _Base):
            master._children.append(self)

    def pack(self, **kw: object) -> None:
        self._packed = True
        self._layout = kw

    def pack_forget(self) -> None:
        self._packed = False

    grid = pack
    place = pack

    def configure(self, **kw: object) -> None:
        self._opts.update(kw)

    config = configure

    def cget(self, key: str) -> object:
        if key in self._opts:
            return self._opts[key]
        raise TclError(f"unknown option {key!r}")

    def __getitem__(self, key: str) -> object:
        return self.cget(key)

    def bind(self, seq: str, func: object = None, add: object = None) -> None:
        self._binds[seq] = func

    def bind_all(self, seq: str, func: object = None, add: object = None) -> None:
        self._binds[seq] = func

    def after(self, delay: int, func: object = None, *args: object) -> int:
        return 0

    def after_idle(self, func: object = None, *args: object) -> int:
        return 0

    def tk_popup(self, *args: object, **kw: object) -> None:
        return None

    def winfo_children(self) -> list[_Base]:
        return list(self._children)

    def winfo_ismapped(self) -> bool:
        return self._packed

    def winfo_width(self) -> int:
        return self._req[0]

    def winfo_height(self) -> int:
        return self._req[1]

    def winfo_reqwidth(self) -> int:
        return self._req[0]

    def winfo_reqheight(self) -> int:
        return self._req[1]

    def winfo_x(self) -> int:
        return 40

    def winfo_y(self) -> int:
        return 60

    def winfo_screenwidth(self) -> int:
        return 1920

    def winfo_screenheight(self) -> int:
        return 1080

    def title(self, text: str | None = None) -> str | None:
        if text is None:
            return self._attrs.get("title", "")
        self._attrs["title"] = text
        return None

    def attributes(self, *args: object) -> object:
        if len(args) == 1:
            return self._attrs.get(args[0])
        self._attrs[args[0]] = args[1]
        return None

    def overrideredirect(self, flag: object = None) -> bool | None:
        if flag is None:
            return bool(self._attrs.get("overrideredirect"))
        self._attrs["overrideredirect"] = bool(flag)
        return None

    def protocol(self, name: str, func: object = None) -> object:
        if func is None:
            return self._attrs.get(("protocol", name))
        self._attrs[("protocol", name)] = func
        return None

    def geometry(self, spec: str | None = None) -> str | None:
        return "" if spec is None else None

    def state(self, new: str | None = None) -> str:
        return "normal"

    def deiconify(self) -> None:
        self._alive = True

    def withdraw(self) -> None:
        self._alive = False

    def destroy(self) -> None:
        self._alive = False
        if isinstance(self._master, _Base):
            try:
                self._master._children.remove(self)
            except ValueError:
                pass

    def quit(self) -> None:
        self._alive = False

    def mainloop(self, n: int = 0) -> None:
        return None

    def update(self) -> None:
        return None

    def update_idletasks(self) -> None:
        return None

    def grab_release(self) -> None:
        return None

    def add_command(self, **kw: object) -> None:
        return None

    def add_separator(self, **kw: object) -> None:
        return None

    def add_checkbutton(self, **kw: object) -> None:
        return None

    def create_rectangle(self, *args: object, **kw: object) -> int:
        return 1

    def create_text(self, *args: object, **kw: object) -> int:
        return 2

class Tk(_Base):
    """根窗口：记住 geometry 设进来的尺寸，winfo_* 如实回报。"""

    _GEOM_RE = re.compile(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)")

    def __init__(self, *args: object, **kw: object) -> None:
        super().__init__(None, **kw)
        self._geom = [260, 200, 40, 60]

    def geometry(self, spec: str | None = None) -> str | None:
        if spec is None:
            return "%dx%d+%d+%d" % tuple(self._geom)
        m = self._GEOM_RE.match(str(spec))
        if m:
            self._geom = [int(g) for g in m.groups()]
        return None

    def winfo_width(self) -> int:
        return self._geom[0]

    def winfo_height(self) -> int:
        return self._geom[1]

    def winfo_x(self) -> int:
        return self._geom[2]

    def winfo_y(self) -> int:
        return self._geom[3]

class Frame(_Base):
    pass

class Label(_Base):
    pass

class Canvas(_Base):
    pass

class Menu(_Base):
    pass

class Widget(_Base):
    pass

class Event:
    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)

class _Var:
    def __init__(self, master: object = None, value: object = None, **kw: object) -> None:
        self._value = value

    def get(self) -> object:
        return self._value

    def set(self, value: object) -> None:
        self._value = value

class BooleanVar(_Var):
    def __init__(self, master: object = None, value: bool = False, **kw: object) -> None:
        super().__init__(master, bool(value), **kw)

    def get(self) -> bool:
        return bool(self._value)

    def set(self, value: object) -> None:
        self._value = bool(value)

def install() -> bool:
    """没有真 tkinter 时装上替身。返回 True 表示这次用的是替身。"""
    mod = sys.modules.get("tkinter")
    if mod is not None and getattr(mod, _STUB_MARK, False):
        return True
    try:
        import tkinter  # noqa: F401
    except ModuleNotFoundError:      # 可能是缺 tkinter，也可能是缺底层 _tkinter
        pass
    else:
        return False

    fake = types.ModuleType("tkinter")
    for name, obj in {
        "Tk": Tk, "Frame": Frame, "Label": Label, "Canvas": Canvas, "Menu": Menu,
        "Widget": Widget, "Event": Event, "BooleanVar": BooleanVar,
        "StringVar": _Var, "TclError": TclError,
    }.items():
        setattr(fake, name, obj)
    setattr(fake, _STUB_MARK, True)

    box = types.ModuleType("tkinter.messagebox")
    box.showerror = lambda *a, **kw: None
    box.showinfo = lambda *a, **kw: None
    fake.messagebox = box

    sys.modules["tkinter"] = fake
    sys.modules["tkinter.messagebox"] = box
    return True

def prepare() -> bool:
    """装上替身（需要时）并把界面会写的状态文件改道到临时目录。返回是否用了替身。

    不改道的话，测试里的 quit() 会把窗口位置/尺寸写回 data/window.json ——
    跑一次测试就把用户的窗口摆位冲掉了。install_dir.txt 同理：构造 Overlay 会
    走一遍日志探测，找到运行中的客户端就把安装目录缓存下来。
    """
    stub = install()
    from hsai import logwatch, ui

    ui.STATE_PATH = _tmp_dir() / "window.json"
    ui.LAST_REVIEW_JSON = _tmp_dir() / "no_review.json"
    ui.ADVICE_FEEDBACK_JSONL = _tmp_dir() / "advice_feedback.jsonl"
    logwatch.INSTALL_CACHE = _tmp_dir() / "install_dir.txt"
    return stub


def widget_texts(widget) -> list[str]:
    """递归收齐控件树里的 Label 文本，替身和真 tkinter 都要能用。

    没装 tkinter 时 install() 换上的替身把文本记在 `_opts` 里；真 tkinter 的控件
    没有 `_opts`，只能用 `cget("text")`。只认一种，同一个用例会在 CI 和开发机上
    跑出不同结果 —— 这条兜底顺序别省。
    """
    out: list[str] = []
    for child in widget.winfo_children():
        opts = getattr(child, "_opts", None)
        text = opts.get("text") if isinstance(opts, dict) else None
        if not isinstance(text, str):
            try:
                text = child.cget("text")
            except Exception:
                text = None
        if isinstance(text, str):
            out.append(text)
        out.extend(widget_texts(child))
    return out
