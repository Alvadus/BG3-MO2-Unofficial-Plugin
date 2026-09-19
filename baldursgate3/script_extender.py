import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import mobase # type: ignore
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import modSettings

SETTINGS_FILE = "ScriptExtenderSettings.json"
UPDATER_FILE = "ScriptExtenderUpdaterConfig.json"
LOADER_FILE = "DWrite.dll"

_TARGET_VERSION_PATTERN = re.compile(r"^\d+(\.\d+){0,3}$")
_DEVELOPER_NOTE = "Developer option. Leave it at the default unless a mod author tells you otherwise."
_PROFILER_NOTE = "Lua profiler warning threshold. Warnings only appear while the console is open."


@dataclass(frozen=True)
class Option:
    key: str
    label: str
    kind: str
    default: object
    description: str
    advanced: bool = False
    choices: tuple = ()


SETTINGS_OPTIONS = (
    Option("CreateConsole", "Create console", "bool", False,
           "Opens a console window with Script Extender output. Holding Shift while the game starts also opens it."),
    Option("EnableAchievements", "Enable achievements", "bool", True,
           "Keeps achievements working while mods are enabled."),
    Option("DisableLauncher", "Disable launcher", "bool", False,
           "Stops the game from opening the Larian launcher."),
    Option("SendCrashReports", "Send crash reports", "bool", True,
           "Uploads crash dumps after the game crashes."),
    Option("InsanityCheck", "Disable mod crash check", "bool", False,
           "Stops the game from turning mods off the next time it starts after a crash."),
    Option("LogRuntime", "Log runtime", "bool", False,
           "Writes Script Extender console and script output to a log file."),
    Option("LogFailedCompile", "Log failed story compilation", "bool", True,
           "Writes Osiris story compilation errors to a log file."),
    Option("LogCompile", "Log story compilation", "bool", False,
           "Logs Osiris story compilation to a log file."),
    Option("EnableLogging", "Enable Osiris logging", "bool", False,
           "Logs Osiris activity, such as rule evaluation and queries, to a log file."),
    Option("LogDirectory", "Log folder", "str", "",
           "Folder for the log files. Leave empty to use the default."),
    Option("CustomProfile", "Custom profile", "str", "",
           "Use a player profile other than Public. Enter the profile folder name."),
    Option("DeveloperMode", "Developer mode", "bool", False,
           "Debug features for mod development. Some mods print extra log messages when it is on.", True),
    Option("DisableStoryPatching", "Disable story patching", "bool", False,
           "Stops the story in a save from being updated with mod scripts when the save loads.", True),
    Option("DefaultToClientConsole", "Default to client console", "bool", False,
           "Starts the console on the client side instead of the server side.", True),
    Option("ClearOnReset", "Clear console on reset", "bool", True,
           "Clears the console when the reset command is used.", True),
    Option("EnableDebugger", "Enable Osiris debugger", "bool", False,
           "Enables the Osiris debugger interface for VS Code.", True),
    Option("DebuggerPort", "Osiris debugger port", "int", 9999,
           "Port the Osiris debugger listens on.", True),
    Option("DebugFlags", "Osiris debugger flags", "int", 0,
           "Flags passed to the Osiris debugger.", True),
    Option("EnableLuaDebugger", "Enable Lua debugger", "bool", False,
           "Enables the Lua debugger interface for VS Code.", True),
    Option("LuaDebuggerPort", "Lua debugger port", "int", 9998,
           "Port the Lua debugger listens on.", True),
    Option("LuaBuiltinResourceDirectory", "Lua built-in scripts folder", "str", "",
           "Extra folder checked for built-in scripts, for testing changes to them.", True),
    Option("ProfilerLoadThresholdError", "Profiler: mod load threshold", "int", 50000, _PROFILER_NOTE, True),
    Option("ProfilerLoadCallbackThresholdError", "Profiler: load callback threshold", "int", 50000, _PROFILER_NOTE, True),
    Option("ProfilerCallbackThresholdError", "Profiler: callback threshold", "int", 5000, _PROFILER_NOTE, True),
    Option("ProfilerClientCallbackThresholdError", "Profiler: client callback threshold", "int", 2000, _PROFILER_NOTE, True),
    Option("DisableStoryCompilation", "Disable story compilation", "bool", True, _DEVELOPER_NOTE, True),
    Option("ExtendStory", "Extend story", "bool", False, _DEVELOPER_NOTE, True),
    Option("LocalMessagePassing", "Local message passing", "bool", True, _DEVELOPER_NOTE, True),
    Option("ForceCrashReporting", "Force crash reporting", "bool", False, _DEVELOPER_NOTE, True),
    Option("Optick", "Optick profiler", "bool", False, _DEVELOPER_NOTE, True),
    Option("MergeVirtualTextures", "Merge virtual textures", "bool", False, _DEVELOPER_NOTE, True),
)

