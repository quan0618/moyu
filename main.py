# -*- coding: utf-8 -*-
"""
moyu —— 极简桌面悬浮看板 v1.0

功能概述：
    1. 无边框、85% 半透明、始终置顶的悬浮小窗，实时显示行情数值（大字号）。
    2. 后台线程每 1 秒 POST 一次接口，取回价格并更新界面。
    3. 按住左键可拖动窗口；鼠标移入时右上角出现关闭按钮 ✕，移出即隐藏。
    4. 右键菜单可切换背景色 / 字体颜色，以及若干预设窗口尺寸。
    5. 点击 ✕ 或双击窗口即刻退出进程（os._exit(0)，避免后台线程报错）。

技术栈：Python 3.10+，仅依赖标准库 tkinter + 第三方 requests。
"""

import json
import os
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import colorchooser
from urllib.parse import urlparse

import requests

# ---------------------------------------------------------------------------
# 常量配置
# ---------------------------------------------------------------------------

# 行情接口地址
API_URL = "https://mbmodule-openapi.paas.cmbchina.com/product/v1/func/market-center"

# 请求体：固定查询 H 类型产品
API_PAYLOAD = [{"prdType": "H"}]

# 请求头（按接口常规约定，尽量贴近浏览器行为）
API_HEADERS = {
    "Content-Type": "application/json;charset=UTF-8",
    "Accept": "application/json, text/plain, */*",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://" + urlparse(API_URL).netloc + "/",
}

# 刷新间隔（秒）
REFRESH_INTERVAL = 1.0

# 请求超时（秒），必须小于刷新间隔附近的值，避免线程堆积
REQUEST_TIMEOUT = 3.0

# 默认外观
DEFAULT_BG_COLOR = "#2c3e50"   # 深灰蓝背景
DEFAULT_FG_COLOR = "#e74c3c"   # 红色字体
DEFAULT_ALPHA = 0.85           # 85% 不透明度

# 预设窗口尺寸（宽, 高）
PRESET_SIZES = [
    ("小 (180 x 70)", 180, 70),
    ("中 (260 x 100)", 260, 100),
    ("大 (360 x 140)", 360, 140),
    ("超大 (480 x 180)", 480, 180),
]
DEFAULT_SIZE_INDEX = 1  # 默认使用“中”

# 字体族：Windows 下优先使用等宽数字字体，保证数字跳动时不抖动
FONT_FAMILY_CANDIDATES = ["Consolas", "Cascadia Mono", "Segoe UI", "Microsoft YaHei"]


