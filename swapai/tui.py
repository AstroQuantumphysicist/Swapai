"""SwapAI Textual TUI — the coolest Codex router dashboard."""

from __future__ import annotations

import time

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (Button, DataTable, Footer, Header, Input, Label,
                             RichLog, Static)

from . import accounts, codex_client, config, usage
from .accounts import LoginFlow, refresh_account
from .router import router
from .server import ServerThread

BANNER = (
    "[bold cyan] ███████ ██     ██  █████  ██████   █████  ██[/bold cyan]\n"
    "[bold cyan] ██      ██     ██ ██   ██ ██   ██ ██   ██ ██[/bold cyan]\n"
    "[bold blue] ███████ ██  █  ██ ███████ ██████  ███████ ██[/bold blue]\n"
    "[bold magenta]      ██ ██ ███ ██ ██   ██ ██      ██   ██ ██[/bold magenta]\n"
    "[bold magenta] ███████  ███ ███  ██   ██ ██      ██   ██ ██[/bold magenta]\n"
    "          [dim italic]the best Codex router[/dim italic]"
)

# 5-row block digits for the headline number.
_BIG = {
    "0": ["█████", "█   █", "█   █", "█   █", "█████"],
    "1": ["  ██ ", " ███ ", "  ██ ", "  ██ ", " ████"],
    "2": ["█████", "    █", "█████", "█    ", "█████"],
    "3": ["█████", "    █", " ████", "    █", "█████"],
    "4": ["█   █", "█   █", "█████", "    █", "    █"],
    "5": ["█████", "█    ", "█████", "    █", "█████"],
    "6": ["█████", "█    ", "█████", "█   █", "█████"],
    "7": ["█████", "    █", "   █ ", "  █  ", "  █  "],
    "8": ["█████", "█   █", "█████", "█   █", "█████"],
    "9": ["█████", "█   █", "█████", "    █", "█████"],
    ".": ["     ", "     ", "     ", "     ", "  █  "],
    ",": ["     ", "     ", "     ", "  █  ", " █   "],
    "K": ["█  █ ", "█ █  ", "██   ", "█ █  ", "█  █ "],
    "M": ["█   █", "██ ██", "█ █ █", "█   █", "█   █"],
    "B": ["████ ", "█   █", "████ ", "█   █", "████ "],
    " ": ["     "] * 5,
}

# Top-to-bottom gradient for the hero digits.
_HERO_GRADIENT = ["#7dd3fc", "#38bdf8", "#0ea5e9", "#6366f1", "#a855f7"]


def _big_number(s: str) -> str:
    rows = ["", "", "", "", ""]
    for ch in s:
        glyph = _BIG.get(ch.upper(), _BIG[" "])
        for i in range(5):
            rows[i] += glyph[i] + " "
    return "\n".join(
        f"[{_HERO_GRADIENT[i]}]{row}[/]" for i, row in enumerate(rows))


def _human(n: float) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "0"
    if n >= 1_000_000_000:
        return f"{n/1e9:.2f}B"
    if n >= 1_000_000:
        return f"{n/1e6:.2f}M"
    if n >= 1_000:
        return f"{n/1e3:.1f}K"
    return f"{n:.0f}"


_SPARK = "▁▂▃▄▅▆▇█"
_SPARK_COLORS = ["#475569", "#0891b2", "#06b6d4", "#22d3ee",
                 "#67e8f9", "#a5f3fc", "#fbbf24", "#f97316"]


def _sparkline(series: list[int], width: int = 48) -> str:
    if not series or not any(series):
        return "[dim]── no traffic yet — point a client at the API ──[/dim]"
    data = series[-width:]
    peak = max(data) or 1
    out = []
    for v in data:
        idx = int((v / peak) * (len(_SPARK) - 1))
        out.append(f"[{_SPARK_COLORS[idx]}]{_SPARK[idx]}[/]")
    return "".join(out)


def _meter(percent_left: float, width: int = 10) -> str:
    """Colored remaining-limit meter."""
    left = max(0.0, min(100.0, percent_left))
    color = "#4ade80" if left > 50 else "#facc15" if left > 15 else "#f87171"
    n = round(left / 100 * width)
    return f"[{color}]{'█' * n}[/][#334155]{'░' * (width - n)}[/] [{color}]{left:.0f}%[/]"