UPDATER_OPTIONS = (
    Option("UpdateChannel", "Update channel", "choice", "Release",
           "Which release stream the Script Extender updates from.", choices=("Release", "Devel", "Nightly")),
    Option("TargetVersion", "Target version", "version", "",
           "Pin a specific Script Extender version, for example 29.0.0.0. Leave empty to always use the newest."),
    Option("DisableUpdates", "Disable updates", "bool", False,
           "Stops the Script Extender from updating itself automatically."),
    Option("IPv4Only", "IPv4 only", "bool", False,
           "Only use IPv4 when downloading updates."),
    Option("TargetResourceDigest", "Target resource digest", "str", "",
           "Pin a specific build by its resource digest.", True),
    Option("CachePath", "Cache folder", "str", "",
           "Where downloaded Script Extender versions are stored. Leave empty for the default.", True),
    Option("ManifestURL", "Manifest URL", "str", "", _DEVELOPER_NOTE, True),
    Option("ManifestName", "Manifest name", "str", "", _DEVELOPER_NOTE, True),
    Option("DebugLoadSE", "Load from game folder", "bool", False, _DEVELOPER_NOTE, True),
)


@dataclass
class ConfigCopy:
    name: str
    path: Path
    active: bool
    overwrite: bool = False
    wins: bool = False
    new: bool = False

    def describe(self, winner):
        if self.new:
            return "Overwrite (new file)"
        if not self.active:
            return f"{self.name} - inactive, not used by the game"
        if self.wins:
            return f"{self.name} - used by the game"
        if winner is not None:
            return f"{self.name} - overridden by {winner.name}"
        return self.name


def _root_bin(path):
    return Path(path) / "Root" / "bin"


def is_installed(organizer: mobase.IOrganizer):
    modlist = organizer.modList()
    for mod_name in modlist.allModsByProfilePriority():
        if not modlist.state(mod_name) & mobase.ModState.ACTIVE:
            continue
        mod = modlist.getMod(mod_name)
        if mod is not None and (_root_bin(mod.absolutePath()) / LOADER_FILE).is_file():
            return True
    return (_root_bin(organizer.overwritePath()) / LOADER_FILE).is_file()


def find_config_copies(organizer: mobase.IOrganizer, file_name):
    modlist = organizer.modList()
    copies = []
    for mod_name in modlist.allModsByProfilePriority():
        mod = modlist.getMod(mod_name)
        if mod is None:
            continue
        path = _root_bin(mod.absolutePath()) / file_name
        if path.is_file():
            active = bool(modlist.state(mod_name) & mobase.ModState.ACTIVE)
            copies.append(ConfigCopy(mod_name, path, active))

    overwrite = _root_bin(organizer.overwritePath()) / file_name
    if overwrite.is_file():
        copies.append(ConfigCopy("Overwrite", overwrite, True, overwrite=True))

    if not copies:
        return [ConfigCopy("Overwrite", overwrite, True, overwrite=True, wins=True, new=True)]

    winner = next((copy for copy in reversed(copies) if copy.active), None)
    ordered = list(reversed(copies))
    if winner is not None:
        winner.wins = True
        ordered.remove(winner)
        ordered.insert(0, winner)
    return ordered


def _strip_comments(text):
    out = []
    index = 0
    length = len(text)
    in_string = False
    found = False
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if char == "\\" and index + 1 < length:
                out.append(text[index + 1])
                index += 2
                continue
            if char == '"':
                in_string = False
            index += 1
        elif char == '"':
            in_string = True
            out.append(char)
            index += 1
        elif text.startswith("//", index):
            found = True
            end = text.find("\n", index)
            index = length if end == -1 else end
        elif text.startswith("/*", index):
            found = True
            end = text.find("*/", index + 2)
            index = length if end == -1 else end + 2
        else:
            out.append(char)
            index += 1
    return "".join(out), found


def _strip_trailing_commas(text):
    out = []
    index = 0
    length = len(text)
    in_string = False
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if char == "\\" and index + 1 < length:
                out.append(text[index + 1])
                index += 2
                continue
            if char == '"':
                in_string = False
        elif char == '"':
            in_string = True
            out.append(char)
        elif char == ",":
            ahead = index + 1
            while ahead < length and text[ahead] in " \t\r\n":
                ahead += 1
            if not (ahead < length and text[ahead] in "}]"):
                out.append(char)
        else:
            out.append(char)
        index += 1
    return "".join(out)


