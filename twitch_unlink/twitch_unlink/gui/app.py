"""Настольное окно (tkinter) — отвязка Twitch Drops без браузера."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from twitch_unlink.accounts import AccountLine, load_accounts_file
from twitch_unlink.client import DropGameLink, TwitchUnlinkClient
from twitch_unlink.errors import format_user_error
from pathlib import Path

from twitch_unlink.proxy_rotator import ProxyRotator, parse_proxy_list

# Twitch-палитра
BG = "#0e0e10"
PANEL = "#18181b"
BORDER = "#2f2f35"
TEXT = "#efeff1"
MUTED = "#adadb8"
ACCENT = "#9147ff"
ACCENT_HOVER = "#772ce8"
GREEN = "#00f593"
DANGER = "#e91916"


class TwitchDropsApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Twitch Drops Unlink")
        self.root.geometry("960x620")
        self.root.minsize(780, 480)
        self.root.configure(bg=BG)

        self._accounts: list[AccountLine] = []
        self._accounts_path: str | None = None
        self._client: TwitchUnlinkClient | None = None
        self._active_account: AccountLine | None = None
        self._games: list[DropGameLink] = []
        self._proxy_rotator: ProxyRotator | None = None
        self._proxy_countdown = tk.StringVar(value="Прокси: не заданы")
        self._status = tk.StringVar(value="Выберите файл с аккаунтами")
        self._user_label = tk.StringVar(value="—")
        self._work_q: queue.Queue[tuple[Callable[..., Any], tuple, dict]] = queue.Queue()
        self._ui_q: queue.Queue[tuple[Callable[..., Any], tuple, dict]] = queue.Queue()

        self._setup_styles()
        self._build_ui()
        self.root.after(80, self._pump_queues)
        self.root.after(500, self._tick_proxy_label)

    def _setup_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG, foreground=TEXT, fieldbackground=PANEL, bordercolor=BORDER)
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("Panel.TLabel", background=PANEL, foreground=TEXT)
        style.configure("TButton", padding=8)
        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff")
        style.map("Accent.TButton", background=[("active", ACCENT_HOVER)])
        style.configure("Danger.TButton", foreground=DANGER)
        style.configure(
            "Treeview",
            background=PANEL,
            fieldbackground=PANEL,
            foreground=TEXT,
            rowheight=28,
            bordercolor=BORDER,
        )
        style.configure("Treeview.Heading", background=BORDER, foreground=TEXT, relief="flat")
        style.map("Treeview", background=[("selected", ACCENT)])

    def _build_ui(self) -> None:
        header = ttk.Frame(self.root, padding=(16, 12))
        header.pack(fill=tk.X)
        ttk.Label(header, text="Twitch Drops — отвязка игр", font=("Segoe UI", 16, "bold")).pack(
            side=tk.LEFT
        )
        ttk.Label(header, textvariable=self._user_label, style="Muted.TLabel").pack(side=tk.RIGHT)

        toolbar = ttk.Frame(self.root, padding=(16, 0))
        toolbar.pack(fill=tk.X)
        ttk.Button(toolbar, text="Файл аккаунтов…", command=self._pick_accounts).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(toolbar, text="Файл прокси…", command=self._pick_proxies).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(toolbar, text="Сброс прокси", command=self._clear_proxies).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(toolbar, text="Обновить игры", command=self._refresh_games).pack(side=tk.LEFT)
        ttk.Label(toolbar, textvariable=self._proxy_countdown, style="Muted.TLabel").pack(side=tk.RIGHT)

        body = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, padx=16, pady=12)

        left = ttk.Frame(body, style="Panel.TFrame", padding=8)
        body.add(left, weight=1)
        ttk.Label(left, text="Аккаунты (login:password:token)", style="Panel.TLabel").pack(anchor=tk.W)
        acc_frame = ttk.Frame(left, style="Panel.TFrame")
        acc_frame.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        acc_scroll = ttk.Scrollbar(acc_frame)
        acc_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._account_list = tk.Listbox(
            acc_frame,
            bg=PANEL,
            fg=TEXT,
            selectbackground=ACCENT,
            selectforeground="#fff",
            highlightthickness=0,
            borderwidth=0,
            yscrollcommand=acc_scroll.set,
            activestyle="none",
            font=("Consolas", 10),
        )
        self._account_list.pack(fill=tk.BOTH, expand=True)
        acc_scroll.config(command=self._account_list.yview)
        self._account_list.bind("<<ListboxSelect>>", lambda _e: self._on_account_selected())

        right = ttk.Frame(body, style="Panel.TFrame", padding=8)
        body.add(right, weight=2)
        ttk.Label(right, text="Привязанные игры Drops", style="Panel.TLabel").pack(anchor=tk.W)

        filter_row = ttk.Frame(right, style="Panel.TFrame")
        filter_row.pack(fill=tk.X, pady=(6, 6))
        ttk.Label(filter_row, text="Поиск:", style="Panel.TLabel").pack(side=tk.LEFT)
        self._search = ttk.Entry(filter_row)
        self._search.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        self._search.bind("<KeyRelease>", lambda _e: self._filter_games())

        tree_frame = ttk.Frame(right, style="Panel.TFrame")
        tree_frame.pack(fill=tk.BOTH, expand=True)
        cols = ("name", "client_id", "campaign", "status")
        self._tree = ttk.Treeview(tree_frame, columns=cols, show="headings", selectmode="browse")
        for cid, title, w in (
            ("name", "Игра", 200),
            ("client_id", "client_id", 220),
            ("campaign", "Кампания", 160),
            ("status", "Статус", 100),
        ):
            self._tree.heading(cid, text=title)
            self._tree.column(cid, width=w, stretch=(cid == "name"))
        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        manual = ttk.Frame(right, style="Panel.TFrame", padding=(0, 10, 0, 0))
        manual.pack(fill=tk.X)
        ttk.Label(manual, text="client_id вручную:", style="Panel.TLabel").pack(side=tk.LEFT)
        self._manual_id = ttk.Entry(manual, width=36)
        self._manual_id.pack(side=tk.LEFT, padx=8)
        ttk.Button(manual, text="Отвязать", style="Danger.TButton", command=self._unlink_manual).pack(
            side=tk.LEFT
        )
        ttk.Button(manual, text="Отвязать выбранную", style="Accent.TButton", command=self._unlink_selected).pack(
            side=tk.RIGHT
        )

        foot = ttk.Frame(self.root, padding=(16, 8))
        foot.pack(fill=tk.X)
        ttk.Label(foot, textvariable=self._status, style="Muted.TLabel").pack(side=tk.LEFT)

    def _run_bg(self, fn: Callable[..., Any], *args: Any, on_ok: Callable[[Any], None] | None = None, **kwargs: Any) -> None:
        def worker() -> None:
            try:
                result = fn(*args, **kwargs)
                if on_ok:
                    self._ui_q.put((on_ok, (result,), {}))
            except Exception as exc:  # noqa: BLE001
                self._ui_q.put((self._show_error_exc, (exc,), {}))

        threading.Thread(target=worker, daemon=True).start()

    def _pump_queues(self) -> None:
        while True:
            try:
                fn, args, kwargs = self._ui_q.get_nowait()
            except queue.Empty:
                break
            fn(*args, **kwargs)
        self.root.after(80, self._pump_queues)

    def _show_error(self, message: str) -> None:
        self._status.set(message)
        messagebox.showerror("Ошибка", message)

    def _show_error_exc(self, exc: BaseException) -> None:
        text = format_user_error(exc)
        self._show_error(text)

    def _pick_accounts(self) -> None:
        path = filedialog.askopenfilename(
            title="Файл аккаунтов",
            filetypes=[("Текст", "*.txt"), ("Все файлы", "*.*")],
        )
        if not path:
            return
        try:
            accounts = load_accounts_file(path)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Файл аккаунтов", str(exc))
            return
        self._accounts = accounts
        self._accounts_path = path
        self._account_list.delete(0, tk.END)
        for acc in accounts:
            self._account_list.insert(tk.END, acc.display_name)
        self._status.set(f"Загружено аккаунтов: {len(accounts)} — {path}")
        self._clear_games()
        if accounts:
            self._account_list.selection_set(0)
            self._on_account_selected()

    def _pick_proxies(self) -> None:
        path = filedialog.askopenfilename(
            title="Файл прокси",
            filetypes=[("Текст", "*.txt"), ("Все файлы", "*.*")],
        )
        if not path:
            return
        try:
            lines = parse_proxy_list(Path(path).read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            messagebox.showerror("Прокси", str(exc))
            return
        if not lines:
            messagebox.showwarning("Прокси", "Файл пуст.")
            return
        if self._proxy_rotator:
            self._proxy_rotator.stop()
        self._proxy_rotator = ProxyRotator(lines, interval_sec=60, on_rotate=self._on_proxy_rotate)
        self._proxy_rotator.start()
        if self._client:
            self._client.attach_proxy_rotator(self._proxy_rotator)
        self._status.set(f"Прокси: {len(lines)} шт., смена каждые 60 с")

    def _clear_proxies(self) -> None:
        if self._proxy_rotator:
            self._proxy_rotator.stop()
        self._proxy_rotator = None
        if self._client:
            self._client.apply_proxy(None)
        self._proxy_countdown.set("Прокси: отключены (прямое подключение)")
        self._status.set("Прокси сброшены — используется ваш интернет")
        if self._active_account:
            self._on_account_selected()

    def _on_proxy_rotate(self, _proxy: str | None) -> None:
        pass

    def _tick_proxy_label(self) -> None:
        if self._proxy_rotator:
            st = self._proxy_rotator.status()
            if st.enabled:
                left = max(0, int(st.seconds_until_rotation))
                self._proxy_countdown.set(f"Прокси {st.index + 1}/{st.total} · смена через {left} с")
        self.root.after(500, self._tick_proxy_label)

    def _on_account_selected(self) -> None:
        sel = self._account_list.curselection()
        if not sel:
            return
        acc = self._accounts[int(sel[0])]
        self._active_account = acc
        self._status.set(f"Подключение: {acc.display_name}…")
        self._run_bg(self._connect_account, acc, on_ok=self._after_connect)

    def _connect_account(self, acc: AccountLine) -> tuple[str, list[DropGameLink], TwitchUnlinkClient]:
        client = TwitchUnlinkClient(acc.token)
        if self._proxy_rotator:
            client.attach_proxy_rotator(self._proxy_rotator)
        info = client.validate()
        games = client.list_drop_game_links()
        return info.login, games, client

    def _after_connect(self, payload: tuple[str, list[DropGameLink], TwitchUnlinkClient]) -> None:
        login, games, client = payload
        self._client = client
        self._games = games
        self._user_label.set(f"@{login}")
        self._status.set(f"Аккаунт @{login}: игр с привязкой — {len(games)}")
        self._render_games(games)

    def _clear_games(self) -> None:
        self._games = []
        self._tree.delete(*self._tree.get_children())
        self._user_label.set("—")

    def _render_games(self, games: list[DropGameLink]) -> None:
        self._tree.delete(*self._tree.get_children())
        q = self._search.get().strip().lower()
        for g in games:
            if q and q not in g.name.lower() and q not in g.client_id.lower():
                continue
            status = "в инвентаре" if g.in_progress else "привязано"
            self._tree.insert("", tk.END, iid=g.client_id, values=(g.name, g.client_id, g.campaign_name or "—", status))

    def _filter_games(self) -> None:
        self._render_games(self._games)

    def _refresh_games(self) -> None:
        if not self._client:
            messagebox.showinfo("Обновить", "Сначала выберите аккаунт.")
            return
        self._status.set("Загрузка списка игр…")

        def load() -> list[DropGameLink]:
            assert self._client is not None
            return self._client.list_drop_game_links()

        self._run_bg(load, on_ok=self._on_games_refreshed)

    def _on_games_refreshed(self, games: list[DropGameLink]) -> None:
        self._games = games
        self._render_games(games)
        self._status.set(f"Список обновлён: {len(games)} игр")

    def _selected_game(self) -> DropGameLink | None:
        sel = self._tree.selection()
        if not sel:
            return None
        cid = str(sel[0])
        for g in self._games:
            if g.client_id == cid:
                return g
        return DropGameLink(
            game_id=cid,
            name=str(self._tree.item(sel[0], "values")[0]),
            client_id=cid,
            campaign_name=None,
            linked_at=None,
            in_progress=False,
        )

    def _unlink_selected(self) -> None:
        game = self._selected_game()
        if not game:
            messagebox.showinfo("Отвязка", "Выберите игру в списке.")
            return
        if not messagebox.askyesno("Подтверждение", f"Отвязать «{game.name}»?"):
            return
        self._do_unlink(game.client_id, game.name)

    def _unlink_manual(self) -> None:
        cid = self._manual_id.get().strip()
        if not cid:
            messagebox.showinfo("client_id", "Введите client_id игры.")
            return
        if not messagebox.askyesno("Подтверждение", f"Отвязать по client_id:\n{cid}"):
            return
        self._do_unlink(cid, cid)

    def _do_unlink(self, client_id: str, label: str) -> None:
        if not self._client:
            messagebox.showinfo("Отвязка", "Нет активного аккаунта.")
            return
        self._status.set(f"Отвязка «{label}»…")

        def work() -> None:
            assert self._client is not None
            self._client.unlink_drop_game(client_id)

        def done(_: None) -> None:
            self._status.set(f"Готово: «{label}» отвязана")
            self._refresh_games()

        self._run_bg(work, on_ok=done)

    def run(self) -> None:
        self.root.mainloop()
        if self._proxy_rotator:
            self._proxy_rotator.stop()


def main() -> int:
    TwitchDropsApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())