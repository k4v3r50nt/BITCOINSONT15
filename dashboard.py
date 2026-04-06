import asyncio
import time
from datetime import datetime
from typing import Optional, List, Dict, Any

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table
from rich.text import Text
from rich import box

# ── ASCII art header ────────────────────────────────────────────────────────

ASCII_LOGO = """\
 ██████╗ ██╗████████╗ ██████╗ ██████╗ ███╗   ██╗███████╗ ██████╗ ███╗   ██╗████████╗ ██╗███████╗
 ██╔══██╗██║╚══██╔══╝██╔════╝██╔═══██╗████╗  ██║██╔════╝██╔═══██╗████╗  ██║╚══██╔══╝███║██╔════╝
 ██████╔╝██║   ██║   ██║     ██║   ██║██╔██╗ ██║███████╗██║   ██║██╔██╗ ██║   ██║   ╚██║███████╗
 ██╔══██╗██║   ██║   ██║     ██║   ██║██║╚██╗██║╚════██║██║   ██║██║╚██╗██║   ██║    ██║╚════██║
 ██████╔╝██║   ██║   ╚██████╗╚██████╔╝██║ ╚████║███████║╚██████╔╝██║ ╚████║   ██║    ██║███████║
 ╚═════╝ ╚═╝   ╚═╝    ╚═════╝ ╚═════╝ ╚═╝  ╚═══╝╚══════╝ ╚═════╝ ╚═╝  ╚═══╝   ╚═╝    ╚═╝╚══════╝"""

SMALL_LOGO = "  ₿  BITCOINSONT15"

# ── Chart helpers ────────────────────────────────────────────────────────────

CHART_WIDTH = 60
CHART_HEIGHT = 16
BRAILLE_UP = "▲"
BRAILLE_DN = "▼"

