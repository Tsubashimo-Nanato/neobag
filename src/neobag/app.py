"""Mouse and keyboard UI. Blocking OS/ROS work stays off the event loop."""
from __future__ import annotations

import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import os
import signal
import time

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, RichLog, Select, SelectionList, Static

from .recorder import Recorder
from .resources import ResourceMonitor, ResourceSnapshot
from .topics import Topic, TopicDiscovery


class LogScreen(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Back"), ("l", "dismiss", "Back")]

    def __init__(self, lines: list[str]):
        super().__init__()
        self.lines = lines

    def compose(self) -> ComposeResult:
        yield RichLog(id="log", wrap=True, markup=False)
        yield Button("Back · Esc", id="close-log")

    def on_mount(self) -> None:
        for line in self.lines:
            self.query_one(RichLog).write(line)

    def on_button_pressed(self) -> None:
        self.dismiss()


class NeoBagApp(App):
    TITLE = "neobag"
    CSS = """
    Screen { background: #0b1220; color: #dce6f1; }
    #brand { height: 1; background: #14233a; padding: 0 1; color: #62d4c5; text-style: bold; }
    #metrics { height: 3; }
    .metric { width: 1fr; border: round #28435d; padding: 0 1; }
    #warning { height: 2; padding: 0 1; color: #92a7bc; }
    #warning.pressure { background: #492e19; color: #ffcd78; text-style: bold; }
    #filters, #selection-actions, #destination, #record-actions { height: 3; padding: 0 1; }
    #search { width: 1fr; }
    #category { width: 30; }
    Button { min-width: 8; margin-right: 1; }
    #selection-actions Button { width: 1fr; }
    #topics { height: 1fr; min-height: 3; margin: 0 1; border: round #28435d; border-title-color: #a8bed0; }
    #topics:disabled { opacity: 1; }
    #parent { width: 2fr; border-title-color: #a8bed0; }
    #name { width: 1fr; border-title-color: #a8bed0; }
    #summary { height: 2; padding: 0 1; color: #a8bed0; }
    #start { background: #126e62; color: #ffffff; }
    #stop { background: #793939; color: #ffffff; }
    #record-state { width: 1fr; padding: 1 0 0 1; }
    Footer { height: 1; background: #14233a; }
    LogScreen { padding: 2 4; background: #0b1220 95%; }
    #log { height: 1fr; border: round #62d4c5; }
    Screen.compact #metrics { height: 1; }
    Screen.compact .metric { border: none; background: #14233a; }
    Screen.compact #selection-actions { height: 1; }
    Screen.compact #selection-actions Button { height: 1; min-height: 1; border: none; }
    """
    BINDINGS = [
        ("ctrl+r", "refresh", "Refresh"),
        ("ctrl+s", "start_recording", "Record"),
        ("ctrl+t", "stop_recording", "Stop"),
        ("ctrl+l", "logs", "Logs"),
        ("ctrl+q", "safe_quit", "Quit"),
        ("ctrl+c", "safe_quit", "Quit"),
    ]

    def __init__(self, output_parent: Path | None = None, bag_name: str | None = None,
                 storage: str = "sqlite3", discovery=None, recorder=None, monitor=None):
        super().__init__()
        self.output_parent = output_parent or Path.home() / "bags"
        self.initial_name = bag_name or self.new_name()
        self.storage = storage
        self.discovery = discovery or TopicDiscovery()
        self.recorder = recorder or Recorder()
        self.monitor = monitor or ResourceMonitor()
        self.topics: list[Topic] = []
        self.selected: set[str] = set()
        self.lines: deque[str] = deque(maxlen=400)
        self.discovery_error = ""
        self.notice = "Choose topics, output directory and bag name."
        self._scanning = False
        self._sampling = False
        self._starting = False
        self._rebuilding = False
        self._quit_after_stop = False
        self._last_state = "idle"
        self._signal_handlers = {}
        self._start_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="neobag-start")

    @staticmethod
    def new_name() -> str:
        return datetime.now().strftime("rosbag2_%Y_%m_%d-%H_%M_%S")

    def compose(self) -> ComposeResult:
        domain = os.environ.get("ROS_DOMAIN_ID", "0")
        yield Static(f"neobag  /  LIVE ROS 2   ·   domain {domain}   ·   {self.storage}", id="brand", markup=False)
        with Horizontal(id="metrics"):
            for key in ("cpu", "gpu", "ram", "disk"):
                yield Static(f"{key.upper()}  …", id=key, classes="metric", markup=False)
        yield Static("Sampling system resources…", id="warning", markup=False)
        with Horizontal(id="filters"):
            yield Input(placeholder="Search topic or message type", id="search")
            yield Select([("All categories", "all")], value="all", allow_blank=False, id="category")
        with Horizontal(id="selection-actions"):
            yield Button("All", id="all", tooltip="Select every discovered topic, including filtered topics")
            yield Button("None", id="none", tooltip="Clear every selection")
            yield Button("Category +", id="category-all", tooltip="Select every topic in the chosen category")
            yield Button("Category −", id="category-none", tooltip="Clear the chosen category")
        yield SelectionList[str](id="topics")
        with Horizontal(id="destination"):
            yield Input(str(self.output_parent), placeholder="Output directory", id="parent")
            yield Input(self.initial_name, placeholder="Bag name", id="name")
        with Horizontal(id="record-actions"):
            yield Button("● Record", id="start")
            yield Button("■ Stop", id="stop", disabled=True)
            yield Button("Refresh", id="refresh")
            yield Static("IDLE", id="record-state", markup=False)
        yield Static(self.notice, id="summary", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.screen.set_class(self.size.height < 30, "compact")
        self.query_one("#parent").border_title = "Output directory"
        self.query_one("#name").border_title = "Bag name"
        self.query_one("#topics").border_title = "Live graph · 0 topics"
        self.run_worker(self.refresh_topics())
        self.run_worker(self.sample_resources())
        self.set_interval(5, self.refresh_topics)
        self.set_interval(1, self.sample_resources)
        self.set_interval(0.25, self.poll_recorder)
        # Terminal disconnects and TERM must also request rosbag finalization.
        if os.name == "posix":
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):
                self._signal_handlers[sig] = signal.getsignal(sig)
                loop.add_signal_handler(sig, lambda: self.call_later(self.action_safe_quit))

    def on_resize(self) -> None:
        self.screen.set_class(self.size.height < 30, "compact")

    def on_unmount(self) -> None:
        # A worker cancellation does not stop an already running Popen call.
        # Join it before checking for an owned recorder, even on UI exceptions.
        self._start_executor.shutdown(wait=True, cancel_futures=True)
        if self.recorder.is_active:
            self.recorder.request_stop()
        for sig, previous in self._signal_handlers.items():
            asyncio.get_running_loop().remove_signal_handler(sig)
            signal.signal(sig, previous)

    def finish_cleanup(self) -> None:
        """Final fallback after Textual exits, including unexpected UI errors."""
        self._start_executor.shutdown(wait=True, cancel_futures=True)
        if not self.recorder.is_active:
            return
        self.recorder.request_stop()
        print("neobag: waiting for rosbag to finish saving…", flush=True)
        previous_error = None
        while self.recorder.is_active:
            try:
                self.recorder.poll()
                if self.recorder.error and self.recorder.error != previous_error:
                    print(self.recorder.error, flush=True)
                    previous_error = self.recorder.error
                time.sleep(0.1)
            except KeyboardInterrupt:
                print("neobag: save still in progress; waiting for the recorder.", flush=True)
        self.recorder.poll()
        print(self.recorder.error or f"neobag: saved {self.recorder.output_path}", flush=True)

    async def refresh_topics(self) -> None:
        if not self.is_running or self._scanning or self.recorder.is_active or self._starting:
            return
        self._scanning = True
        try:
            topics = await asyncio.to_thread(self.discovery.scan)
            # A record click may occur while discovery is in flight.
            if not self.is_running or self.recorder.is_active or self._starting:
                return
            self.topics = topics
            self.discovery_error = ""
            live = {t.name for t in topics}
            removed = self.selected - live
            self.selected.intersection_update(live)
            if removed:
                self.notice = f"{len(removed)} selected topic(s) disappeared; selection updated."
            category = self.query_one("#category", Select)
            old_value = category.value
            categories = sorted({t.category for t in topics})
            category.set_options([("All categories", "all")] + [(c, c) for c in categories])
            category.value = old_value if old_value in categories else "all"
            self.rebuild_topics()
        except Exception as exc:
            self.discovery_error = str(exc)
            self.lines.append(f"Discovery: {exc}")
            self.update_summary()
        finally:
            self._scanning = False

    def visible_topics(self) -> list[Topic]:
        query = self.query_one("#search", Input).value.strip().casefold()
        category = self.query_one("#category", Select).value
        return [t for t in self.topics if (category == "all" or t.category == category)
                and (not query or query in f"{t.name} {' '.join(t.types)}".casefold())]

    def rebuild_topics(self) -> None:
        widget = self.query_one("#topics", SelectionList)
        visible = self.visible_topics()
        highlighted_name = None
        if widget.highlighted is not None and widget.highlighted < widget.option_count:
            highlighted_name = widget.get_option_at_index(widget.highlighted).value
        previous_scroll = widget.scroll_y
        self._rebuilding = True
        with widget.prevent(SelectionList.SelectedChanged):
            widget.clear_options()
            for topic in visible:
                label = Text(topic.name, style="bold")
                label.append(f"  {' | '.join(topic.types)}", style="#7993ab")
                widget.add_option((label, topic.name, topic.name in self.selected))
        names = [topic.name for topic in visible]
        if highlighted_name in names:
            widget.highlighted = names.index(highlighted_name)
            widget.scroll_to(y=previous_scroll, animate=False, immediate=True)
        self._rebuilding = False
        widget.border_title = f"Live graph · {len(self.topics)} topics · {len(self.selected)} selected"
        widget.border_subtitle = "Space / click to toggle"
        self.update_summary()

    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged) -> None:
        if self._rebuilding or self.recorder.is_active or self._starting:
            return
        visible = {t.name for t in self.visible_topics()}
        self.selected.difference_update(visible)
        self.selected.update(event.selection_list.selected)
        self.query_one("#topics").border_title = f"Live graph · {len(self.topics)} topics · {len(self.selected)} selected"
        self.update_summary()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search":
            self.rebuild_topics()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "category":
            self.rebuild_topics()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        key = event.button.id
        if key == "start":
            self.action_start_recording()
        elif key == "stop":
            self.action_stop_recording()
        elif key == "refresh":
            self.action_refresh()
        elif key in ("all", "none", "category-all", "category-none"):
            if self.recorder.is_active or self._starting:
                return
            category = self.query_one("#category", Select).value
            names = {t.name for t in self.topics if key in ("all", "none") or category == "all" or t.category == category}
            if key in ("all", "category-all"):
                self.selected.update(names)
            else:
                self.selected.difference_update(names)
            self.rebuild_topics()

    def action_refresh(self) -> None:
        self.run_worker(self.refresh_topics())

    def action_start_recording(self) -> None:
        if not self.recorder.is_active and not self._starting:
            self.run_worker(self.start_recording())

    async def start_recording(self) -> None:
        if self._starting or self.recorder.is_active:
            return
        if self.discovery_error:
            self.notice = "Refresh ROS discovery successfully before recording."
            self.update_summary()
            return
        self._starting = True
        self.lock_controls(True)
        parent = Path(self.query_one("#parent", Input).value).expanduser()
        name = self.query_one("#name", Input).value.strip()
        try:
            path = await asyncio.wrap_future(self._start_executor.submit(
                self.recorder.start, sorted(self.selected), parent, name, self.storage,
            ))
            self.notice = f"Recording to {path}"
            self.lines.append(self.notice)
            if self._quit_after_stop:
                self.recorder.request_stop()
        except Exception as exc:
            self.notice = f"Cannot start: {exc}"
            self.lines.append(self.notice)
        finally:
            self._starting = False
            self.lock_controls(self.recorder.is_active)
            self.update_summary()

    def action_stop_recording(self) -> None:
        if self.recorder.is_active:
            self.recorder.request_stop()
            self.notice = "Stopping · waiting for rosbag to flush and save metadata…"
            self.update_summary()

    def lock_controls(self, locked: bool) -> None:
        if not self.is_running:
            return
        for key in ("search", "category", "all", "none", "category-all", "category-none", "topics", "parent", "name", "start", "refresh"):
            self.query_one(f"#{key}").disabled = locked
        self.query_one("#stop").disabled = not self.recorder.is_active

    def poll_recorder(self) -> None:
        # Textual can deliver an already queued timer while pruning widgets.
        # Process cleanup continues in on_unmount / finish_cleanup instead.
        if not self.is_running or self._starting:
            return
        self.recorder.poll()
        self.lines.extend(self.recorder.drain_logs())
        state = self.recorder.state
        elapsed = ""
        if self.recorder.is_active and self.recorder.started_at is not None:
            seconds = int(time.monotonic() - self.recorder.started_at)
            elapsed = f"  {seconds // 60:02d}:{seconds % 60:02d}"
        self.query_one("#record-state", Static).update(state.upper() + elapsed)
        if self.recorder.error:
            self.notice = self.recorder.error
        if state != self._last_state:
            self.lock_controls(self.recorder.is_active)
            if state == "finished":
                self.notice = f"Saved · {self.recorder.output_path}"
                self.query_one("#name", Input).value = self.new_name()
            self._last_state = state
        self.update_summary()
        if self._quit_after_stop and not self.recorder.is_active:
            self.exit()

    async def sample_resources(self) -> None:
        if not self.is_running or self._sampling:
            return
        self._sampling = True
        try:
            parent = Path(self.query_one("#parent", Input).value).expanduser()
            snapshot = await asyncio.to_thread(self.monitor.sample, parent)
            if self.is_running:
                self.show_resources(snapshot)
        except Exception as exc:
            if self.is_running:
                self.query_one("#warning", Static).update(f"Resource monitor unavailable: {exc}")
        finally:
            self._sampling = False

    def show_resources(self, s: ResourceSnapshot) -> None:
        if not self.is_running:
            return
        def percent(value):
            return "N/A" if value is None else f"{value:.0f}%"
        self.query_one("#cpu", Static).update(f"CPU  {percent(s.cpu_percent)}")
        self.query_one("#gpu", Static).update(f"GPU  {percent(s.gpu_percent)}")
        self.query_one("#gpu").tooltip = s.gpu_source
        self.query_one("#ram", Static).update(f"RAM  {percent(s.ram_percent)}")
        if s.ram_used_bytes is not None and s.ram_total_bytes:
            self.query_one("#ram").tooltip = f"{s.ram_used_bytes / 2**30:.1f} / {s.ram_total_bytes / 2**30:.1f} GiB"
        free = "N/A" if s.disk_free_bytes is None else f"{s.disk_free_bytes / 2**30:.1f}G"
        self.query_one("#disk", Static).update(f"FREE {free}")
        warning = self.query_one("#warning", Static)
        warning.set_class(bool(s.warnings), "pressure")
        warning.update(" | ".join(s.warnings) if s.warnings else "Resources normal · pressure alerts estimate risk; recording rate is not measured.")

    def update_summary(self) -> None:
        if not self.is_running:
            return
        prefix = f"{len(self.selected)} selected / {len(self.topics)} discovered"
        text = self.discovery_error or self.notice
        self.query_one("#summary", Static).update(f"{prefix}  ·  {text}")
        if not self.recorder.is_active and not self._starting:
            self.query_one("#start").disabled = not self.selected or bool(self.discovery_error)

    def action_logs(self) -> None:
        self.push_screen(LogScreen(list(self.lines)))

    def action_safe_quit(self) -> None:
        self._quit_after_stop = True
        if self._starting:
            return
        if self.recorder.is_active:
            self.action_stop_recording()
        else:
            self.exit()

    def action_quit(self) -> None:
        self.action_safe_quit()