class ApiKeyModal(ModalScreen[str]):
    BINDINGS = [("escape", "dismiss('')", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label("[b cyan]⚿  Network API key[/b cyan]")
            yield Label("[dim]Clients authenticate with Bearer <key>. "
                        "Saved to ~/.swapai/.env[/dim]")
            yield Input(value=config.get_api_key() or "",
                        placeholder="sk-swapai-...", id="key-input")
            with Horizontal(id="modal-buttons"):
                yield Button("⚡ Generate", id="gen", variant="primary")
                yield Button("✔ Save", id="save", variant="success")
                yield Button("✕ Cancel", id="cancel")

    def on_button_pressed(self, e: Button.Pressed) -> None:
        inp = self.query_one("#key-input", Input)
        if e.button.id == "gen":
            inp.value = config.generate_api_key()
        elif e.button.id == "save":
            val = inp.value.strip()
            if val:
                try:
                    config.set_api_key(val)
                except Exception:
                    pass
            self.dismiss(val)
        else:
            self.dismiss("")


class SwapAIApp(App):
    CSS_PATH = "tui.tcss"
    TITLE = "SwapAI"
    SUB_TITLE = "the best Codex router"
    BINDINGS = [
        ("a", "add_account", "＋ Account"),
        ("d", "delete_account", "✕ Account"),
        ("r", "refresh", "↻ Limits"),
        ("s", "toggle_server", "▶ Server"),
        ("k", "set_key", "⚿ API key"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.server = ServerThread()
        self._started_at = time.time()

    # ---- layout ------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True, icon="◢◤")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Static(BANNER, id="banner")
                yield Static(id="server-card")
                yield DataTable(id="accounts-table", zebra_stripes=True,
                                cursor_type="row")
            with Vertical(id="center"):
                yield Static(id="hero")
                with Horizontal(id="tiles"):
                    yield Static(id="tile-in", classes="tile")
                    yield Static(id="tile-out", classes="tile")
                    yield Static(id="tile-rate", classes="tile")
                    yield Static(id="tile-req", classes="tile")
                yield Static(id="spark-card")
                with Horizontal(id="dash-bottom"):
                    yield Static(id="models-card")
                    yield Static(id="capacity-card")
            with Vertical(id="right"):
                yield RichLog(id="log", highlight=True, markup=True,
                              max_lines=300)
        yield Footer()

    def on_mount(self) -> None:
        try:
            self.theme = "tokyo-night"
        except Exception:
            pass
        # Panel titles drawn into the borders.
        titles = {
            "#server-card": " ⚡ SERVER ",
            "#accounts-table": " 👤 ACCOUNTS ",
            "#hero": " ◆ TOTAL TOKENS PROCESSED ",
            "#spark-card": " 〜 THROUGHPUT · 60 MIN ",
            "#models-card": " ◇ BY MODEL · 24H ",
            "#capacity-card": " ∞ 24/7 CAPACITY PLAN ",
            "#log": " ☰ ACTIVITY ",
        }
        for sel, title in titles.items():
            try:
                self.query_one(sel).border_title = title
            except Exception:
                pass
        table = self.query_one("#accounts-table", DataTable)
        table.add_columns("", "Account", "Plan", "5h left", "Learned cap")
        self.log_line("[bold cyan]SwapAI online.[/bold cyan]")
        self.log_line("[dim]a[/dim] add account · [dim]s[/dim] start server"
                      " · [dim]k[/dim] API key")
        if not config.get_api_key():
            self.log_line("[yellow]⚠ No API key — server would be OPEN. "
                          "Press k.[/yellow]")
        self.refresh_views()
        self.set_interval(3, self.refresh_views)

    # ---- helpers -----------------------------------------------------
    def log_line(self, msg: str) -> None:
        try:
            self.query_one("#log", RichLog).write(
                f"[#475569]{time.strftime('%H:%M:%S')}[/] {msg}")
        except Exception:
            pass

    def refresh_views(self) -> None:
        # Never let a render error crash the app.
        try:
            router.reload()
        except Exception:
            pass
        for fn in (self._render_accounts, self._render_server_card,
                   self._render_dashboard):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                self.log_line(f"[red]render: {exc}[/red]")

    def _render_accounts(self) -> None:
        table = self.query_one("#accounts-table", DataTable)
        table.clear()
        accs = router.accounts
        for i, a in enumerate(accs):
            active = i == router.active_index and not a.is_rate_limited
            marker = ("[#4ade80]▶[/]" if active else
                      "[#f87171]■[/]" if a.is_rate_limited else
                      "[#64748b]·[/]")
            cap = (f"[#4ade80]{_human(a.learned_tokens_per_5h)}[/]"
                   if a.learned_tokens_per_5h > 0
                   else "[dim italic]learning…[/dim italic]")
            prim = (_meter(a.primary.remaining_percent, 8)
                    if a.primary.window_minutes else "[dim]—[/dim]")
            table.add_row(marker, a.email[:20], a.plan or "?", prim, cap,
                          key=a.id)
        if not accs:
            table.add_row("", "[dim]none — press a to login[/dim]", "", "", "")

    def _render_server_card(self) -> None:
        key = config.get_api_key()
        key_disp = (f"[#4ade80]{key[:12]}…[/]" if key
                    else "[#f87171]not set ⚠[/]")
        state = ("[#4ade80 b]● RUNNING[/]" if self.server.running
                 else "[#64748b]○ stopped[/]")
        models = router.common_models()
        models_disp = (f"[#a855f7]{', '.join(models)}[/]" if models
                       else "[dim]none common yet[/dim]")
        up = int(time.time() - self._started_at)
        uptime = f"{up // 3600:02d}:{up % 3600 // 60:02d}:{up % 60:02d}"
        card = (
            f"{state}   [dim]up[/dim] {uptime}\n"
            f"[#38bdf8 u]http://{self.server.host}:{self.server.port}/v1[/]\n"
            f"[dim]key[/dim]     {key_disp}\n"
            f"[dim]models[/dim]  {models_disp}\n"
            f"[dim]active[/dim]  "
            f"#{router.active_index + 1 if router.accounts else 0}"
            f" of {len(router.accounts)} accounts"
        )
        self.query_one("#server-card", Static).update(card)

    # ---- the big dashboard ------------------------------------------
    def _render_dashboard(self) -> None:
        life = usage.lifetime_stats()
        hour = usage.stats_last_hours(1.0)

        total = life.total_tokens
        hero = (
            f"\n{_big_number(_human(total))}\n\n"
            f"[dim]{total:,} tokens · {life.requests:,} requests · "
            f"since first traffic {life.span_hours:.1f}h[/dim]"
        )
        self.query_one("#hero", Static).update(hero)

        def tile(icon: str, label: str, value: str, color: str,
                 sub: str) -> str:
            return (f"[{color}]{icon}[/] [dim]{label}[/dim]\n"
                    f"[bold {color}]{value}[/]\n[#64748b]{sub}[/]")

        self.query_one("#tile-in", Static).update(
            tile("▼", "INPUT /h", _human(hour.input_per_hour), "#4ade80",
                 f"{life.input_tokens:,} total"))
        self.query_one("#tile-out", Static).update(
            tile("▲", "OUTPUT /h", _human(hour.output_per_hour), "#f472b6",
                 f"{life.output_tokens:,} total"))
        self.query_one("#tile-rate", Static).update(
            tile("⚡", "TOKENS /h", _human(hour.tokens_per_hour), "#facc15",
                 "last 60 min"))
        self.query_one("#tile-req", Static).update(
            tile("✦", "REQUESTS /h", f"{hour.requests}", "#38bdf8",
                 f"{life.requests:,} total"))

        series = usage.throughput_series(minutes=60, buckets=48)
        peak = max(series) if series else 0
        self.query_one("#spark-card", Static).update(
            f"{_sparkline(series, 48)}\n"
            f"[dim]-60m[/dim]{' ' * 36}[dim]now[/dim]  "
            f"[#fbbf24]peak {_human(peak)}[/]")

        bd = usage.per_model_breakdown(hours=24)
        if bd:
            lines = []
            top = max((d["in"] + d["out"]) for d in bd.values()) or 1
            for m, d in sorted(bd.items(),
                               key=lambda kv: -(kv[1]["in"] + kv[1]["out"])):
                tot = d["in"] + d["out"]
                bar_n = max(1, round(tot / top * 12))
                lines.append(
                    f"[#a855f7]{m[:17]:<17}[/] [#6366f1]{'▮' * bar_n}[/]"
                    f" {_human(tot)} [dim]({d['requests']} req)[/dim]")
            self.query_one("#models-card", Static).update("\n".join(lines))
        else:
            self.query_one("#models-card", Static).update(
                "[dim italic]no traffic yet[/dim italic]")

        plans = [a.plan for a in router.accounts]
        learned = [a.learned_tokens_per_5h for a in router.accounts]
        sub = usage.subscriptions_needed(plans, hour.tokens_per_hour, learned)
        sustain = ("[#4ade80 b]✔ SUSTAINABLE[/]" if sub.sustainable
                   else "[#f87171 b]✘ NOT SUSTAINABLE[/]")
        src = ("[#4ade80]tiktoken-learned[/]" if sub.learned
               else "[#facc15]plan estimate[/]")
        cap = (
            f"[dim]source[/dim]     {src}\n"
            f"[dim]per sub[/dim]    {_human(sub.capacity_per_sub_per_hour)} tok/h\n"
            f"[dim]fleet now[/dim]  {_human(sub.total_capacity_per_hour)} tok/h"
            f" · {sub.current_subs} sub\n"
            f"[dim]need 24/7[/dim]  [b]{sub.subs_needed}[/b] sub"
            f"  [dim](have {sub.current_subs})[/dim]\n"
            f"{sustain}"
        )
        self.query_one("#capacity-card", Static).update(cap)

    # ---- actions -----------------------------------------------------
    def action_set_key(self) -> None:
        self.push_screen(ApiKeyModal(), lambda _v: self.refresh_views())

    def action_toggle_server(self) -> None:
        try:
            if self.server.running:
                self.server.stop()
                self.log_line("[yellow]■ Server stopped.[/yellow]")
            else:
                if not router.accounts:
                    self.log_line("[red]Add an account first (a).[/red]")
                    return
                self.server.start()
                self.log_line(
                    f"[#4ade80]▶ Serving at http://{self.server.host}:"
                    f"{self.server.port}/v1[/]")
        except Exception as exc:  # noqa: BLE001
            self.log_line(f"[red]server: {exc}[/red]")
        self.refresh_views()

    def action_add_account(self) -> None:
        self.log_line("[cyan]⇢ Opening browser for Codex login…[/cyan]")
        self._do_login()

    @work(thread=True, exclusive=False)
    def _do_login(self) -> None:
        try:
            flow = LoginFlow()
            self.call_from_thread(
                self.log_line,
                f"If the browser didn't open: [link]{flow.auth_url}[/link]")
            flow.open_browser()
            code = flow.wait_for_code()
            acc = flow.exchange(code)
            acc.save()
            self.call_from_thread(
                self.log_line,
                f"[#4ade80]✔ {acc.email} ({acc.plan or 'unknown plan'})[/]")
            self.call_from_thread(self.log_line,
                                  "[cyan]⇢ Probing models…[/cyan]")
            models = codex_client.probe_models(acc)
            self.call_from_thread(
                self.log_line,
                f"[#4ade80]✔ {len(models)} models[/] "
                f"[dim]({', '.join(models) or 'none'})[/dim]")
            self.call_from_thread(self.refresh_views)
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.log_line,
                                  f"[red]✘ Login failed: {exc}[/red]")

    def action_delete_account(self) -> None:
        table = self.query_one("#accounts-table", DataTable)
        if not router.accounts:
            return
        try:
            row_key = table.coordinate_to_cell_key(
                table.cursor_coordinate).row_key
        except Exception:
            return
        for a in router.accounts:
            if a.id == row_key.value:
                try:
                    a.delete()
                except Exception:
                    pass
                self.log_line(f"[yellow]✕ Removed {a.email}[/yellow]")
                break
        self.refresh_views()

    def action_refresh(self) -> None:
        self.log_line("[cyan]↻ Refreshing tokens & limits…[/cyan]")
        self._refresh_all()

    @work(thread=True, exclusive=True)
    def _refresh_all(self) -> None:
        for a in accounts.list_accounts():
            try:
                refresh_account(a)
                codex_client.probe_models(a)
            except Exception as exc:  # noqa: BLE001
                self.call_from_thread(
                    self.log_line, f"[red]{a.email}: {exc}[/red]")
        self.call_from_thread(self.refresh_views)
        self.call_from_thread(self.log_line, "[#4ade80]✔ Limits updated.[/]")


def run() -> None:
    config.load_env()
    SwapAIApp().run()
