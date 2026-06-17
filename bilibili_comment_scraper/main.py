"""Bilibili comment scraper GUI."""

from __future__ import annotations

import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from bilibili_client import BilibiliClient
from exporter import export_comments_to_excel


class CommentScraperApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("B站评论区用户评论爬取工具")
        self.root.geometry("760x620")
        self.root.minsize(680, 520)

        self.client = BilibiliClient()
        self.comments = []
        self.summary: dict = {}
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()

        self._build_ui()

    def _build_ui(self) -> None:
        padding = {"padx": 12, "pady": 6}

        frame = ttk.Frame(self.root, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        title = ttk.Label(
            frame,
            text="爬取指定视频下某用户的全部评论，并导出为 Excel",
            font=("PingFang SC", 14, "bold"),
        )
        title.pack(anchor=tk.W, pady=(0, 12))

        form = ttk.LabelFrame(frame, text="搜索条件", padding=12)
        form.pack(fill=tk.X)

        ttk.Label(form, text="BV号:").grid(row=0, column=0, sticky=tk.W, **padding)
        self.bvid_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.bvid_var, width=40).grid(
            row=0, column=1, sticky=tk.EW, **padding
        )
        ttk.Label(form, text="例如: BV1xx411c7mD").grid(row=0, column=2, sticky=tk.W, **padding)

        ttk.Label(form, text="用户名:").grid(row=1, column=0, sticky=tk.W, **padding)
        self.username_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.username_var, width=40).grid(
            row=1, column=1, sticky=tk.EW, **padding
        )
        ttk.Label(form, text="需与B站昵称完全一致").grid(row=1, column=2, sticky=tk.W, **padding)

        self.exact_match_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            form,
            text="精确匹配用户名（取消勾选则为模糊匹配）",
            variable=self.exact_match_var,
        ).grid(row=2, column=1, columnspan=2, sticky=tk.W, **padding)

        ttk.Label(form, text="Cookie (可选):").grid(row=3, column=0, sticky=tk.NW, **padding)
        self.cookie_var = tk.StringVar()
        cookie_entry = ttk.Entry(form, textvariable=self.cookie_var, width=40)
        cookie_entry.grid(row=3, column=1, sticky=tk.EW, **padding)
        ttk.Label(
            form,
            text="登录 B 站后从浏览器复制 Cookie，\n可减少限流并提高成功率",
        ).grid(row=3, column=2, sticky=tk.W, **padding)

        form.columnconfigure(1, weight=1)

        button_row = ttk.Frame(frame)
        button_row.pack(fill=tk.X, pady=8)

        self.start_btn = ttk.Button(button_row, text="开始爬取", command=self.start_scrape)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.cancel_btn = ttk.Button(
            button_row, text="取消", command=self.cancel_scrape, state=tk.DISABLED
        )
        self.cancel_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.export_btn = ttk.Button(
            button_row, text="导出 Excel", command=self.export_excel, state=tk.DISABLED
        )
        self.export_btn.pack(side=tk.LEFT)

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(frame, textvariable=self.status_var).pack(anchor=tk.W, pady=(4, 0))

        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.pack(fill=tk.X, pady=8)

        log_frame = ttk.LabelFrame(frame, text="运行日志", padding=8)
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = tk.Text(log_frame, height=18, wrap=tk.WORD, state=tk.DISABLED)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.configure(yscrollcommand=scrollbar.set)

        tip = ttk.Label(
            frame,
            text="提示：评论较多的视频可能需要较长时间，请耐心等待并保持网络畅通。",
            foreground="#666666",
        )
        tip.pack(anchor=tk.W, pady=(8, 0))

    def append_log(self, message: str) -> None:
        def _append() -> None:
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.insert(tk.END, message + "\n")
            self.log_text.see(tk.END)
            self.log_text.configure(state=tk.DISABLED)

        self.root.after(0, _append)

    def set_running(self, running: bool) -> None:
        def _update() -> None:
            if running:
                self.start_btn.configure(state=tk.DISABLED)
                self.cancel_btn.configure(state=tk.NORMAL)
                self.export_btn.configure(state=tk.DISABLED)
                self.progress.start(12)
                self.status_var.set("正在爬取...")
            else:
                self.start_btn.configure(state=tk.NORMAL)
                self.cancel_btn.configure(state=tk.DISABLED)
                self.progress.stop()
                if self.comments:
                    self.export_btn.configure(state=tk.NORMAL)
                    self.status_var.set(f"完成，共找到 {len(self.comments)} 条评论")
                else:
                    self.status_var.set("就绪")

        self.root.after(0, _update)

    def start_scrape(self) -> None:
        bvid = self.bvid_var.get().strip()
        username = self.username_var.get().strip()

        if not bvid:
            messagebox.showwarning("输入错误", "请输入 BV 号")
            return
        if not username:
            messagebox.showwarning("输入错误", "请输入用户名")
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "任务正在进行中")
            return

        self.comments = []
        self.summary = {}
        self.stop_event.clear()
        self.client.set_cookie(self.cookie_var.get())
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

        self.set_running(True)
        self.worker = threading.Thread(
            target=self._run_scrape,
            args=(bvid, username, self.exact_match_var.get()),
            daemon=True,
        )
        self.worker.start()

    def cancel_scrape(self) -> None:
        self.stop_event.set()
        self.append_log("正在取消，请稍候...")

    def _run_scrape(self, bvid: str, username: str, exact_match: bool) -> None:
        try:
            comments, summary = self.client.collect_user_comments(
                bvid,
                username,
                exact_match=exact_match,
                progress_callback=self.append_log,
                should_stop=self.stop_event.is_set,
            )
            self.comments = comments
            self.summary = summary
            if not comments:
                self.root.after(
                    0,
                    lambda: messagebox.showinfo(
                        "结果",
                        "未找到该用户的评论，请检查 BV 号和用户名是否正确。",
                    ),
                )
        except InterruptedError:
            self.append_log("任务已取消")
        except Exception as exc:
            self.append_log(f"错误: {exc}")
            self.root.after(0, lambda: messagebox.showerror("爬取失败", str(exc)))
        finally:
            self.set_running(False)

    def export_excel(self) -> None:
        if not self.comments:
            messagebox.showwarning("提示", "没有可导出的评论，请先完成爬取")
            return

        default_name = (
            f"B站评论_{self.summary.get('username', '用户')}_"
            f"{self.summary.get('bvid', 'video')}_"
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        )
        file_path = filedialog.asksaveasfilename(
            title="保存 Excel 文件",
            defaultextension=".xlsx",
            initialfile=default_name,
            filetypes=[("Excel 文件", "*.xlsx")],
        )
        if not file_path:
            return

        try:
            saved_path = export_comments_to_excel(self.comments, file_path, self.summary)
            self.append_log(f"已导出: {saved_path}")
            messagebox.showinfo("导出成功", f"Excel 已保存到:\n{saved_path}")
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))


def main() -> None:
    root = tk.Tk()
    try:
        style = ttk.Style()
        if "aqua" in style.theme_names():
            style.theme_use("aqua")
    except tk.TclError:
        pass
    CommentScraperApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