def read_config(path: Path):
    if not path.is_file():
        return {}, False
    text, had_comments = _strip_comments(path.read_text(encoding="utf-8-sig"))
    text = _strip_trailing_commas(text)
    if not text.strip():
        return {}, had_comments
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} does not contain a JSON object")
    return data, had_comments


def _coerce(option, value):
    if option.kind == "bool":
        return value if isinstance(value, bool) else option.default
    if option.kind == "int":
        valid = isinstance(value, int) and not isinstance(value, bool) and value >= 0
        return value if valid else option.default
    if option.kind == "choice":
        return value if value in option.choices else option.default
    return value if isinstance(value, str) else option.default


def values_from_config(options, data):
    return {option.key: _coerce(option, data.get(option.key, option.default)) for option in options}


def build_config(options, values, original):
    known = {option.key for option in options}
    output = {key: value for key, value in original.items() if key not in known}
    for option in options:
        if values[option.key] != option.default:
            output[option.key] = values[option.key]
    return output


def write_config(path: Path, data, keep_backup):
    if keep_backup and path.is_file():
        backup = path.with_name(path.name + ".bak")
        if not backup.exists():
            shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    modSettings.write_text_atomically(path, json.dumps(data, indent=2) + "\n")


class _ConfigPage(QWidget):
    def __init__(self, organizer, file_name, options, parent=None):
        super().__init__(parent)
        self._organizer = organizer
        self._file_name = file_name
        self._options = options
        self._controls = {}
        self._rows = []
        self._copies = []
        self._copy = None
        self._current_index = -1
        self._original = {}
        self._loaded = {}
        self._needs_backup = False

        layout = QVBoxLayout(self)
        picker = QHBoxLayout()
        picker.addWidget(QLabel("File:", self))
        self._combo = QComboBox(self)
        picker.addWidget(self._combo, 1)
        self._open_button = QPushButton("Open folder", self)
        self._open_button.clicked.connect(lambda _checked=False: self._open_folder())
        picker.addWidget(self._open_button)
        layout.addLayout(picker)

        self._note = QLabel(self)
        self._note.setWordWrap(True)
        layout.addWidget(self._note)

        holder = QWidget()
        grid = QGridLayout(holder)
        for row, option in enumerate(options):
            label = QLabel(option.label, holder)
            label.setToolTip(option.description)
            control = self._make_control(option, holder)
            control.setToolTip(option.description)
            grid.addWidget(label, row, 0)
            grid.addWidget(control, row, 1)
            self._controls[option.key] = control
            self._rows.append((option, label, control))
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(len(options), 1)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(holder)
        layout.addWidget(scroll, 1)

        self._combo.currentIndexChanged.connect(self._on_copy_changed)
        self.refresh_copies()

    def _make_control(self, option, parent):
        if option.kind == "bool":
            return QCheckBox(parent)
        if option.kind == "int":
            box = QSpinBox(parent)
            box.setRange(0, 2147483647)
            return box
        if option.kind == "choice":
            box = QComboBox(parent)
            box.addItems(option.choices)
            return box
        edit = QLineEdit(parent)
        if option.kind == "version":
            edit.setPlaceholderText("Newest")
        return edit

    def refresh_copies(self, keep_path=None):
        self._copies = find_config_copies(self._organizer, self._file_name)
        winner = next((copy for copy in self._copies if copy.wins), None)
        index = 0
        if keep_path is not None:
            index = next((i for i, copy in enumerate(self._copies) if copy.path == keep_path), 0)
        self._combo.blockSignals(True)
        self._combo.clear()
        for copy in self._copies:
            self._combo.addItem(copy.describe(winner))
        self._combo.setCurrentIndex(index)
        self._combo.blockSignals(False)
        self._load(index)

    def _select_index(self, index):
        self._combo.blockSignals(True)
        self._combo.setCurrentIndex(index)
        self._combo.blockSignals(False)

    def _on_copy_changed(self, index):
        if index < 0 or index == self._current_index:
            return
        if self.is_dirty():
            answer = QMessageBox.question(
                self,
                "Unsaved changes",
                f"Save your changes to {self._copy.name} first?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
            )
            if answer == QMessageBox.StandardButton.Cancel or (
                answer == QMessageBox.StandardButton.Save and not self.save()
            ):
                self._select_index(self._current_index)
                return
        self._load(index)

    def _load(self, index):
        copy = self._copies[index]
        try:
            data, had_comments = read_config(copy.path)
        except (OSError, ValueError, UnicodeDecodeError) as e:
            QMessageBox.warning(self, "Script Extender settings", f"Could not read {copy.path}:\n{e}")
            data, had_comments = {}, True
        self._copy = copy
        self._current_index = index
        self._original = data
        self._needs_backup = had_comments
        self._loaded = values_from_config(self._options, data)
        self.set_values(self._loaded)
        self._open_button.setEnabled(copy.path.parent.is_dir())

        winner = next((item for item in self._copies if item.wins), None)
        if copy.new:
            note = "No settings file exists yet. Saving a change creates one in overwrite."
        elif not copy.active:
            note = "This mod is inactive, so the game won't read this file."
        elif not copy.wins and winner is not None:
            note = f"The game reads the higher priority copy in {winner.name} instead of this one."
        else:
            note = "This is the copy the game reads."
        self._note.setText(note)

    def values(self):
        values = {}
        for option in self._options:
            control = self._controls[option.key]
            if option.kind == "bool":
                values[option.key] = control.isChecked()
            elif option.kind == "int":
                values[option.key] = control.value()
            elif option.kind == "choice":
                values[option.key] = control.currentText()
            else:
                values[option.key] = control.text().strip()
        return values

    def set_values(self, values):
        for option in self._options:
            control = self._controls[option.key]
            value = values.get(option.key, option.default)
            if option.kind == "bool":
                control.setChecked(bool(value))
            elif option.kind == "int":
                control.setValue(int(value))
            elif option.kind == "choice":
                control.setCurrentText(str(value))
            else:
                control.setText(str(value))

    def reset_defaults(self):
        self.set_values({option.key: option.default for option in self._options})

    def set_advanced_visible(self, visible):
        for option, label, control in self._rows:
            shown = visible or not option.advanced
            label.setVisible(shown)
            control.setVisible(shown)

    def is_dirty(self):
        return self._copy is not None and self.values() != self._loaded

    def saved_name(self):
        return f"{self._file_name} in {self._copy.name}"

    def save(self):
        if self._copy is None:
            return True
        values = self.values()
        for option in self._options:
            value = values[option.key]
            if option.kind == "version" and value and not _TARGET_VERSION_PATTERN.match(value):
                QMessageBox.warning(
                    self,
                    "Script Extender settings",
                    f"{option.label} must look like 29.0.0.0, or be left empty.",
                )
                return False
        data = build_config(self._options, values, self._original)
        try:
            write_config(self._copy.path, data, self._needs_backup)
        except (OSError, modSettings.ModSettingsWriteError) as e:
            QMessageBox.critical(self, "Script Extender settings", f"Could not save {self._copy.path}:\n{e}")
            return False
        if self._copy.new:
            self.refresh_copies(keep_path=self._copy.path)
        else:
            self._original = data
            self._needs_backup = False
            self._loaded = values
        return True

    def _open_folder(self):
        folder = self._copy.path.parent if self._copy is not None else None
        if folder is not None and folder.is_dir():
            os.startfile(str(folder))