BLOCK_CHARS = [" ", "▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]


def _normalize(prices: List[float], lo: float, hi: float, height: int) -> List[int]:
    if hi == lo:
        return [height // 2] * len(prices)
    return [
        min(height - 1, max(0, int((p - lo) / (hi - lo) * (height - 1))))
        for p in prices
    ]


def build_price_chart(
    price_history: List[Dict],
    open_price: float,
    chart_width: int = CHART_WIDTH,
    chart_height: int = CHART_HEIGHT,
) -> List[Text]:
    """Build ASCII line chart. Returns list of Rich Text lines (top to bottom)."""
    now = time.time()
    window_start = now - 900

    # Filter to window and sample to chart_width points
    pts = [p for p in price_history if p["timestamp"] >= window_start]
    if len(pts) < 2:
        lines = []
        for _ in range(chart_height):
            lines.append(Text("─" * chart_width, style="dim"))
        lines.append(Text("  No price data yet  ".center(chart_width), style="dim yellow"))
        return lines

    # Sample evenly
    step = max(1, len(pts) // chart_width)
    sampled = [pts[i]["price"] for i in range(0, len(pts), step)]
    sampled = sampled[:chart_width]
    # Pad with last value if needed
    while len(sampled) < chart_width:
        sampled.append(sampled[-1])

    lo = min(sampled)
    hi = max(sampled)
    padding = (hi - lo) * 0.1 if hi != lo else 50
    lo -= padding
    hi += padding

    rows = [[" "] * chart_width for _ in range(chart_height)]
    normalized = _normalize(sampled, lo, hi, chart_height)

    current = sampled[-1] if sampled else 0

    for x, y in enumerate(normalized):
        # Draw column line from bottom to point
        for row_y in range(chart_height):
            if row_y == y:
                rows[chart_height - 1 - row_y][x] = "●" if x == chart_width - 1 else "─"
            elif row_y < y:
                rows[chart_height - 1 - row_y][x] = " "
            else:
                rows[chart_height - 1 - row_y][x] = " "

    # Draw line connecting points
    for x in range(1, chart_width):
        y0 = normalized[x - 1]
        y1 = normalized[x]
        if y1 > y0:
            for mid in range(y0, y1 + 1):
                rows[chart_height - 1 - mid][x] = "╱"
        elif y1 < y0:
            for mid in range(y1, y0 + 1):
                rows[chart_height - 1 - mid][x] = "╲"
        else:
            rows[chart_height - 1 - y1][x] = "─"

    # Draw open price reference line
    if open_price > 0 and lo < open_price < hi:
        open_row = int((open_price - lo) / (hi - lo) * (chart_height - 1))
        open_row = chart_height - 1 - open_row
        if 0 <= open_row < chart_height:
            for x in range(chart_width):
                if rows[open_row][x] == " ":
                    rows[open_row][x] = "·"

    # Convert to Rich Text with colors
    result_lines = []
    # Y-axis labels
    y_labels = [
        f"${hi:>8,.0f}",
        " " * 10,
        f"${(hi + lo) / 2:>8,.0f}",
        " " * 10,
        f"${lo:>8,.0f}",
    ]
    label_rows = [0, chart_height // 4, chart_height // 2, 3 * chart_height // 4, chart_height - 1]

    for row_i, row in enumerate(rows):
        line_str = "".join(row)
        is_above_open = (chart_height - 1 - row_i) >= (
            int((open_price - lo) / (hi - lo) * (chart_height - 1)) if hi != lo and open_price > 0 else 0
        )
        color = "green" if current >= open_price else "red"

        y_label = ""
        for li, label_row in enumerate(label_rows):
            if row_i == label_row and li < len(y_labels):
                y_label = y_labels[li]
                break

        t = Text()
        if y_label:
            t.append(y_label, style="dim cyan")
        else:
            t.append(" " * 10, style="")
        t.append("│", style="dim")
        t.append(line_str, style=color)
        result_lines.append(t)

    # X-axis
    x_axis = Text()
    x_axis.append(" " * 10, style="")
    x_axis.append("└" + "─" * chart_width, style="dim")
    result_lines.append(x_axis)

    # Time labels
    time_row = Text()
    time_row.append(" " * 11, style="")
    marks = [0, 12, 24, 36, 48, 59]
    positions = []
    for mark in marks:
        ts = window_start + (mark / chart_width) * 900
        label = datetime.fromtimestamp(ts).strftime("%H:%M")
        positions.append((mark, label))

    prev_pos = 0
    for pos, label in positions:
        gap = pos - prev_pos
        time_row.append(" " * gap, style="")
        time_row.append(label, style="dim cyan")
        prev_pos = pos + len(label)
    result_lines.append(time_row)

    return result_lines


# ── Dashboard state ──────────────────────────────────────────────────────────

class DashboardState:
    def __init__(self, initial_bankroll: float):
        self.initial_bankroll = initial_bankroll
        self.bankroll: float = initial_bankroll
        self.current_price: float = 0.0
        self.delta_pct: float = 0.0
        self.delta_1min: float = 0.0
        self.window_high: float = 0.0
        self.window_low: float = 0.0
        self.volume: float = 0.0
        self.window_open_price: float = 0.0
        self.price_history: List[Dict] = []
        self.rsi: Optional[float] = None
        self.macd: Optional[float] = None
        self.vwap: Optional[float] = None

        # Signal state
        self.signal_direction: Optional[str] = None
        self.signal_confidence: float = 0.0
        self.strategy_momentum: Optional[str] = None
        self.strategy_mean_rev: Optional[str] = None
        self.strategy_macd: Optional[str] = None
        self.skip_reason: Optional[str] = None

        # Window state
        self.current_slug: str = "—"
        self.time_remaining: int = 900
        self.window_progress: float = 0.0

        # Risk state
        self.circuit_breaker_active: bool = False
        self.circuit_breaker_remaining: int = 0

        # Trade state
        self.active_trade: Optional[Dict] = None

        # Stats
        self.stats: Dict = {}
        self.recent_trades: List[Dict] = []

        self.last_update: float = 0.0


# ── Layout builders ──────────────────────────────────────────────────────────

def _make_header(state: DashboardState) -> Panel:
    bankroll_color = "bright_green" if state.bankroll >= state.initial_bankroll else "bright_red"
    bankroll_diff = state.bankroll - state.initial_bankroll
    diff_str = f"+${bankroll_diff:.2f}" if bankroll_diff >= 0 else f"-${abs(bankroll_diff):.2f}"

    t = Text()
    t.append(SMALL_LOGO + "\n", style="bold bright_green")
    t.append("  MODE: ", style="dim")
    t.append("[PAPER TRADING]", style="bold yellow")
    t.append("   BANKROLL: ", style="dim")
    t.append(f"${state.bankroll:.2f}", style=f"bold {bankroll_color}")
    t.append(f" ({diff_str})", style=bankroll_color)
    t.append(f"   Updated: {datetime.now().strftime('%H:%M:%S')}", style="dim")

    return Panel(t, style="bold", border_style="bright_green", height=4)


def _make_price_panel(state: DashboardState) -> Panel:
    t = Text()
    price = state.current_price
    t.append(f"${price:>12,.2f}\n", style="bold bright_white")

    delta = state.delta_pct
    arrow = "▲" if delta >= 0 else "▼"
    delta_color = "bright_green" if delta >= 0 else "bright_red"
    dollar_delta = price * abs(delta) / 100
    t.append(f"{arrow} ${dollar_delta:.2f} ({delta:+.2f}%) from open\n", style=f"bold {delta_color}")

    d1 = state.delta_1min
    arrow1 = "▲" if d1 >= 0 else "▼"
    d1_color = "green" if d1 >= 0 else "red"
    t.append(f"{arrow1} {d1:+.3f}% last 1min\n\n", style=d1_color)

    t.append(f"HIGH  ${state.window_high:>10,.2f}\n", style="green")
    t.append(f"LOW   ${state.window_low:>10,.2f}\n", style="red")
    t.append(f"VOL   {state.volume:>10.3f} BTC\n", style="dim cyan")
    if state.vwap:
        t.append(f"VWAP  ${state.vwap:>10,.2f}\n", style="dim yellow")
    if state.rsi is not None:
        rsi_color = "red" if state.rsi > 70 else ("green" if state.rsi < 30 else "cyan")
        t.append(f"RSI   {state.rsi:>10.1f}\n", style=rsi_color)

    return Panel(t, title="[bold]BTC/USD[/bold]", border_style="cyan", padding=(0, 1))


def _make_chart_panel(state: DashboardState) -> Panel:
    chart_lines = build_price_chart(
        state.price_history,
        state.window_open_price,
        CHART_WIDTH,
        CHART_HEIGHT,
    )
    t = Text()
    for line in chart_lines:
        t.append_text(line)
        t.append("\n")
    return Panel(
        t,
        title="[bold]BTC Price — Last 15min[/bold]",
        border_style="blue",
        padding=(0, 0),
    )


def _make_signal_panel(state: DashboardState) -> Panel:
    t = Text()

    # Window info
    slug_short = state.current_slug[-20:] if len(state.current_slug) > 20 else state.current_slug
    t.append(f"Window: {slug_short}\n\n", style="dim")

    # Time remaining
    mins = state.time_remaining // 60
    secs = state.time_remaining % 60
    t.append(f"Time Left: ", style="dim")
    t.append(f"{mins:02d}:{secs:02d}\n", style="bold bright_yellow")

    # Progress bar
    filled = int(state.window_progress * 20)
    bar = "█" * filled + "░" * (20 - filled)
    t.append(f"[{bar}] {state.window_progress * 100:.0f}%\n\n", style="yellow")

    # Signal
    if state.signal_direction == "UP":
        t.append("  ⬆  UP  \n", style="bold bright_green on black")
    elif state.signal_direction == "DOWN":
        t.append("  ⬇  DOWN  \n", style="bold bright_red on black")
    else:
        t.append("  ──  SKIP  \n", style="bold dim")
        if state.skip_reason:
            t.append(f"  ({state.skip_reason})\n", style="dim red")

    # Confidence bar
    conf = state.signal_confidence
    conf_filled = int(conf * 10)
    conf_bar = "█" * conf_filled + "░" * (10 - conf_filled)
    conf_color = "bright_green" if conf >= 0.67 else ("yellow" if conf >= 0.33 else "red")
    t.append(f"\nConf: [{conf_bar}] {conf * 100:.0f}%\n\n", style=conf_color)

    # Strategy votes
    def strat_icon(val):
        if val is not None:
            return ("✓", "green") if val else ("✗", "red")
        return ("─", "dim")

    icon, color = strat_icon(state.strategy_momentum)
    t.append(f"Momentum    [{icon}] ", style=color)
    t.append(f"{str(state.strategy_momentum or '—')}\n", style="dim")

    icon, color = strat_icon(state.strategy_mean_rev)
    t.append(f"Mean Rev    [{icon}] ", style=color)
    t.append(f"{str(state.strategy_mean_rev or '—')}\n", style="dim")

    icon, color = strat_icon(state.strategy_macd)
    t.append(f"MACD Cross  [{icon}] ", style=color)
    t.append(f"{str(state.strategy_macd or '—')}\n\n", style="dim")

    # Circuit breaker
    if state.circuit_breaker_active:
        rem_min = state.circuit_breaker_remaining // 60
        t.append(f"⚠ CIRCUIT BREAKER\n", style="bold red")
        t.append(f"  Paused {rem_min}m remaining\n", style="red")
    else:
        t.append("Circuit Breaker: OK\n", style="dim green")

    # Active trade
    if state.active_trade:
        t.append("\n─── ACTIVE TRADE ───\n", style="bold yellow")
        at = state.active_trade
        t.append(f"Dir: {at.get('direction', '—')}\n", style="yellow")
        t.append(f"Cost: ${at.get('cost_usd', 0):.2f}\n", style="yellow")

    return Panel(t, title="[bold]Signals & State[/bold]", border_style="magenta", padding=(0, 1))


def _make_stats_panel(state: DashboardState) -> Panel:
    stats = state.stats

    # Top stats
    t = Text()
    total = stats.get("total", 0)
    win_rate = stats.get("win_rate", 0.0)
    total_pnl = stats.get("total_pnl", 0.0)
    best = stats.get("best_trade", 0.0)
    worst = stats.get("worst_trade", 0.0)

    pnl_color = "bright_green" if total_pnl >= 0 else "bright_red"
    pnl_str = f"+${total_pnl:.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):.2f}"

    t.append(f"Trades: {total}  |  ", style="dim")
    t.append(f"Win Rate: {win_rate:.1f}%  |  ", style="cyan")
    t.append(f"P&L: {pnl_str}  |  ", style=pnl_color)
    t.append(f"Best: +${best:.2f}  |  ", style="green")
    t.append(f"Worst: -${abs(worst):.2f}\n", style="red")

    # Trades table
    table = Table(box=box.SIMPLE, expand=True, show_header=True, header_style="bold dim")
    table.add_column("Window", style="dim", width=12)
    table.add_column("Dir", width=5)
    table.add_column("Conf", width=6)
    table.add_column("Result", width=7)
    table.add_column("P&L", width=8)
    table.add_column("Bankroll", width=10)

    for trade in state.recent_trades:
        resolved = trade.get("resolved", 0)
        win = trade.get("win")
        pnl = trade.get("pnl")
        direction = trade.get("direction", "—")
        conf = trade.get("confidence", 0)
        bankroll_after = trade.get("bankroll_after")
        window_ts = trade.get("window_ts", 0)

        window_str = datetime.fromtimestamp(window_ts).strftime("%H:%M") if window_ts else "—"
        dir_style = "bright_green" if direction == "UP" else "bright_red"

        if resolved and win is not None:
            result_str = "WIN ✓" if win else "LOSS ✗"
            result_style = "bright_green" if win else "bright_red"
            pnl_str_t = f"+${pnl:.2f}" if pnl and pnl >= 0 else f"-${abs(pnl or 0):.2f}"
            pnl_style = "green" if pnl and pnl >= 0 else "red"
            br_str = f"${bankroll_after:.2f}" if bankroll_after else "—"
        else:
            result_str = "OPEN"
            result_style = "yellow"
            pnl_str_t = "—"
            pnl_style = "dim"
            br_str = "—"

        table.add_row(
            window_str,
            Text(direction[:4], style=dir_style),
            f"{conf * 100:.0f}%",
            Text(result_str, style=result_style),
            Text(pnl_str_t, style=pnl_style),
            br_str,
        )

    combined = Text()
    combined.append_text(t)
    return Panel(
        Layout(
            Panel(combined, border_style="dim", height=3),
            name="top",
        ),
        title="[bold]Performance & Trades[/bold]",
        border_style="blue",
    )


def _make_full_stats_panel(state: DashboardState) -> Panel:
    """Full panel with stats row + trades table."""
    stats = state.stats

    total = stats.get("total", 0)
    win_rate = stats.get("win_rate", 0.0)
    total_pnl = stats.get("total_pnl", 0.0)
    best = stats.get("best_trade", 0.0)
    worst = stats.get("worst_trade", 0.0)

    pnl_color = "bright_green" if total_pnl >= 0 else "bright_red"
    pnl_str = f"+${total_pnl:.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):.2f}"

    table = Table(box=box.SIMPLE_HEAVY, expand=True, show_header=True, header_style="bold cyan")
    table.add_column("Window", width=8)
    table.add_column("Dir", width=5)
    table.add_column("Conf", width=6)
    table.add_column("Result", width=8)
    table.add_column("P&L", width=9)
    table.add_column("Bankroll", width=10)

    for trade in state.recent_trades[:8]:
        resolved = trade.get("resolved", 0)
        win = trade.get("win")
        pnl = trade.get("pnl")
        direction = trade.get("direction", "—")
        conf = trade.get("confidence", 0)
        bankroll_after = trade.get("bankroll_after")
        window_ts = trade.get("window_ts", 0)

        window_str = datetime.fromtimestamp(window_ts).strftime("%H:%M") if window_ts else "—"
        dir_style = "bright_green" if direction == "UP" else "bright_red"

        if resolved and win is not None:
            result_str = "WIN ✓" if win else "LOSS ✗"
            result_style = "bright_green" if win else "bright_red"
            pnl_val = pnl or 0
            pnl_str_t = f"+${pnl_val:.2f}" if pnl_val >= 0 else f"-${abs(pnl_val):.2f}"
            pnl_style = "green" if pnl_val >= 0 else "red"
            br_str = f"${bankroll_after:.2f}" if bankroll_after else "—"
        else:
            result_str = "OPEN"
            result_style = "yellow"
            pnl_str_t = "—"
            pnl_style = "dim"
            br_str = "—"

        table.add_row(
            window_str,
            Text(direction[:4], style=dir_style),
            f"{conf * 100:.0f}%",
            Text(result_str, style=result_style),
            Text(pnl_str_t, style=pnl_style),
            br_str,
        )

    summary = Text()
    summary.append(f"Trades: {total}  ", style="dim")
    summary.append(f"WinRate: {win_rate:.1f}%  ", style="cyan")
    summary.append(f"P&L: {pnl_str}  ", style=pnl_color)
    summary.append(f"Best: +${best:.2f}  ", style="green")
    summary.append(f"Worst: ${worst:.2f}", style="red")

    from rich.console import Group as RichGroup
    content = RichGroup(summary, table)

    return Panel(content, title="[bold]Recent Trades[/bold]", border_style="blue", height=14)


def build_layout(state: DashboardState) -> Layout:
    layout = Layout()

    layout.split_column(
        Layout(name="header", size=4),
        Layout(name="main"),
        Layout(name="footer", size=14),
    )

    layout["main"].split_row(
        Layout(name="price", ratio=2),
        Layout(name="chart", ratio=4),
        Layout(name="signals", ratio=2),
    )

    layout["header"].update(_make_header(state))
    layout["price"].update(_make_price_panel(state))
    layout["chart"].update(_make_chart_panel(state))
    layout["signals"].update(_make_signal_panel(state))
    layout["footer"].update(_make_full_stats_panel(state))

    return layout


class Dashboard:
    def __init__(self, initial_bankroll: float):
        self.state = DashboardState(initial_bankroll)
        self._live: Optional[Live] = None
        self._console = Console()

    def start(self):
        self._live = Live(
            build_layout(self.state),
            console=self._console,
            refresh_per_second=1,
            screen=True,
        )
        self._live.start()

    def stop(self):
        if self._live:
            self._live.stop()

    def update(self, **kwargs):
        for k, v in kwargs.items():
            if hasattr(self.state, k):
                setattr(self.state, k, v)
        self.state.last_update = time.time()
        if self._live:
            self._live.update(build_layout(self.state))

    def update_from_market(self, snapshot: dict):
        self.update(
            current_price=snapshot.get("price", 0),
            delta_pct=snapshot.get("delta_pct", 0),
            delta_1min=snapshot.get("delta_1min", 0),
            window_high=snapshot.get("window_high", 0),
            window_low=snapshot.get("window_low", 0),
            volume=snapshot.get("volume", 0),
            window_open_price=snapshot.get("window_open_price", 0),
            price_history=snapshot.get("price_history", []),
            rsi=snapshot.get("rsi"),
            macd=snapshot.get("macd"),
            vwap=snapshot.get("vwap"),
        )

    def update_from_signal(self, signal: dict):
        details = signal.get("strategy_details", {})
        self.update(
            signal_direction=signal.get("direction"),
            signal_confidence=signal.get("confidence", 0.0),
            strategy_momentum=details.get("momentum"),
            strategy_mean_rev=details.get("mean_reversion"),
            strategy_macd=details.get("macd_cross"),
            skip_reason=signal.get("skip_reason"),
        )

    def update_from_scanner(self, scanner):
        self.update(
            current_slug=scanner.current_slug,
            time_remaining=scanner.time_remaining(),
            window_progress=scanner.window_progress(),
        )

    def update_from_risk(self, risk_status: dict):
        self.update(
            circuit_breaker_active=risk_status.get("circuit_breaker_active", False),
            circuit_breaker_remaining=risk_status.get("circuit_breaker_remaining", 0),
            bankroll=risk_status.get("bankroll", self.state.bankroll),
        )

    def update_trades(self, stats: dict, recent_trades: list):
        self.update(stats=stats, recent_trades=recent_trades)
