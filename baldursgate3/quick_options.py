import os
from pathlib import Path
import time

import mobase # type: ignore
from PyQt6.QtCore import QDateTime, qDebug, Qt, QTimer
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QGridLayout,
    QGroupBox,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import modSettings
from . import script_extender

_SHOW_SELECTED_COUNT_FROM = 2
_TOOLTIP_PAK_LIMIT = 20
_SCRIPT_EXTENDER_CHECK_SECONDS = 5


class BG3QuickOptions:
    def __init__(self, organizer: mobase.IOrganizer, plugin_name, appdata_directory):
        self._organizer = organizer
        self._plugin_name = plugin_name
        self._appdata_directory = appdata_directory
        self._main_window = None
        self._quick_options_tab = None
        self._quick_options_selected_buttons = []
        self._quick_options_timer = None
        self._quick_options_log = None
        self._divine_status_box = None
        self._divine_status_label = None
        self._dotnet_runtimes = None
        self._pak_mod_cache = {}
        self._script_extender_box = None
        self._script_extender_label = None
        self._script_extender_checked_at = 0.0

    def install_tab(self, window):
        self._main_window = window
        self._connect_selection_signals()
        if self._quick_options_tab is not None:
            return

        tab_widget = self._find_right_panel_tab_widget(window)
        if tab_widget is None:
            qDebug("BG3 quick options tab: right panel tab widget was not found.")
            return

        for index in range(tab_widget.count()):
            if tab_widget.tabText(index).replace("&", "").strip().casefold() == "bg3":
                self._quick_options_tab = tab_widget.widget(index)
                return

        data_index = self._find_tab_index(tab_widget, "Data")
        insert_index = data_index if data_index >= 0 else tab_widget.count()
        tab = self._create_quick_options_tab(tab_widget)
        self._quick_options_tab = tab
        tab_widget.insertTab(insert_index, tab, "BG3")

    def _find_right_panel_tab_widget(self, window):
        for tab_widget in window.findChildren(QTabWidget):
            tab_names = {
                tab_widget.tabText(index).replace("&", "").strip().casefold()
                for index in range(tab_widget.count())
            }
            if "data" in tab_names and ("saves" in tab_names or "downloads" in tab_names):
                return tab_widget
        return None

    def _find_tab_index(self, tab_widget, tab_name):
        normalized = tab_name.casefold()
        for index in range(tab_widget.count()):
            if tab_widget.tabText(index).replace("&", "").strip().casefold() == normalized:
                return index
        return -1

    def _create_quick_options_tab(self, parent):
        tab = QWidget(parent)
        layout = QVBoxLayout(tab)

        mods_box = QGroupBox("Mods", tab)
        mods_layout = QGridLayout(mods_box)
        import_button = QPushButton("Import manually installed mods", mods_box)
        import_button.clicked.connect(lambda _checked=False: self._import_manual_mods())
        mods_layout.addWidget(import_button, 0, 0)
        mods_layout.setColumnStretch(1, 1)

        cache_box = QGroupBox("Metadata cache", tab)
        cache_layout = QGridLayout(cache_box)

        regenerate_all_button = QPushButton("Regenerate all paks metadata", cache_box)
        regenerate_all_button.clicked.connect(lambda _checked=False: self._pak_metadata_action(True, False))
        cache_layout.addWidget(regenerate_all_button, 0, 0)

        regenerate_selected_button = QPushButton("Regenerate selected paks metadata", cache_box)
        regenerate_selected_button.clicked.connect(lambda _checked=False: self._pak_metadata_action(True, True))
        cache_layout.addWidget(regenerate_selected_button, 0, 1)

        clear_all_button = QPushButton("Clear all paks metadata", cache_box)
        clear_all_button.clicked.connect(lambda _checked=False: self._pak_metadata_action(False, False))
        cache_layout.addWidget(clear_all_button, 1, 0)

        clear_selected_button = QPushButton("Clear selected paks metadata", cache_box)
        clear_selected_button.clicked.connect(lambda _checked=False: self._pak_metadata_action(False, True))
        cache_layout.addWidget(clear_selected_button, 1, 1)
        cache_layout.setColumnStretch(2, 1)

        self._quick_options_selected_buttons = [
            clear_selected_button,
            regenerate_selected_button,
        ]

        script_extender_box = QGroupBox("Script Extender", tab)
        script_extender_layout = QGridLayout(script_extender_box)
        script_extender_button = QPushButton("Edit settings...", script_extender_box)
        script_extender_button.clicked.connect(lambda _checked=False: self._edit_script_extender_settings())
        script_extender_layout.addWidget(script_extender_button, 0, 0)
        script_extender_label = QLabel(script_extender_box)
        script_extender_layout.addWidget(script_extender_label, 0, 1)
        script_extender_layout.setColumnStretch(1, 1)
        self._script_extender_box = script_extender_box
        self._script_extender_label = script_extender_label

        divine_box = QGroupBox("Dependencies", tab)
        divine_layout = QGridLayout(divine_box)
        divine_label = QLabel(divine_box)
        divine_label.setTextFormat(Qt.TextFormat.RichText)
        divine_layout.addWidget(divine_label, 0, 0)
        divine_layout.setColumnStretch(1, 1)
        self._divine_status_box = divine_box
        self._divine_status_label = divine_label

        log_box = QPlainTextEdit(tab)
        log_box.setReadOnly(True)
        log_box.setMaximumBlockCount(200)
        log_box.setPlaceholderText("Action log")
        log_box.setMinimumHeight(160)
        self._quick_options_log = log_box

        self._start_quick_options_timer(tab)
        self._update_quick_options_selected_counts()
        self._refresh_divine_status()
        self._refresh_script_extender_box(force=True)

        layout.addWidget(mods_box)
        layout.addWidget(cache_box)
        layout.addWidget(script_extender_box)
        layout.addWidget(divine_box)
        layout.addWidget(log_box, 1)
        return tab

    def _dotnet_runtime_versions(self):
        if self._dotnet_runtimes is not None:
            return self._dotnet_runtimes

        roots = []
        for candidate in (
            os.getenv("DOTNET_ROOT"),
            os.getenv("ProgramW6432"),
            os.getenv("ProgramFiles"),
            r"C:\Program Files",
        ):
            if not candidate:
                continue
            root = Path(candidate)
            if root.name.casefold() != "dotnet":
                root = root / "dotnet"
            if root not in roots:
                roots.append(root)

        versions = set()
        for root in roots:
            shared = root / "shared" / "Microsoft.NETCore.App"
            if not shared.is_dir():
                continue
            try:
                entries = list(shared.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.is_dir():
                    versions.add(entry.name)

        self._dotnet_runtimes = sorted(versions)
        return self._dotnet_runtimes

    def _divine_runtime_version(self):
        for version in self._dotnet_runtime_versions():
            if version.split(".", 1)[0] == "8":
                return version
        return ""

    def _refresh_divine_status(self):
        if self._divine_status_label is None:
            return

        version = self._divine_runtime_version()
        if version:
            state = f'<span style="color:#3fb950;">Installed</span> ({version})'
        else:
            state = '<span style="color:#f85149;">Not installed</span> - .NET 8 Desktop Runtime x64'

        unreadable = modSettings.unreadable_pak_names()
        without_lslib = modSettings.fallback_pak_names()
        tooltip = []
        if unreadable:
            paks = "pak" if len(unreadable) == 1 else "paks"
            state += f" - <span style='color:#f85149;'>{len(unreadable)} {paks} couldn't be read</span>"
            tooltip += ["Couldn't be read:"] + self._tooltip_pak_names(unreadable)
        if without_lslib:
            paks = "pak" if len(without_lslib) == 1 else "paks"
            state += f" - {len(without_lslib)} {paks} read without LSLib"
            tooltip += ["Read without LSLib:"] + self._tooltip_pak_names(without_lslib)

        self._divine_status_label.setToolTip("\n".join(tooltip))
        self._divine_status_label.setText(f"LSLib dependencies: {state}")
        self._divine_status_box.setVisible(True)

    def _tooltip_pak_names(self, pak_names):
        shown = pak_names[:_TOOLTIP_PAK_LIMIT]
        if len(pak_names) > _TOOLTIP_PAK_LIMIT:
            shown.append(f"...and {len(pak_names) - _TOOLTIP_PAK_LIMIT} more")
        return shown

    def _refresh_script_extender_box(self, force=False):
        if self._script_extender_box is None:
            return
        if not self._script_extender_editor_enabled():
            self._script_extender_box.setVisible(False)
            return
        now = time.monotonic()
        if not force and now - self._script_extender_checked_at < _SCRIPT_EXTENDER_CHECK_SECONDS:
            return
        self._script_extender_checked_at = now

        try:
            installed = script_extender.is_installed(self._organizer)
            copies = (
                script_extender.find_config_copies(self._organizer, script_extender.SETTINGS_FILE)
                if installed else []
            )
        except Exception as e:
            qDebug(f"BG3: Script Extender check failed: {e}")
            return

        self._script_extender_box.setVisible(installed)
        if not installed:
            return
        winner = next((copy for copy in copies if copy.wins), None)
        if not copies or copies[0].new:
            text = "No settings file yet. Saving a change creates one in overwrite."
        elif winner is None:
            text = "Settings files exist only in inactive mods."
        else:
            text = f"Settings file used by the game: {winner.name}"
        self._script_extender_label.setText(text)

    def _script_extender_editor_enabled(self):
        try:
            return bool(self._organizer.pluginSetting(self._plugin_name, "script_extender_settings_editor"))
        except Exception:
            return False

    def _edit_script_extender_settings(self):
        try:
            dialog = script_extender.ScriptExtenderSettingsDialog(self._organizer, self._mo2_main_window())
            dialog.exec()
            for name in dialog.saved_files():
                self.log(f"Script Extender: saved {name}.")
        except Exception as e:
            qDebug(f"BG3: Script Extender settings failed: {e}")
            self.log(f"Script Extender: could not open the settings: {e}")
        self._refresh_script_extender_box(force=True)

    def _start_quick_options_timer(self, parent):
        if self._quick_options_timer is not None:
            return
        self._quick_options_timer = QTimer(parent)
        self._quick_options_timer.timeout.connect(self._refresh_quick_options)
        self._quick_options_timer.start(1000)

    def _refresh_quick_options(self):
        self._connect_selection_signals()
        self._update_quick_options_selected_counts()
        self._refresh_script_extender_box()

    def _connect_selection_signals(self):
        main_window = self._mo2_main_window()
        if main_window is None:
            return
        for view in main_window.findChildren(QAbstractItemView):
            try:
                selection_model = view.selectionModel()
                if selection_model is None:
                    continue
                try:
                    selection_model.selectionChanged.disconnect(self._on_selection_changed)
                except TypeError:
                    pass
                selection_model.selectionChanged.connect(self._on_selection_changed)
            except RuntimeError:
                continue

    def _on_selection_changed(self, *_args):
        self._update_quick_options_selected_counts()

    def _update_quick_options_selected_counts(self):
        try:
            selected_count = len(self._selected_pak_mod_names())
            labels = [
                "Clear selected paks metadata",
                "Regenerate selected paks metadata",
            ]
            for button, label in zip(self._quick_options_selected_buttons, labels):
                if selected_count >= _SHOW_SELECTED_COUNT_FROM:
                    button.setText(f"{label} ({selected_count})")
                else:
                    button.setText(label)
                button.setEnabled(selected_count > 0)
            self._refresh_divine_status()
        except Exception as e:
            qDebug(f"BG3: selection refresh failed: {e}")

    def _selected_pak_mod_names(self):
        selected_names = set()
        known_names = set(self._organizer.modList().allMods())
        main_window = self._mo2_main_window()
        if main_window is None:
            return []

        for view in main_window.findChildren(QAbstractItemView):
            selection_model = view.selectionModel()
            if selection_model is None:
                continue

            if not selection_model.hasSelection():
                continue

            model = view.model()
            selected_indexes = list(selection_model.selectedIndexes())
            if not selected_indexes:
                selected_indexes = list(selection_model.selectedRows())
            current_index = view.currentIndex()
            if current_index.isValid() and selection_model.isSelected(current_index):
                selected_indexes.append(current_index)
            for index in selected_indexes:
                for name in self._mod_names_from_selected_index(model, index, known_names):
                    if self._mod_contains_pak(name):
                        selected_names.add(name)

        return sorted(selected_names)

    def _mod_names_from_selected_index(self, model, index, known_names):
        names = []
        raw_name = str(model.data(index, Qt.ItemDataRole.DisplayRole) or "").strip()
        if raw_name in known_names:
            names.append(raw_name)

        try:
            column_count = model.columnCount(index.parent())
        except Exception:
            return names

        for column in range(column_count):
            row_index = model.index(index.row(), column, index.parent())
            name = str(model.data(row_index, Qt.ItemDataRole.DisplayRole) or "").strip()
            if name in known_names and name not in names:
                names.append(name)
        return names

    def _mo2_main_window(self):
        if self._main_window is not None:
            return self._main_window
        if hasattr(self._organizer, "mainWindow"):
            return self._organizer.mainWindow()
        return None

    def _all_pak_mod_names(self):
        return [
            mod_name
            for mod_name in self._organizer.modList().allMods()
            if self._mod_contains_pak(mod_name)
        ]

    def invalidate_pak_cache(self):
        self._pak_mod_cache.clear()

    def notify_mods_changed(self, mod_names):
        if not hasattr(self._organizer, "modDataChanged"):
            return
        modlist = self._organizer.modList()
        for mod_name in mod_names:
            try:
                mod = modlist.getMod(mod_name)
                if mod is not None:
                    self._organizer.modDataChanged(mod)
                    return
            except Exception as e:
                qDebug(f"BG3: could not refresh the content flags for {mod_name}: {e}")

    def _mod_contains_pak(self, mod_name):
        cached = self._pak_mod_cache.get(mod_name)
        if cached is not None:
            return cached

        mod = self._organizer.modList().getMod(mod_name)
        if not mod:
            return False

        found = any((Path(mod.absolutePath()) / "PAK_FILES").glob("*.pak"))
        if not found:
            found = self._mod_has_pak_metadata_section(mod_name)
        self._pak_mod_cache[mod_name] = found
        return found

    def _mod_has_pak_metadata_section(self, mod_name):
        mod = self._organizer.modList().getMod(mod_name)
        if not mod:
            return False

        meta_ini = Path(mod.absolutePath()) / "meta.ini"
        if not meta_ini.exists():
            return False

        config = modSettings.read_meta_ini(meta_ini)
        if config is None:
            return False
        return any(section.casefold().endswith(".pak") for section in config.sections())

    def _confirm_quick_action(self, title, message):
        result = QMessageBox.question(
            self._mo2_main_window(),
            title,
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return result == QMessageBox.StandardButton.Yes

    def log(self, message):
        if self._quick_options_log is None:
            return
        timestamp = QDateTime.currentDateTime().toString("HH:mm:ss")
        self._quick_options_log.appendPlainText(f"[{timestamp}] {message}")

    def _run_pak_metadata_action(self, title, callback, success_message, changed_mods=None):
        self.log(f"{title}: started.")
        readers_before = modSettings.native_reader_stats()
        dialog = QProgressDialog(title, "Cancel", 0, 0, self._mo2_main_window())
        dialog.setWindowTitle(title)
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setValue(0)
        QApplication.processEvents()

        def report(done, total, label):
            if total:
                dialog.setMaximum(total)
                dialog.setValue(min(done, total))
            dialog.setLabelText(f"{title}\n{label}")
            QApplication.processEvents()
            return not dialog.wasCanceled()

        try:
            result = callback(report)
        except Exception as e:
            dialog.close()
            self.log(f"{title}: failed: {e}")
            QMessageBox.critical(self._mo2_main_window(), title, str(e))
            return

        dialog.close()
        self.invalidate_pak_cache()
        self.notify_mods_changed(changed_mods or [])
        self._update_quick_options_selected_counts()
        message = success_message(result)
        if result.get("cancelled"):
            message = f"Cancelled. {message}"
        self.log(f"{title}: {message}")
        self.log_reader_activity(title, readers_before)
        QMessageBox.information(self._mo2_main_window(), title, message)

    def _pak_metadata_action(self, regenerate, selected):
        title = f"{'Regenerate' if regenerate else 'Clear'} {'selected' if selected else 'all'} paks metadata"
        mods_noun = "selected mod(s)" if selected else "mod(s)"

        if selected:
            mod_names = self._selected_pak_mod_names()
            minimum = 1
            empty = "No selected mods with pak files were found."
        elif regenerate:
            mod_names = self._all_pak_mod_names()
            minimum = 1
            empty = "No installed mods with pak files were found."
        else:
            mod_names = list(self._organizer.modList().allMods())
            minimum = 1
            empty = "No installed mods were found."

        if len(mod_names) < minimum:
            self.log(f"{title}: {empty[0].lower()}{empty[1:]}")
            QMessageBox.information(self._mo2_main_window(), "BG3", empty)
            return

        if regenerate:
            question = (
                f"Regenerate pak metadata for {len(mod_names)} {mods_noun}?\n\n"
                "This may take a while on large modlists."
            )
        else:
            question = (
                f"Remove pak metadata sections from {len(mod_names)} {mods_noun}?\n\n"
                "Other meta.ini information will be kept."
            )

        if not self._confirm_quick_action(title, question):
            self.log(f"{title}: cancelled.")
            return

        names = mod_names if (regenerate or selected) else None
        if regenerate:
            self._run_pak_metadata_action(
                title,
                lambda report: modSettings.regenerate_pak_metadata(
                    self._organizer,
                    self._organizer.modList(),
                    self._organizer.profile(),
                    names,
                    progress=report,
                ),
                lambda result: f"Regenerated pak metadata for {result['paks']} pak(s) in {result['mods']} {mods_noun}.",
                changed_mods=mod_names,
            )
        else:
            self._run_pak_metadata_action(
                title,
                lambda report: modSettings.clear_pak_metadata(
                    self._organizer.modList(), names, progress=report
                ),
                lambda result: f"Removed {result['sections']} pak metadata section(s) from {result['mods']} {mods_noun}.",
                changed_mods=mod_names,
            )

    def _manual_mods_directory(self):
        return self._appdata_directory() / "Mods"

    def _import_manual_mods(self):
        title = "Import manually installed mods"
        mods_dir = self._manual_mods_directory()
        if mods_dir is None or not mods_dir.is_dir():
            self.log(f"{title}: the game's Mods folder was not found.")
            QMessageBox.information(
                self._mo2_main_window(), title, "The game's Mods folder was not found."
            )
            return

        candidates = modSettings.find_manual_mods(mods_dir, self._organizer.modList())
        if not candidates:
            self.log(f"{title}: nothing to import.")
            QMessageBox.information(
                self._mo2_main_window(),
                title,
                f"No unmanaged .pak files were found in\n{mods_dir}",
            )
            return

        total_mb = sum(p.stat().st_size for p in candidates) / (1024 * 1024)
        preview = "\n".join(f"  {p.name}" for p in candidates[:8])
        if len(candidates) > 8:
            preview += f"\n  ... and {len(candidates) - 8} more"

        if not self._confirm_quick_action(
            title,
            f"Move {len(candidates)} pak(s) ({total_mb:.0f} MB) out of\n{mods_dir}\n"
            f"into new mods in this MO2 instance, and enable them?\n\n{preview}\n\n"
            "The files are MOVED, not copied. Close the game first.",
        ):
            self.log(f"{title}: cancelled.")
            return

        self._run_pak_metadata_action(
            title,
            lambda report: modSettings.import_manual_mods(
                self._organizer,
                self._organizer.modList(),
                self._organizer.profile(),
                mods_dir,
                progress=report,
            ),
            lambda result: (
                f"Imported {result['imported']} of {result['found']} pak(s)"
                + (f", skipped {result['skipped']}." if result["skipped"] else ".")
            ),
        )
        self._organizer.refresh()

    def log_reader_activity(self, title, before):
        after = modSettings.native_reader_stats()
        scanned = (after.get("native", 0) - before.get("native", 0)) + (
            after.get("divine", 0) - before.get("divine", 0)
        )
        unreadable = modSettings.take_unreadable_paks()
        without_lslib = modSettings.take_fallback_reasons()

        if scanned:
            paks = "pak" if scanned == 1 else "paks"
            self.log(f"{title}: updated {scanned} {paks}.")

        self._log_pak_names(title, unreadable, "couldn't be read")
        self._log_pak_names(title, without_lslib, "was read without LSLib", "were read without LSLib")
        if unreadable or without_lslib:
            self._refresh_divine_status()

    def _log_pak_names(self, title, pak_names, message, plural_message=None):
        for pak_name in pak_names[:3]:
            self.log(f"{title}: {pak_name} {message}.")
        if len(pak_names) > 3:
            self.log(f"{title}: {len(pak_names) - 3} more {plural_message or message}.")

    def log_modsettings_result(self):
        try:
            summary = modSettings.mod_settings_summary(self._organizer.profile())
        except Exception as e:
            qDebug(f"BG3: could not summarise modsettings.lsx: {e}")
            return
        if summary is None:
            return
        modules, order = summary
        overrides = modSettings.last_override_skip()
        message = f"modsettings.lsx written: {modules} mod(s), {order} in load order."
        if overrides:
            message += f" {overrides} pure override mod(s) are active but not listed."
        self.log(message)

    def report_modsettings_failure(self, reason):
        message = (
            f"modsettings.lsx could not be updated:\n\n{reason}\n\n"
            "The game will start with the previous load order. If bg3.exe or "
            "bg3_dx11.exe is still running from a crash, close it and launch again."
        )
        qDebug(f"BG3: {message}")
        self.log(f"modsettings.lsx generation failed: {reason}")
        QMessageBox.warning(self._mo2_main_window(), "Baldur's Gate 3", message)
