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

from textual.app import App, ComposeResult
from textual.events import Resize
from textual.containers import Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, RichLog, Select, Static

from .recorder import Recorder
from .resources import ResourceMonitor, ResourceSnapshot
from .topics import Topic, TopicDiscovery
from .profiles import ProfileStore, resolve_profile
from .topic_tree import TopicTree, SelectedTopics


class ProfileScreen(ModalScreen):
    """Save current checkboxes, or load a named selection for editing."""
    BINDINGS = [("escape", "dismiss", "Back")]

    def compose(self) -> ComposeResult:
        yield Static("Topic profiles · select a saved profile to load it", id="profile-title")
        yield Select([], id="profile-choice", prompt="Saved profiles")
        yield Input(placeholder="Profile name", id="profile-name", max_length=64)
        with Horizontal(id="profile-actions"):
            yield Button("Load", id="profile-load")
            yield Button("Save / update", id="profile-save")
            yield Button("Back", id="profile-back")
        yield Static("Save stores the current checkboxes. Same name updates; a new name creates a copy.\nOffline selections are retained. Load, close, edit checkboxes, then save to update.", id="profile-status", markup=False)

    def on_mount(self) -> None:
        self.reload_choices()
        self.query_one("#profile-name", Input).value = self.app.profile_name

    def reload_choices(self) -> None:
        try:
            data = self.app.profiles.read()
            choice = self.query_one("#profile-choice", Select)
            choice.set_options([(name, name) for name in data["profiles"]])
            choice.value = data["active"]
        except (OSError, ValueError) as exc:
            self.query_one("#profile-status", Static).update(f"Cannot read profiles: {exc}")

    def on_select_changed(self, event: Select.Changed) -> None:
        event.stop()
        if isinstance(event.value, str):
            self.query_one("#profile-name", Input).value = event.value

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        key = event.button.id
        if key == "profile-back":
            self.dismiss()
            return
        try:
            if key == "profile-save":
                name = self.query_one("#profile-name", Input).value.strip()
                self.app.profiles.save(name, self.app.selected)
                self.app.profile_name = name
                self.app._pending_profile = None
                self.reload_choices()
                self.query_one("#profile-status", Static).update(f"Saved {name}: {len(self.app.selected)} topics. This profile loads on next start.")
            elif key == "profile-load":
                name = self.query_one("#profile-choice", Select).value
                if not isinstance(name, str):
                    raise ValueError("Choose a saved profile")
                profile = self.app.profiles.activate(name)
                self.app.apply_profile(name, profile)
                self.dismiss()
        except (OSError, ValueError, KeyError) as exc:
            self.query_one("#profile-status", Static).update(f"Cannot save/load profile: {exc}")


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
    #topic-panes { height: 1fr; min-height: 8; }
    #topics { width: 2fr; height: 1fr; min-height: 3; margin: 0 1; border: round #28435d; border-title-color: #a8bed0; }
    #selected-topics { width: 1fr; min-width: 28; height: 1fr; margin-right: 1; border: round #62d4c5; border-title-color: #a8bed0; }
    #topics > .tree--cursor { background: #243f59; }
    #topics > .tree--highlight { background: #172c43; }
    #selected-topics > .option-list--option-highlighted { background: #243f59; }
    Screen.narrow #topic-panes { layout: vertical; }
    Screen.narrow #topics { width: 1fr; }
    Screen.narrow #selected-topics { width: 1fr; height: 4; min-height: 4; margin: 0 1; }
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
    ProfileScreen { padding: 1 2; background: #0b1220 98%; }
    #profile-title { height: 2; color: #62d4c5; }
    #profile-actions { height: 3; }
    #profile-status { height: auto; padding-top: 1; }
    Screen.compact #metrics { height: 1; }
    Screen.compact #warning { height: 1; }
    Screen.compact #summary { height: 1; }
    Screen.compact .metric { border: none; background: #14233a; }
    Screen.compact #selection-actions { height: 1; }
    Screen.compact #selection-actions Button { height: 1; min-height: 1; border: none; }
    """
    BINDINGS = [
        ("ctrl+r", "refresh", "Refresh"),
        ("ctrl+s", "start_recording", "Record"),
        ("ctrl+t", "stop_recording", "Stop"),
        ("ctrl+l", "logs", "Logs"),
        ("ctrl+o", "profiles", "Profiles"),
        ("ctrl+q", "safe_quit", "Quit"),
        ("ctrl+c", "safe_quit", "Quit"),
    ]

    def __init__(self, output_parent: Path | None = None, bag_name: str | None = None,
                 storage: str = "auto", discovery=None, recorder=None, monitor=None, profiles=None):
        super().__init__()
        self.output_parent = output_parent or Path.home() / "bags"
        self.initial_name = bag_name or self.new_name()
        self.storage = storage
        self.discovery = discovery or TopicDiscovery()
        self.recorder = recorder or Recorder()
        self.monitor = monitor or ResourceMonitor()
        self.topics: list[Topic] = []
        self._known_topics: dict[str, Topic] = {}
        self.selected: set[str] = set()
        self.lines: deque[str] = deque(maxlen=400)
        self.discovery_error = ""
        self.notice = "Choose topics, output directory and bag name."
        self.profiles = profiles or ProfileStore()
        self.profile_name = ""
        self._pending_profile = None
        try:
            data = self.profiles.read()
            self.profile_name = data["active"]
            self._pending_profile = data["profiles"][self.profile_name]
            self.selected = set(self._pending_profile["topics"])
        except (OSError, ValueError) as exc:
            self.notice = f"Cannot read profiles: {exc}"
        self._scanning = False
        self._sampling = False
        self._starting = False
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
            yield Button("Select all", id="all", tooltip="Select all current search/category candidates, including collapsed branches; keep other selections")
            yield Button("Select none", id="none", tooltip="Deselect current search/category candidates; keep other selections")
            yield Button("Expand", id="expand", tooltip="Expand the current topic tree")
            yield Button("Collapse", id="collapse", tooltip="Collapse the current topic tree; preserve all selections")
            yield Button("Profiles", id="profiles", tooltip="Save, load and update named topic selections · Ctrl+O")
        with Horizontal(id="topic-panes"):
            yield TopicTree(id="topics")
            yield SelectedTopics(id="selected-topics")
        with Horizontal(id="destination"):
            yield Input(str(self.output_parent), placeholder="Output directory", id="parent")
            yield Input(self.initial_name, placeholder="Bag name", id="name")
        with Horizontal(id="record-actions"):
            yield Button("● Record", id="start")
            yield Button("■ Stop", id="stop", disabled=True)
            yield Button("Refresh", id="refresh", tooltip="Rescan ROS topics manually · Ctrl+R")
            yield Static("IDLE", id="record-state", markup=False)
        yield Static(self.notice, id="summary", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.screen.set_class(self.size.height < 30, "compact")
        self.screen.set_class(self.size.width < 110, "narrow")
        self.query_one("#parent").border_title = "Output directory"
        self.query_one("#name").border_title = "Bag name"
        self.query_one("#topics").border_title = "Live graph · 0 topics"
        self.query_one("#selected-topics").border_title = "Selected · 0"
        self.query_one("#selected-topics").border_subtitle = "Click / Space to remove"
        # Discover once on startup; subsequent scans use Refresh / Ctrl+R.
        self.run_worker(self.refresh_topics())
        self.run_worker(self.sample_resources())
        self.set_interval(1, self.sample_resources)
        self.set_interval(0.25, self.poll_recorder)
        # Terminal disconnects and TERM must also request rosbag finalization.
        if os.name == "posix":
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):
                self._signal_handlers[sig] = signal.getsignal(sig)
                loop.add_signal_handler(sig, lambda: self.call_later(self.action_safe_quit))

    def on_resize(self, event: Resize) -> None:
        # Keep the main layout current even when a logs/profiles modal is open.
        for screen in self.screen_stack:
            screen.set_class(event.size.height < 30, "compact")
            screen.set_class(event.size.width < 110, "narrow")

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
            self._known_topics.update({topic.name: topic for topic in topics})
            self.discovery_error = ""
            if self._pending_profile is not None and topics:
                self.selected = resolve_profile(self._pending_profile, [t.name for t in topics])
                if not self._pending_profile.get("patterns"):
                    self._pending_profile = None
            category = self.query_one("#category", Select)
            old_value = category.value
            categories = sorted({t.category for t in topics} | {"Offline"})
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
        return [t for t in self.display_topics() if (category == "all" or t.category == category)
                and all(word in f"{t.name} {' '.join(t.types)}".casefold() for word in query.split())]

    def display_topics(self) -> list[Topic]:
        live = {topic.name for topic in self.topics}
        offline = [Topic(name, self._known_topics[name].types if name in self._known_topics else (), "Offline")
                   for name in sorted(self.selected - live)]
        return self.topics + offline

    def rebuild_topics(self) -> None:
        widget = self.query_one("#topics", TopicTree)
        visible = self.visible_topics()
        view_key = (self.query_one("#search", Input).value.strip().casefold(),
                    str(self.query_one("#category", Select).value))
        widget.set_topics(visible, self.selected, view_key)
        widget.border_title = f"Candidates · {len(visible)} topics"
        widget.border_subtitle = "Click folder: fold · Space / checkbox: select"
        self.rebuild_selected()
        self.update_summary()

    def rebuild_selected(self) -> None:
        widget = self.query_one("#selected-topics", SelectedTopics)
        names = sorted(self.selected)
        known = {topic.name: topic for topic in self.display_topics()}
        # Filtering never rebuilds this independent list or changes its scroll.
        widget.set_topics([known[name] for name in names])
        widget.border_title = f"Selected · {len(names)}"
        widget.border_subtitle = "Click / Space to remove" if names else "Choose topics from the tree"

    def on_topic_tree_toggle_requested(self, event: TopicTree.ToggleRequested) -> None:
        if self.recorder.is_active or self._starting:
            return
        self._pending_profile = None
        if event.names <= self.selected:
            self.selected.difference_update(event.names)
        else:
            self.selected.update(event.names)
        self.rebuild_topics()

    def on_selected_topics_remove_requested(self, event: SelectedTopics.RemoveRequested) -> None:
        if self.recorder.is_active or self._starting:
            return
        self._pending_profile = None
        self.selected.discard(event.name)
        self.rebuild_topics()

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
        elif key == "profiles":
            self.action_profiles()
        elif key in ("expand", "collapse"):
            widget = self.query_one("#topics", TopicTree)
            if key == "expand":
                widget.root.expand_all()
            else:
                for node in widget.root.children:
                    node.collapse_all()
        elif key in ("all", "none"):
            if self.recorder.is_active or self._starting:
                return
            self._pending_profile = None
            names = {topic.name for topic in self.visible_topics()}
            before = len(self.selected)
            if key == "all":
                self.selected.update(names)
            else:
                self.selected.difference_update(names)
            changed = abs(len(self.selected) - before)
            self.notice = f"{'Added' if key == 'all' else 'Removed'} {changed} candidates · other selections kept."
            self.rebuild_topics()

    def action_refresh(self) -> None:
        self.run_worker(self.refresh_topics())

    def action_start_recording(self) -> None:
        if not isinstance(self.screen, ModalScreen) and not self.recorder.is_active and not self._starting:
            self.run_worker(self.start_recording())

    async def start_recording(self) -> None:
        if self._starting or self.recorder.is_active:
            return
        if self.discovery_error:
            self.notice = "Refresh ROS discovery successfully before recording."
            self.update_summary()
            return
        self._starting = True
        self.notice = "Preparing recorder · checking storage and receiver settings…"
        self.lock_controls(True)
        self.update_summary()
        parent = Path(self.query_one("#parent", Input).value).expanduser()
        name = self.query_one("#name", Input).value.strip()
        try:
            path = await asyncio.wrap_future(self._start_executor.submit(
                self.recorder.start, sorted(self.selected), parent, name, self.storage,
            ))
            self.notice = f"Recording to {path}"
            settings = getattr(self.recorder, "settings_notice", "")
            if settings:
                self.query_one("#brand", Static).update(f"neobag / domain {os.environ.get('ROS_DOMAIN_ID', '0')} · {settings}")
                self.lines.append(f"Recorder settings: {settings}")
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
        for key in ("all", "none", "profiles", "selected-topics", "parent", "name", "start", "refresh"):
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
        lost = getattr(self.recorder, "transport_lost", 0)
        if lost:
            self.query_one("#record-state", Static).update(f"{state.upper()} · LOST ≥{lost}")
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
        warnings = list(s.warnings)
        configuration_warning = getattr(self.recorder, "configuration_warning", "")
        if configuration_warning:
            warnings.append(configuration_warning)
        lost = getattr(self.recorder, "transport_lost", 0)
        if lost:
            warnings.insert(0, f"ROS reported {lost} transport messages lost. Saved bag is incomplete.")
        warning.set_class(bool(warnings), "pressure")
        warning.update(" | ".join(warnings) if warnings else "Resources normal · pressure alerts estimate risk; recording rate is not measured.")

    def update_summary(self) -> None:
        if not self.is_running:
            return
        prefix = f"{len(self.selected)} selected / {len(self.topics)} discovered"
        offline = self.selected - {t.name for t in self.topics}
        if offline:
            prefix += f" / {len(offline)} offline (wait for publisher)"
        text = self.discovery_error or self.notice
        lost = getattr(self.recorder, "transport_lost", 0)
        if lost:
            text = f"ROS reported ≥{lost} messages lost · {text}"
        self.query_one("#summary", Static).update(f"{prefix}  ·  {text}")
        if not self.recorder.is_active and not self._starting:
            self.query_one("#start").disabled = not self.selected or bool(self.discovery_error)

    def action_logs(self) -> None:
        self.push_screen(LogScreen(list(self.lines)))

    def action_profiles(self) -> None:
        if not self.recorder.is_active and not self._starting and not isinstance(self.screen, ModalScreen):
            self.push_screen(ProfileScreen())

    def apply_profile(self, name: str, profile: dict) -> None:
        self.profile_name = name
        self.selected = resolve_profile(profile, [t.name for t in self.topics])
        self._pending_profile = profile if not self.topics or profile.get("patterns") else None
        self.notice = f"Loaded profile: {name}. Edit checkboxes, then Profiles → Save / update."
        self.rebuild_topics()

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