class ScriptExtenderSettingsDialog(QDialog):
    def __init__(self, organizer: mobase.IOrganizer, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Script Extender settings")
        self.resize(640, 640)
        self._saved = []

        layout = QVBoxLayout(self)
        self._tabs = QTabWidget(self)
        self._pages = [
            _ConfigPage(organizer, SETTINGS_FILE, SETTINGS_OPTIONS, self),
            _ConfigPage(organizer, UPDATER_FILE, UPDATER_OPTIONS, self),
        ]
        self._tabs.addTab(self._pages[0], "Settings")
        self._tabs.addTab(self._pages[1], "Updater")
        layout.addWidget(self._tabs, 1)

        self._advanced = QCheckBox("Show developer options", self)
        self._advanced.toggled.connect(self._set_advanced_visible)
        layout.addWidget(self._advanced)
        layout.addWidget(QLabel("Changes take effect the next time you start the game.", self))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close, self
        )
        reset = buttons.addButton("Reset to defaults", QDialogButtonBox.ButtonRole.ResetRole)
        reset.clicked.connect(lambda _checked=False: self._tabs.currentWidget().reset_defaults())
        buttons.button(QDialogButtonBox.StandardButton.Save).clicked.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._set_advanced_visible(False)

    def saved_files(self):
        return list(self._saved)

    def _set_advanced_visible(self, visible):
        for page in self._pages:
            page.set_advanced_visible(visible)

    def _save(self):
        for page in self._pages:
            if not page.is_dirty():
                continue
            name = page.saved_name()
            if not page.save():
                self._tabs.setCurrentWidget(page)
                return
            self._saved.append(name)
        self.accept()

    def reject(self):
        if any(page.is_dirty() for page in self._pages):
            answer = QMessageBox.question(
                self,
                "Unsaved changes",
                "Discard your unsaved changes?",
                QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Discard:
                return
        super().reject()
