"""Namespace tree with explicit selection actions and readable type badges."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib

from rich.style import Style
from rich.text import Text
from textual import events
from textual.message import Message
from textual.widgets import Tree, OptionList
from textual.widgets.tree import TreeNode
from textual.widgets.option_list import Option

from .topics import Topic


# Light foregrounds remain legible on both the normal and focused dark rows.
TYPE_COLORS = {
    "sensor_msgs/msg/Image": "#7dd3fc",
    "sensor_msgs/msg/CompressedImage": "#93c5fd",
    "sensor_msgs/msg/CameraInfo": "#c4b5fd",
    "sensor_msgs/msg/Imu": "#fdba74",
    "sensor_msgs/msg/MagneticField": "#fda4af",
    "sensor_msgs/msg/PointCloud2": "#86efac",
    "sensor_msgs/msg/LaserScan": "#bef264",
    "nav_msgs/msg/Odometry": "#5eead4",
    "tf2_msgs/msg/TFMessage": "#fde68a",
    "geometry_msgs/msg/Twist": "#fef08a",
    "rcl_interfaces/msg/Log": "#cbd5e1",
}
PALETTE = ("#7dd3fc", "#c4b5fd", "#86efac", "#fdba74", "#fda4af", "#5eead4", "#fde68a")


def type_color(message_type: str) -> str:
    if message_type in TYPE_COLORS:
        return TYPE_COLORS[message_type]
    # Python's hash is randomized; the same custom type needs a stable color.
    return PALETTE[hashlib.sha256(message_type.encode()).digest()[0] % len(PALETTE)]


def append_types(label: Text, topic: Topic) -> None:
    for message_type in topic.types:
        label.append(f"  [{message_type.rsplit('/', 1)[-1]}]", style=type_color(message_type))
    if topic.category == "Offline":
        label.append("  [offline]", style="#fde68a")


def selected_label(topic: Topic, width: int) -> Text:
    label = Text("× " + topic.name.rsplit("/", 1)[-1], style="bold #dce6f1")
    append_types(label, topic)
    label.truncate(width, overflow="ellipsis")
    namespace = topic.name.rsplit("/", 1)[0] or "/"
    if len(namespace) > width - 2:
        parts = namespace.strip("/").split("/")
        while len("…/" + "/".join(parts)) > width - 2 and len(parts) > 1:
            parts.pop(0)
        namespace = "…/" + "/".join(parts)
        if len(namespace) > width - 2:
            namespace = "…" + namespace[-(width - 3):]
    label.append(f"\n  {namespace}", style="#a8bed0")
    return label


class SelectedTopics(OptionList):
    """A two-line, scrollable tray; selecting a row requests its removal."""
    BINDINGS = [("space", "select", "Remove topic")]

    class RemoveRequested(Message):
        def __init__(self, name: str):
            super().__init__()
            self.name = name

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._topics: tuple[Topic, ...] = ()
        self._display_key = None

    def set_topics(self, topics: list[Topic]) -> None:
        self._topics = tuple(topics)
        width = max(16, self.content_size.width - 2)
        key = (self._topics, width)
        if key == self._display_key:
            return
        self._display_key = key
        previous_scroll = self.scroll_y
        highlighted = self.highlighted
        name = self.get_option_at_index(highlighted).id if highlighted is not None and highlighted < self.option_count else None
        self.clear_options()
        self.add_options(Option(selected_label(topic, width), id=topic.name) for topic in topics)
        names = [topic.name for topic in topics]
        if name in names:
            self.highlighted = names.index(name)
        elif highlighted is not None and names:
            self.highlighted = min(highlighted, len(names) - 1)
        self.scroll_to(y=previous_scroll, animate=False, immediate=True)

    def on_resize(self) -> None:
        self.set_topics(list(self._topics))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.post_message(self.RemoveRequested(event.option.id))

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        topic = next((topic for topic in self._topics if topic.name == event.option.id), None)
        if topic:
            self.tooltip = topic.name + "\n" + "\n".join(topic.types)


@dataclass
class Entry:
    key: str
    path: str
    names: list[str] = field(default_factory=list)
    topic: Topic | None = None
    caption: str = ""


class TopicTree(Tree[Entry]):
    """Arrows/namespace labels expand; checkboxes or Space change selections."""
    BINDINGS = [
        ("space", "toggle_selection", "Toggle selection"),
        ("left", "collapse_cursor", "Collapse"),
        ("right", "expand_cursor", "Expand"),
    ]

    class ToggleRequested(Message):
        def __init__(self, names: list[str]):
            super().__init__()
            self.names = set(names)

    def __init__(self, **kwargs):
        super().__init__("/", **kwargs)
        self.show_root = False
        self.auto_expand = False
        self.guide_depth = 2
        self.root.expand()
        self.nodes: dict[str, TreeNode[Entry]] = {}
        self.chosen: set[str] = set()
        self._topics: tuple[Topic, ...] = ()
        self._view_key = None
        self._normal_expanded: set[str] | None = None

    def set_topics(self, topics: list[Topic], selected: set[str], view_key: tuple[str, str]) -> None:
        self.chosen = set(selected)
        identity = tuple(sorted(topics, key=lambda topic: topic.name))
        if identity == self._topics and view_key == self._view_key:
            self.update_checks()
            return
        expanded = {key for key, node in self.nodes.items() if node.is_expanded}
        anchor = self.cursor_node.data.key if self.cursor_node and self.cursor_node.data else None
        previous_scroll = self.scroll_y
        same_view = view_key == self._view_key
        had_topics = bool(self._topics)
        # Startup filter events can render an empty tree before discovery
        # finishes. That is not a user's decision to collapse every namespace.
        if self._view_key == ("", "all") and had_topics:
            self._normal_expanded = expanded
        if same_view and had_topics:
            restore_expanded = expanded
        elif view_key == ("", "all"):
            restore_expanded = self._normal_expanded
        else:
            restore_expanded = None
        self._view_key = view_key
        self._topics = identity
        self.move_cursor(None)
        self.clear()
        self.root.expand()
        self.nodes = {}
        filtered = view_key != ("", "all")
        # Build a namespace trie first so single-directory chains can share a
        # row. Real topics remain separate leaves even if a namespace has the
        # same name as a topic, e.g. /camera and /camera/image_raw.
        trie = {}
        for topic in identity:
            children = trie
            parts = topic.name.strip("/").split("/")
            for depth in range(1, len(parts)):
                path = "/" + "/".join(parts[:depth])
                key = "namespace:" + path
                if key not in children:
                    children[key] = (Entry(key, path), {})
                entry, children = children[key]
                entry.names.append(topic.name)
            key = "topic:" + topic.name
            children[key] = (Entry(key, topic.name, [topic.name], topic), {})

        def populate(parent, children):
            for entry, descendants in children.values():
                if entry.topic:
                    self.nodes[entry.key] = parent.add_leaf("", entry)
                    continue
                aliases = [entry.key]
                while len(descendants) == 1:
                    child_entry, child_descendants = next(iter(descendants.values()))
                    if child_entry.topic:
                        break
                    entry, descendants = child_entry, child_descendants
                    aliases.append(entry.key)
                parent_path = parent.data.path if parent.data else ""
                entry.caption = entry.path[len(parent_path):].lstrip("/")
                expand = (any(key in restore_expanded for key in aliases) if restore_expanded is not None
                          else filtered or parent is self.root)
                node = parent.add("", entry, expand=expand)
                for key in aliases:
                    self.nodes[key] = node
                populate(node, descendants)

        populate(self.root, trie)
        self.update_checks()
        # New TreeNodes receive their line numbers lazily on the first build.
        # Resolve that public lookup before restoring a cursor to a new node.
        self.get_node_at_line(0)
        if same_view and anchor in self.nodes:
            node = self.nodes[anchor]
            # A collapsed ancestor is a valid cursor target; hidden leaves are not.
            while node.parent is not None and node.parent is not self.root and not node.parent.is_expanded:
                node = node.parent
            self.move_cursor(node)
            self.call_after_refresh(self.scroll_to, y=previous_scroll, animate=False, immediate=True)
        else:
            self.move_cursor(self.root.children[0] if self.root.children else None)
            self.scroll_to(y=0, animate=False, immediate=True)

    def update_checks(self) -> None:
        for node in set(self.nodes.values()):
            entry = node.data
            count = sum(name in self.chosen for name in entry.names)
            mark = "x" if count == len(entry.names) else "−" if count else " "
            label = Text()
            label.append(f"[{mark}] ", style=Style(color="#dce6f1", meta={"checkbox": True}))
            if entry.topic:
                label.append(entry.path.rsplit("/", 1)[-1], style="bold #dce6f1")
                append_types(label, entry.topic)
            else:
                label.append(entry.caption + "/", style="bold #dce6f1")
                label.append(f"  {count}/{len(entry.names)}", style="#a8bed0")
            node.set_label(label)

    def render_label(self, node, base_style, style) -> Text:
        label = node.label.copy()
        # Cursor/hover styles must not wash out the explicit type badge colors.
        label.stylize_before(style)
        if node.allow_expand:
            icon = self.ICON_NODE_EXPANDED if node.is_expanded else self.ICON_NODE
            return Text.assemble((icon, base_style + Style(meta={"toggle": True})), label)
        return label

    def on_click(self, event: events.Click) -> None:
        if event.style.meta.get("checkbox") and "line" in event.style.meta:
            node = self.get_node_at_line(event.style.meta["line"])
            if node is not None and node.data:
                self.move_cursor(node)
                self.post_message(self.ToggleRequested(node.data.names))
            event.prevent_default()
            event.stop()

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        event.stop()
        if event.node.data is None:
            return
        if event.node.data.topic:
            self.post_message(self.ToggleRequested(event.node.data.names))
        else:
            event.node.toggle()

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        if event.node.data:
            entry = event.node.data
            self.tooltip = entry.path + ("\n" + "\n".join(entry.topic.types) if entry.topic else "\nSpace: select/clear this namespace")

    def action_toggle_selection(self) -> None:
        if self.cursor_node and self.cursor_node.data:
            self.post_message(self.ToggleRequested(self.cursor_node.data.names))

    def action_collapse_cursor(self) -> None:
        node = self.cursor_node
        if node is not None:
            if node.allow_expand and node.is_expanded:
                node.collapse()
            elif node.parent is not self.root:
                self.move_cursor(node.parent)

    def action_expand_cursor(self) -> None:
        if self.cursor_node and self.cursor_node.allow_expand:
            self.cursor_node.expand()