class MoyuBoard:
    """悬浮看板主类：负责界面构建、数据刷新与交互。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root

        # 外观状态
        self.bg_color = DEFAULT_BG_COLOR
        self.fg_color = DEFAULT_FG_COLOR
        self.size_index = DEFAULT_SIZE_INDEX
        self.width, self.height = (
            PRESET_SIZES[self.size_index][1],
            PRESET_SIZES[self.size_index][2],
        )

        # 运行状态
        self._running = True          # 刷新线程存活标志
        self._stop_event = threading.Event()  # 用于打断等待、快速退出
        self._drag_offset = (0, 0)    # 拖动时的鼠标相对窗口偏移
        self._close_button = None     # 关闭按钮引用（鼠标移入时创建）

        self._build_window()
        self._build_widgets()
        self._bind_events()
        self._build_context_menu()

        # 启动后台刷新线程（daemon 保证主线程退出时不会被阻塞）
        self._worker = threading.Thread(
            target=self._refresh_loop, name="price-refresh", daemon=True
        )
        self._worker.start()

    # ------------------------------------------------------------------
    # 界面构建
    # ------------------------------------------------------------------

    def _build_window(self) -> None:
        """窗口级属性：无边框、置顶、半透明、默认位置。"""
        self.root.overrideredirect(True)          # 去掉系统边框与标题栏
        self.root.attributes("-topmost", True)    # 始终置顶
        self.root.attributes("-alpha", DEFAULT_ALPHA)  # 半透明

        # 初次出现在屏幕右上角，避免挡住任务栏与常用区域
        screen_w = self.root.winfo_screenwidth()
        self.root.geometry(f"{self.width}x{self.height}+{screen_w - self.width - 40}+80")

        self.root.configure(bg=self.bg_color)

    def _build_widgets(self) -> None:
        """创建价格文本标签。"""
        # 选择系统已安装的字体，全部缺失时退回 Tk 默认字体
        available = set(tkfont.families(self.root))
        self._font_family = next(
            (f for f in FONT_FAMILY_CANDIDATES if f in available), "TkDefaultFont"
        )
        family = self._font_family

        self.price_label = tk.Label(
            self.root,
            text="--",
            font=(family, self._font_size(), "bold"),
            bg=self.bg_color,
            fg=self.fg_color,
            bd=0,
            highlightthickness=0,
            justify="center",
            anchor="center",
        )
        self.price_label.pack(fill="both", expand=True, padx=6, pady=4)

    def _font_size(self) -> int:
        """根据窗口高度动态计算字号，让数字始终撑满窗口。"""
        return max(14, int(self.height * 0.42))

    # ------------------------------------------------------------------
    # 事件绑定
    # ------------------------------------------------------------------

    def _bind_events(self) -> None:
        """绑定拖动、悬停、双击、右键等交互。"""
        for widget in (self.root, self.price_label):
            widget.bind("<ButtonPress-1>", self._on_drag_start)
            widget.bind("<B1-Motion>", self._on_drag_move)
            widget.bind("<Double-Button-1>", self._on_double_click_exit)
            widget.bind("<Button-3>", self._on_right_click)
            widget.bind("<Enter>", self._on_mouse_enter)
            widget.bind("<Leave>", self._on_mouse_leave)

    def _build_context_menu(self) -> None:
        """右键菜单：颜色与预设尺寸。"""
        self.menu = tk.Menu(self.root, tearoff=0)

        self.menu.add_command(label="更改背景颜色…", command=self._pick_bg_color)
        self.menu.add_command(label="更改字体颜色…", command=self._pick_fg_color)
        self.menu.add_separator()

        # 预设尺寸子菜单
        size_menu = tk.Menu(self.menu, tearoff=0)
        for idx, (label, _w, _h) in enumerate(PRESET_SIZES):
            size_menu.add_command(
                label=label, command=lambda i=idx: self._apply_preset_size(i)
            )
        self.menu.add_cascade(label="窗口大小", menu=size_menu)

        self.menu.add_separator()
        self.menu.add_command(label="退出", command=self._exit_app)

    # ------------------------------------------------------------------
    # 交互实现
    # ------------------------------------------------------------------

    def _on_drag_start(self, event: "tk.Event") -> None:
        """记录按下左键时鼠标相对窗口左上角的偏移。"""
        self._drag_offset = (event.x_root - self.root.winfo_x(),
                             event.y_root - self.root.winfo_y())

    def _on_drag_move(self, event: "tk.Event") -> None:
        """按住左键拖动窗口。"""
        x = event.x_root - self._drag_offset[0]
        y = event.y_root - self._drag_offset[1]
        self.root.geometry(f"+{x}+{y}")

    def _on_double_click_exit(self, _event: "tk.Event" = None) -> None:
        """双击窗口立即退出。"""
        self._exit_app()

    def _on_right_click(self, event: "tk.Event") -> None:
        """右键在鼠标位置弹出菜单。"""
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _on_mouse_enter(self, _event: "tk.Event" = None) -> None:
        """鼠标移入：右上角显示关闭按钮。"""
        if self._close_button is not None:
            return

        # 关闭按钮用一个 Label 模拟按钮，背景透明感通过同色实现
        self._close_button = tk.Label(
            self.root,
            text="✕",
            font=(self._font_family, 11),
            bg=self.bg_color,
            fg=self.fg_color,
            cursor="hand2",
            bd=0,
        )
        self._close_button.place(relx=1.0, x=-2, y=2, anchor="ne")
        self._close_button.bind("<Button-1>", lambda e: self._exit_app())
        # 鼠标移入按钮本身时不应触发“移出窗口”隐藏逻辑
        self._close_button.bind("<Enter>", lambda e: None)
        self._close_button.lift()

    def _on_mouse_leave(self, _event: "tk.Event" = None) -> None:
        """鼠标移出窗口：隐藏关闭按钮。"""
        # 用 after 延迟判断，避免鼠标进入子控件（按钮）时误隐藏
        self.root.after(30, self._hide_close_button_if_outside)

    def _hide_close_button_if_outside(self) -> None:
        """若鼠标已不在窗口范围内，则销毁关闭按钮。"""
        if self._close_button is None:
            return
        try:
            px, py = self.root.winfo_pointerxy()
            widget = self.root.winfo_containing(px, py)
        except (tk.TclError, KeyError):
            widget = None

        # 鼠标仍在窗口或按钮上则保持显示
        if widget is self._close_button:
            return
        if widget in (self.root, self.price_label):
            return

        self._close_button.destroy()
        self._close_button = None

    # ------------------------------------------------------------------
    # 外观调整
    # ------------------------------------------------------------------

    def _pick_bg_color(self) -> None:
        """选择背景颜色。"""
        color = colorchooser.askcolor(
            color=self.bg_color, title="选择背景颜色", parent=self.root
        )[1]
        if color:
            self._set_bg(color)

    def _pick_fg_color(self) -> None:
        """选择字体颜色。"""
        color = colorchooser.askcolor(
            color=self.fg_color, title="选择字体颜色", parent=self.root
        )[1]
        if color:
            self._set_fg(color)

    def _set_bg(self, color: str) -> None:
        """应用背景色到所有控件。"""
        self.bg_color = color
        self.root.configure(bg=color)
        self.price_label.configure(bg=color)
        # 右键菜单保持系统样式，不跟随背景色
        self._style_close_button()

    def _set_fg(self, color: str) -> None:
        """应用字体颜色。"""
        self.fg_color = color
        self.price_label.configure(fg=color)
        self._style_close_button()

    def _style_close_button(self) -> None:
        """关闭按钮与窗口保持同一配色。"""
        if self._close_button is not None:
            self._close_button.configure(bg=self.bg_color, fg=self.fg_color)

    def _apply_preset_size(self, index: int) -> None:
        """按预设索引调整窗口尺寸，并保持窗口中心不变。"""
        _, w, h = PRESET_SIZES[index]
        # 计算当前中心点，缩放后保持相对位置
        cx = self.root.winfo_x() + self.root.winfo_width() // 2
        cy = self.root.winfo_y() + self.root.winfo_height() // 2

        self.size_index = index
        self.width, self.height = w, h

        new_x = cx - w // 2
        new_y = cy - h // 2
        self.root.geometry(f"{w}x{h}+{new_x}+{new_y}")

        # 字号随高度重新计算
        self.price_label.configure(font=(self._font_family, self._font_size(), "bold"))

    # ------------------------------------------------------------------
    # 数据刷新
    # ------------------------------------------------------------------

    def _fetch_price(self) -> str:
        """发起一次请求并解析价格，失败时返回错误提示文本。"""
        try:
            resp = requests.post(
                API_URL,
                data=json.dumps(API_PAYLOAD, separators=(",", ":")),
                headers=API_HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            payload = resp.json()

            # 逐层取值：data.FQAMBPRCZ1.zBuyPrc
            data = payload.get("data") or {}
            node = data.get("FQAMBPRCZ1") or {}
            price = node.get("zBuyPrc")

            if price is None or price == "":
                return "无数据"

            # 价格可能为数字或字符串，统一格式化为两位小数展示
            try:
                return f"{float(price):.2f}"
            except (TypeError, ValueError):
                return str(price)
        except requests.exceptions.Timeout:
            return "超时"
        except requests.exceptions.RequestException:
            return "断网"
        except (ValueError, AttributeError):
            return "解析错误"

    def _refresh_loop(self) -> None:
        """后台线程主循环：每秒请求一次，通过 after 回到主线程更新界面。"""
        while self._running:
            text = self._fetch_price()
            if not self._running:
                break
            # Tkinter 非线程安全：用 after 把更新投递回主线程
            try:
                self.root.after(0, self._update_label, text)
            except tk.TclError:
                # 窗口已销毁，结束线程
                break

            # 使用 Event 等待，便于退出时立即唤醒
            self._stop_event.wait(REFRESH_INTERVAL)

    def _update_label(self, text: str) -> None:
        """主线程中更新价格文本。"""
        try:
            self.price_label.configure(text=text)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # 退出
    # ------------------------------------------------------------------

    def _exit_app(self) -> None:
        """立刻结束进程，绕过 Tkinter 销毁流程，避免后台线程报错。"""
        self._running = False
        self._stop_event.set()
        os._exit(0)


def main() -> None:
    root = tk.Tk()
    root.title("moyu")
    MoyuBoard(root)
    root.mainloop()


if __name__ == "__main__":
    main()
