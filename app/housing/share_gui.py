"""Small Windows desktop UI. No collection, delivery or writable DB connection."""
from __future__ import annotations

import logging
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from app.config import get_settings
from app.housing.share_pack import LABELS, read_frozen_report
from app.housing.share_tool import detect_metadata, export_simple_report, make_report, read_report_file


class ShareWindow:
    def __init__(self, root):
        self.root = root
        self.busy = False
        self.result = None
        self.events = queue.Queue()
        self.controls = []
        self.kind = tk.StringVar()
        self.day = tk.StringVar()
        self.clock = tk.StringVar()
        self.target_pages = tk.StringVar(value="6")
        self.percent = tk.StringVar(value="0%")
        self.summary = tk.StringVar()
        self.output = tk.StringVar(value=str(get_settings().project_root / "分享输出"))
        self.status = tk.StringVar(value="加载本机报表、导入文件，或直接粘贴完整正文。确认报表日期后生成。")
        root.title("住房管家 · 导读和图片生成工具 · 表格版v4")
        root.geometry("1020x800")
        root.minsize(820, 650)
        root.option_add("*Font", ("Microsoft YaHei", 10))
        main = ttk.Frame(root, padding=16)
        main.pack(fill="both", expand=True)
        ttk.Label(main, text="早晚报 → 导读 + 完整图片", font=("Microsoft YaHei", 17, "bold")).pack(anchor="w")
        ttk.Label(main, text="只做本地排版，不会发群消息，也不会改变定时推送。", foreground="#607287").pack(anchor="w", pady=(4, 12))
        imports = ttk.Frame(main)
        imports.pack(fill="x")
        for label, command in [("加载最新早报", lambda: self.load_latest("digest")),
                               ("加载最新晚报", lambda: self.load_latest("evening")),
                               ("导入 TXT / Markdown", self.import_file), ("粘贴正文", self.paste)]:
            self.button(imports, label, command).pack(side="left", padx=(0, 8))
        metadata = ttk.Frame(main)
        metadata.pack(fill="x", pady=12)
        ttk.Label(metadata, text="报表类型").pack(side="left")
        self.kind_box = ttk.Combobox(metadata, textvariable=self.kind, values=list(LABELS.values()), state="readonly", width=7)
        self.kind_box.pack(side="left", padx=(6, 18))
        ttk.Label(metadata, text="报表日期").pack(side="left")
        self.day_entry = ttk.Entry(metadata, textvariable=self.day, width=13)
        self.day_entry.pack(side="left", padx=(6, 4))
        ttk.Label(metadata, text="YYYY-MM-DD").pack(side="left", padx=(0, 18))
        ttk.Label(metadata, text="时间（可留空）").pack(side="left")
        self.clock_entry = ttk.Entry(metadata, textvariable=self.clock, width=7)
        self.clock_entry.pack(side="left", padx=6)
        self.button(metadata, "重新识别", self.detect).pack(side="right")
        picture_options = ttk.Frame(main)
        picture_options.pack(fill="x", pady=(0, 10))
        ttk.Label(picture_options, text="目标图片数").pack(side="left")
        self.pages_box = ttk.Spinbox(picture_options, from_=2, to=12, textvariable=self.target_pages, width=5)
        self.pages_box.pack(side="left", padx=6)
        ttk.Label(picture_options, text="张（建议4—6张）。自动加长图片，保留字号及全文；内容过长时可能超出目标。", foreground="#607287").pack(side="left")
        ttk.Label(main, text="完整正文（可编辑；无法识别日期时请在上方补全）").pack(anchor="w")
        self.editor = ScrolledText(main, wrap="word", undo=True, font=("Microsoft YaHei", 11), height=18)
        self.editor.pack(fill="both", expand=True, pady=(5, 10))
        self.editor.bind("<<Paste>>", lambda event: root.after_idle(self.detect))
        location = ttk.Frame(main)
        location.pack(fill="x")
        ttk.Label(location, text="成品保存到").pack(side="left")
        self.output_entry = ttk.Entry(location, textvariable=self.output)
        self.output_entry.pack(side="left", fill="x", expand=True, padx=8)
        self.button(location, "选择目录", self.choose_output).pack(side="left")
        actions = ttk.Frame(main)
        actions.pack(fill="x", pady=12)
        self.generate_button = self.button(actions, "生成导读和图片", self.generate)
        self.generate_button.pack(side="left", padx=(0, 10))
        self.copy_button = self.button(actions, "复制导读", self.copy_intro)
        self.copy_button.pack(side="left", padx=(0, 10))
        self.folder_button = self.button(actions, "打开成品文件夹", self.open_folder)
        self.folder_button.pack(side="left")
        progress_row = ttk.Frame(main)
        progress_row.pack(fill="x", pady=(0, 8))
        self.progress = ttk.Progressbar(progress_row, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True)
        ttk.Label(progress_row, textvariable=self.percent, width=7, anchor="e").pack(side="right")
        self.summary_label = ttk.Label(main, textvariable=self.summary, font=("Microsoft YaHei", 11, "bold"), foreground="#24703d", wraplength=940)
        self.summary_label.pack(anchor="w")
        ttk.Label(main, textvariable=self.status, wraplength=940, foreground="#1d527d").pack(anchor="w", pady=(4, 0))
        ttk.Label(main, text="成品文件夹内只有：导读.txt、01.png、02.png……　旧版本会保留。", foreground="#607287").pack(anchor="w", pady=(6, 0))
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh_controls()
        self.poll_after = root.after(100, self.poll)

    def button(self, parent, label, command):
        button = ttk.Button(parent, text=label, command=command)
        self.controls.append(button)
        return button

    def refresh_controls(self):
        for widget in self.controls:
            widget.configure(state="disabled" if self.busy else "normal")
        for widget in [self.day_entry, self.clock_entry, self.output_entry, self.editor, self.pages_box]:
            widget.configure(state="disabled" if self.busy else "normal")
        self.kind_box.configure(state="disabled" if self.busy else "readonly")
        if not self.result or self.busy:
            self.copy_button.configure(state="disabled")
            self.folder_button.configure(state="disabled")

    def set_input(self, text, metadata=None):
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", text)
        self.editor.edit_reset()
        self.apply_metadata(metadata or detect_metadata(text))
        self.status.set("正文已载入。请核对报表类型、日期和内容，再点击生成。")

    def apply_metadata(self, values):
        self.kind.set(LABELS.get(values.get("kind"), ""))
        self.day.set(values.get("day", ""))
        self.clock.set(values.get("clock", ""))

    def detect(self):
        if not self.busy:
            self.apply_metadata(detect_metadata(self.editor.get("1.0", "end-1c")))

    def paste(self):
        try:
            self.set_input(self.root.clipboard_get())
        except tk.TclError:
            messagebox.showerror("没有可粘贴文字", "请先复制完整早报或晚报正文。", parent=self.root)

    def import_file(self):
        path = filedialog.askopenfilename(parent=self.root, title="导入报表", filetypes=[("报表文本", "*.txt *.md *.markdown")])
        if path:
            try:
                self.set_input(read_report_file(path))
            except Exception as exc:
                messagebox.showerror("导入失败", str(exc), parent=self.root)

    def choose_output(self):
        path = filedialog.askdirectory(parent=self.root, title="选择成品根目录", initialdir=self.output.get())
        if path:
            self.output.set(path)

    def start(self, task, operation):
        if self.busy:
            return
        self.busy = True
        self.refresh_controls()
        self.progress["value"] = 0
        self.percent.set("0%")
        self.summary.set("")
        self.summary_label.configure(foreground="#24703d")
        self.status.set("正在加载本机已受理的冻结报表……" if task == "load" else "正在生成，请稍候……")
        def run():
            try:
                self.events.put((task, operation()))
            except Exception as exc:
                logging.exception("Share tool %s failed", task)
                self.events.put(("error", str(exc)))
        threading.Thread(target=run, daemon=True).start()

    def load_latest(self, kind):
        self.start("load", lambda: read_frozen_report(kind))

    def generate(self):
        if self.busy:
            return
        try:
            kind = next((k for k, label in LABELS.items() if label == self.kind.get()), "")
            report = make_report(self.editor.get("1.0", "end-1c"), kind, self.day.get(), self.clock.get())
            if not self.output.get().strip():
                raise ValueError("请选择成品保存目录")
            output = Path(self.output.get()).resolve()
            try:
                target_pages = int(self.target_pages.get())
            except ValueError as exc:
                raise ValueError("目标图片数请填写2至12之间的整数") from exc
            if not 2 <= target_pages <= 12:
                raise ValueError("目标图片数请填写2至12之间的整数")
        except ValueError as exc:
            messagebox.showerror("请补全信息", str(exc), parent=self.root)
            return
        self.start("generated", lambda: export_simple_report(report, output, target_pages=target_pages,
            progress=lambda percent, message: self.events.put(("progress", (percent, message)))))

    def poll(self):
        try:
            while True:
                task, value = self.events.get_nowait()
                if task == "progress":
                    percent, message = value
                    self.progress["value"] = max(float(self.progress["value"]), min(100, percent))
                    self.percent.set(f"{int(self.progress['value'])}%")
                    self.status.set(message)
                    continue
                self.busy = False
                self.refresh_controls()
                if task == "load":
                    self.progress["value"] = 100
                    self.percent.set("100%")
                    self.set_input(value.markdown, {"kind": value.kind, "day": str(value.stamp.date()), "clock": value.stamp.strftime("%H:%M")})
                elif task == "generated":
                    self.result = value
                    self.progress["value"] = 100
                    self.percent.set("100%")
                    action = "已复用已有成品，未新增版本" if value["status"] == "existing" else "导读和图片已保存"
                    summary = f"✓ 完成：{action}｜共{value['pages']}张图片"
                    summary += f"｜已识别{value.get('tables',0)}张表格"
                    if value["pages"] > value["target_pages"]:
                        summary += f"（正文较长，超过{value['target_pages']}张目标，已完整保留）"
                    self.summary.set(summary)
                    self.status.set(f"{value['reason']}。\n成品目录：{value['directory']}")
                    self.refresh_controls()
                    messagebox.showinfo("生成完成", f"{summary}\n\n{value['reason']}。\n{value['directory']}\n\n可以点击“复制导读”或“打开成品文件夹”。", parent=self.root)
                else:
                    self.percent.set("失败")
                    self.summary.set("本次操作未完成，已有成品未被覆盖")
                    self.summary_label.configure(foreground="#b42318")
                    self.status.set("本次操作失败，已有成品已保留。可修改输入或目录后重试。")
                    messagebox.showerror("操作失败", value, parent=self.root)
        except queue.Empty:
            pass
        self.poll_after = self.root.after(100, self.poll)

    def copy_intro(self):
        if self.result:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.result["intro"].lstrip("\ufeff"))
            self.status.set("导读已复制，可粘贴到小红书群。")

    def open_folder(self):
        if self.result:
            os.startfile(self.result["directory"])

    def close(self):
        if self.busy:
            messagebox.showinfo("正在处理", "请等待本次加载或生成结束后关闭窗口。", parent=self.root)
            return
        self.root.after_cancel(self.poll_after)
        self.root.destroy()


def main():
    work = get_settings().project_root / "data/share-tool"
    work.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=work / "tool.log", encoding="utf-8", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    root = tk.Tk()
    root.report_callback_exception = lambda *args: (
        logging.error("GUI callback failed", exc_info=args),
        messagebox.showerror("窗口操作失败", "请重试；错误详情已保存在工具日志。", parent=root))
    ShareWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
