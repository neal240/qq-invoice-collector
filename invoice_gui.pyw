"""Windows desktop interface for the reusable QQ invoice collector."""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
import os
from pathlib import Path
import queue
import re
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from tkinter import font as tkfont

from credential_store import (delete_credential, list_accounts, load_credential,
                              save_credential)
from invoice_mail import (HONG_KONG, PdfReader, collect, import_manual_pdfs,
                          load_settings, parse_day, safe_name, save_settings,
                          search_window)


BG = '#f3f6f5'
INK = '#17313a'
MUTED = '#5c7077'
ACCENT = '#176b67'
WHITE = '#ffffff'


class InvoiceApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title('发票归集 · QQ 邮箱')
        self.root.geometry('790x880')
        self.root.minsize(660, 850)
        self.root.configure(bg=BG)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        try:
            tkfont.nametofont('TkDefaultFont').configure(family='Microsoft YaHei UI', size=10)
        except tk.TclError:
            pass

        style = ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('TFrame', background=WHITE)
        style.configure('TLabel', background=WHITE, foreground=INK)
        style.configure('TCheckbutton', background=WHITE, foreground=INK)
        style.configure('TEntry', padding=7)
        style.configure('TButton', padding=(12, 8))
        style.configure('Primary.TButton', background=ACCENT, foreground=WHITE,
                        font=('Microsoft YaHei UI', 10, 'bold'), padding=(19, 10))
        style.map('Primary.TButton', background=[('active', '#105753'), ('disabled', '#93aba8')],
                  foreground=[('disabled', WHITE)])

        settings = load_settings()
        today = datetime.now(HONG_KONG).date()
        last_month = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        self.email = tk.StringVar(value=settings.get('email', ''))
        self.auth = tk.StringVar()
        self.auth_hint = tk.StringVar()
        try:
            self.saved_accounts = list_accounts()
            self.credential_store_error = ''
        except OSError as exc:
            self.saved_accounts = []
            self.credential_store_error = str(exc)
        self.start_date = tk.StringVar(value=settings.get('start', last_month.isoformat()))
        self.end_date = tk.StringVar(value=settings.get('end', today.isoformat()))
        self.output_root = tk.StringVar(value=settings.get(
            'output_root', str(Path(__file__).parent / '归档结果')))
        self.delete_mail = tk.BooleanVar(value=settings.get('delete_mail', False))
        self.show_auth = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value='填写信息后开始归集')
        self.destination = tk.StringVar()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop_event = threading.Event()
        self.running = False
        self.close_after_stop = False
        self.last_output: Path | None = None
        self._build()
        self.email.trace_add('write', self.on_email_changed)
        for value in (self.email, self.start_date, self.end_date, self.output_root):
            value.trace_add('write', lambda *_: self.update_destination())
        self.update_auth_hint()
        self.update_destination()
        self.root.after(100, self.poll_events)

    def _build(self) -> None:
        shell = tk.Frame(self.root, bg=BG, padx=28, pady=24)
        shell.pack(fill='both', expand=True)
        tk.Label(shell, text='发票归集', bg=BG, fg=INK,
                 font=('Microsoft YaHei UI', 21, 'bold')).pack(anchor='w')
        tk.Label(shell, text='选择收件日期，自动保存金额大于 5 元的发票；开票二维码单独留存。',
                 bg=BG, fg=MUTED, font=('Microsoft YaHei UI', 10)).pack(anchor='w', pady=(4, 18))

        card = tk.Frame(shell, bg=WHITE, padx=20, pady=18,
                        highlightbackground='#dce6e3', highlightthickness=1)
        card.pack(fill='x')
        card.grid_columnconfigure(1, weight=1)

        self._label(card, 'QQ 邮箱', 0)
        self.email_entry = ttk.Combobox(card, textvariable=self.email,
                                        values=self.saved_accounts)
        self.email_entry.grid(row=0, column=1, columnspan=2, sticky='ew', pady=(0, 12))
        self.email_entry.focus_set()

        self._label(card, 'IMAP 授权码', 1)
        auth_frame = ttk.Frame(card)
        auth_frame.grid(row=1, column=1, columnspan=2, sticky='ew', pady=(0, 4))
        auth_frame.grid_columnconfigure(0, weight=1)
        self.auth_entry = ttk.Entry(auth_frame, textvariable=self.auth, show='•')
        self.auth_entry.grid(row=0, column=0, sticky='ew')
        ttk.Checkbutton(auth_frame, text='显示', variable=self.show_auth,
                        command=self.toggle_auth).grid(row=0, column=1, padx=(10, 0))
        ttk.Button(auth_frame, text='忘记此邮箱', command=self.forget_auth).grid(
            row=0, column=2, padx=(10, 0))
        tk.Label(card, textvariable=self.auth_hint, bg=WHITE, fg=MUTED,
                 font=('Microsoft YaHei UI', 9), anchor='w', justify='left',
                 wraplength=610).grid(
                     row=2, column=1, columnspan=2, sticky='w', pady=(0, 14))

        self._label(card, '开始日期', 3)
        self._date_field(card, self.start_date, 3)
        self._label(card, '结束日期', 4)
        self._date_field(card, self.end_date, 4)
        tk.Label(card, text='起止日期均包含当天，按邮箱服务器的收件时间筛选。',
                 bg=WHITE, fg=MUTED, font=('Microsoft YaHei UI', 9)).grid(
                     row=5, column=1, columnspan=2, sticky='w', pady=(2, 14))

        self._label(card, '保存位置', 6)
        folder_frame = ttk.Frame(card)
        folder_frame.grid(row=6, column=1, columnspan=2, sticky='ew', pady=(0, 14))
        folder_frame.grid_columnconfigure(0, weight=1)
        ttk.Entry(folder_frame, textvariable=self.output_root).grid(row=0, column=0, sticky='ew')
        ttk.Button(folder_frame, text='浏览…', command=self.browse).grid(row=0, column=1, padx=(8, 0))

        ttk.Checkbutton(card, text='自动处理完成后移入“已删除”（低于5元也删除）',
                        variable=self.delete_mail).grid(row=7, column=1, columnspan=2,
                                                        sticky='w', pady=(0, 2))
        tk.Label(card, text='5.00 元、未覆盖的二维码及金额不明的邮件会留在原处。',
                 bg=WHITE, fg=MUTED, font=('Microsoft YaHei UI', 9)).grid(
                     row=8, column=1, columnspan=2, sticky='w')

        destination_frame = tk.Frame(shell, bg=BG)
        destination_frame.pack(fill='x', pady=(16, 10))
        tk.Label(destination_frame, text='本次结果', bg=BG, fg=MUTED,
                 font=('Microsoft YaHei UI', 9, 'bold')).pack(anchor='w')
        tk.Label(destination_frame, textvariable=self.destination, bg=BG, fg=INK,
                 font=('Microsoft YaHei UI', 9), anchor='w', wraplength=720,
                 justify='left').pack(anchor='w', pady=(4, 0))

        actions = tk.Frame(shell, bg=BG)
        actions.pack(fill='x', pady=(3, 13))
        self.start_button = ttk.Button(actions, text='开始归集', style='Primary.TButton',
                                       command=self.start)
        self.start_button.pack(side='left')
        self.stop_button = ttk.Button(actions, text='停止', command=self.stop, state='disabled')
        self.stop_button.pack(side='left', padx=(10, 0))
        self.import_button = ttk.Button(actions, text='导入手动下载的 PDF/图片',
                                        command=self.import_pdfs)
        self.import_button.pack(side='left', padx=(10, 0))
        self.open_button = ttk.Button(actions, text='打开结果文件夹', command=self.open_result,
                                      state='disabled')
        self.open_button.pack(side='right')

        status_row = tk.Frame(shell, bg=BG)
        status_row.pack(fill='x')
        tk.Label(status_row, textvariable=self.status, bg=BG, fg=INK,
                 font=('Microsoft YaHei UI', 9, 'bold')).pack(side='left')
        self.progressbar = ttk.Progressbar(status_row, mode='indeterminate', length=140)
        self.progressbar.pack(side='right')
        self.log = scrolledtext.ScrolledText(shell, height=9, wrap='word', state='disabled',
                                             relief='flat', bg=WHITE, fg=INK,
                                             font=('Consolas', 9), padx=11, pady=9)
        self.log.pack(fill='both', expand=True, pady=(8, 0))

    def _label(self, parent: tk.Widget, title: str, row: int) -> None:
        ttk.Label(parent, text=title).grid(row=row, column=0, sticky='w',
                                           padx=(0, 16), pady=(0, 12))

    def _date_field(self, parent: tk.Widget, value: tk.StringVar, row: int) -> None:
        group = ttk.Frame(parent)
        group.grid(row=row, column=1, columnspan=2, sticky='ew', pady=(0, 10))
        group.grid_columnconfigure(0, weight=1)
        ttk.Entry(group, textvariable=value).grid(row=0, column=0, sticky='ew')
        ttk.Button(group, text='选择日期', command=lambda: self.pick_date(value)).grid(
            row=0, column=1, padx=(8, 0))

    def toggle_auth(self) -> None:
        self.auth_entry.configure(show='' if self.show_auth.get() else '•')

    def on_email_changed(self, *_args) -> None:
        # A typed code must never be accidentally used for another account.
        self.auth.set('')
        self.update_auth_hint()

    def update_auth_hint(self) -> None:
        address = self.email.get().strip().lower()
        if address in self.saved_accounts:
            message = '此邮箱已保存授权码；留空直接使用。输入新码并登录成功后会替换。'
        elif self.credential_store_error:
            message = f'无法访问 Windows 凭据管理器：{self.credential_store_error}'
        else:
            message = '首次输入授权码并登录成功后，会按邮箱保存到 Windows 凭据管理器。'
        self.auth_hint.set(message)

    def forget_auth(self) -> None:
        if self.running:
            return
        address = self.email.get().strip().lower()
        if not re.fullmatch(r'[^\s@]+@qq\.com', address, re.I):
            messagebox.showerror('邮箱格式错误', '请先选择有效的 QQ 邮箱。')
            return
        try:
            removed = delete_credential(address)
            self.saved_accounts = list_accounts()
        except OSError as exc:
            messagebox.showerror('移除失败', str(exc))
            return
        self.email_entry.configure(values=self.saved_accounts)
        self.update_auth_hint()
        self.append_log(f'{address}：{"本机授权码已移除" if removed else "本机没有已保存的授权码"}')

    def browse(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.output_root.get() or str(Path.home()))
        if folder:
            self.output_root.set(folder)

    def pick_date(self, value: tk.StringVar) -> None:
        try:
            current = parse_day(value.get())
        except ValueError:
            current = datetime.now(HONG_KONG).date()
        popup = tk.Toplevel(self.root)
        popup.title('选择日期')
        popup.transient(self.root)
        popup.resizable(False, False)
        popup.configure(bg=WHITE)
        popup.grab_set()
        year, month = current.year, current.month
        header = tk.Frame(popup, bg=WHITE, padx=12, pady=10)
        header.pack(fill='x')
        grid = tk.Frame(popup, bg=WHITE, padx=12, pady=8)
        grid.pack()

        def render() -> None:
            for child in header.winfo_children() + grid.winfo_children():
                child.destroy()
            ttk.Button(header, text='‹', width=3, command=lambda: shift(-1)).pack(side='left')
            tk.Label(header, text=f'{year} 年 {month} 月', bg=WHITE, fg=INK,
                     font=('Microsoft YaHei UI', 11, 'bold'), width=15).pack(side='left')
            ttk.Button(header, text='›', width=3, command=lambda: shift(1)).pack(side='left')
            for column, weekday in enumerate(('一', '二', '三', '四', '五', '六', '日')):
                tk.Label(grid, text=weekday, bg=WHITE, fg=MUTED, width=4).grid(
                    row=0, column=column, pady=(0, 5))
            for row_index, week in enumerate(calendar.monthcalendar(year, month), start=1):
                for column, day in enumerate(week):
                    if day:
                        ttk.Button(grid, text=str(day), width=3,
                                   command=lambda d=day: choose(d)).grid(
                                       row=row_index, column=column, padx=2, pady=2)

        def shift(delta: int) -> None:
            nonlocal year, month
            index = year * 12 + month - 1 + delta
            year, month = divmod(index, 12)
            month += 1
            render()

        def choose(day: int) -> None:
            value.set(date(year, month, day).isoformat())
            popup.destroy()

        render()
        popup.update_idletasks()
        popup.geometry(f'+{self.root.winfo_rootx() + 120}+{self.root.winfo_rooty() + 150}')

    def output_path(self) -> Path:
        address = self.email.get().strip().lower()
        start = self.start_date.get().strip()
        end = self.end_date.get().strip()
        return Path(self.output_root.get().strip()).expanduser() / safe_name(address) / f'{start}_至_{end}'

    def update_destination(self) -> None:
        path = self.output_path()
        self.destination.set(str(path))
        if not self.running and path.is_dir():
            self.last_output = path
            self.open_button.configure(state='normal')
        elif not self.running:
            self.last_output = None
            self.open_button.configure(state='disabled')

    def append_log(self, message: str) -> None:
        self.log.configure(state='normal')
        self.log.insert('end', message + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def start(self) -> None:
        if self.running:
            return
        if PdfReader is None:
            messagebox.showerror('缺少依赖', '缺少 pypdf。请先运行 python -m pip install -r requirements.txt。')
            return
        address = self.email.get().strip()
        entered_code = self.auth.get().strip()
        if not re.fullmatch(r'[^\s@]+@qq\.com', address, re.I):
            messagebox.showerror('邮箱格式错误', '请输入有效的 @qq.com 邮箱地址。')
            return
        try:
            password = entered_code or load_credential(address)
        except OSError as exc:
            messagebox.showerror('读取授权码失败', str(exc))
            return
        if not password:
            messagebox.showerror('缺少授权码', '请输入此邮箱的 IMAP 授权码；登录成功后会保存在本机。')
            self.auth_entry.focus_set()
            return
        try:
            start_day = parse_day(self.start_date.get().strip())
            end_day = parse_day(self.end_date.get().strip())
            search_window(start_day, end_day)
        except (ValueError, OverflowError) as exc:
            messagebox.showerror('日期错误', str(exc))
            return
        if not self.output_root.get().strip():
            messagebox.showerror('缺少保存位置', '请选择保存目录。')
            return
        output_root = Path(self.output_root.get().strip()).expanduser()
        output = self.output_path()
        try:
            save_settings(address, start_day, end_day, output_root, self.delete_mail.get())
        except OSError as exc:
            messagebox.showerror('无法保存设置', str(exc))
            return
        self.auth.set('')
        self.stop_event.clear()
        self.running = True
        self.start_button.configure(state='disabled')
        self.stop_button.configure(state='normal')
        self.import_button.configure(state='disabled')
        self.open_button.configure(state='disabled')
        self.progressbar.start(12)
        self.status.set('正在连接邮箱并处理邮件…')
        should_delete = self.delete_mail.get()
        self.append_log(f'开始：{address} | {start_day} 至 {end_day} | {"自动删除已完成邮件" if should_delete else "保留原邮件"}')

        def worker() -> None:
            def authenticated() -> None:
                if entered_code:
                    try:
                        save_credential(address, entered_code)
                    except OSError as exc:
                        self.events.put(('credential_warning', str(exc)))
                    else:
                        self.events.put(('credential_saved', address.lower()))

            try:
                rows = collect(address, password, output, start_day, end_day,
                               should_delete,
                               progress=lambda message: self.events.put(('log', message)),
                               stop_requested=self.stop_event.is_set,
                               on_authenticated=authenticated)
            except Exception as exc:
                self.events.put(('error', str(exc)))
            else:
                self.events.put(('done', (rows, output, self.stop_event.is_set())))

        threading.Thread(target=worker, daemon=True).start()

    def import_pdfs(self) -> None:
        if self.running:
            return
        selected = filedialog.askopenfilenames(
            title='选择从发票网页下载的 PDF 或图片',
            filetypes=[('发票文件', '*.pdf *.png *.jpg *.jpeg')])
        if not selected:
            return
        output = self.output_path()
        if not output.is_dir():
            messagebox.showerror('找不到归集结果', '请先用当前邮箱和日期运行一次归集，再导入网页下载的文件。')
            return
        self.running = True
        self.start_button.configure(state='disabled')
        self.import_button.configure(state='disabled')
        self.stop_button.configure(state='disabled')
        self.progressbar.start(12)
        self.status.set('正在核对并导入文件…')

        def worker() -> None:
            try:
                rows = import_manual_pdfs([Path(path) for path in selected], output)
            except Exception as exc:
                self.events.put(('error', str(exc)))
            else:
                self.events.put(('import_done', (rows, output)))

        threading.Thread(target=worker, daemon=True).start()

    def stop(self) -> None:
        self.stop_event.set()
        self.stop_button.configure(state='disabled')
        self.status.set('正在停止，当前邮件处理完成后退出…')

    def poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == 'log':
                    self.append_log(str(payload))
                elif kind == 'credential_saved':
                    address = str(payload)
                    self.saved_accounts = sorted(set(self.saved_accounts + [address]))
                    self.email_entry.configure(values=self.saved_accounts)
                    self.credential_store_error = ''
                    self.update_auth_hint()
                    self.append_log(f'{address}：登录成功，授权码已保存在 Windows 凭据管理器。')
                elif kind == 'credential_warning':
                    self.append_log('邮箱登录成功，但授权码未能保存：' + str(payload))
                elif kind == 'error':
                    self.finish()
                    self.status.set('处理失败，请查看错误')
                    self.append_log('错误：' + str(payload))
                    messagebox.showerror('归集失败', str(payload))
                elif kind == 'done':
                    rows, output, stopped = payload
                    self.finish()
                    self.last_output = output
                    self.open_button.configure(state='normal')
                    self.status.set('已停止' if stopped else '处理完成')
                    self.append_log(f'{"已停止" if stopped else "完成"}：本次 {len(rows)} 条记录。结果：{output}')
                elif kind == 'import_done':
                    rows, output = payload
                    self.finish()
                    self.last_output = output
                    self.open_button.configure(state='normal')
                    archived = sum(row['status'] == '已归档' for row in rows)
                    self.status.set(f'导入完成：{archived} 份发票已归档')
                    self.append_log(f'手动导入：{len(rows)} 份文件，{archived} 份金额大于 5 元。结果：{output}')
        except queue.Empty:
            pass
        if self.close_after_stop and not self.running:
            self.root.destroy()
        else:
            self.root.after(100, self.poll_events)

    def finish(self) -> None:
        self.running = False
        self.progressbar.stop()
        self.start_button.configure(state='normal')
        self.import_button.configure(state='normal')
        self.stop_button.configure(state='disabled')

    def open_result(self) -> None:
        if self.last_output and self.last_output.exists():
            os.startfile(self.last_output)

    def close(self) -> None:
        if self.running:
            self.close_after_stop = True
            self.stop()
        else:
            self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == '__main__':
    InvoiceApp().run()
