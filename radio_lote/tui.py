"""Keyboard-first Textual interface. Hardware work stays in the service layer."""

from __future__ import annotations

import copy
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
from typing import Callable

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.widgets import (
    Button, Checkbox, DataTable, Footer, Header, Input, Label, ProgressBar,
    RichLog, Select, Static, TabbedContent, TabPane,
)

from . import core
from .i18n import normalize_language, translate


def plain(value) -> str:
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(value or ""))


def frequency_hz(value: str) -> int:
    try:
        amount = Decimal(value.strip().replace(",", ".")) * 1_000_000
        if not amount.is_finite() or amount != amount.to_integral_value():
            raise ValueError
        return int(amount)
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise core.RadioLoteError("Invalid exact MHz frequency / Frequência em MHz inválida.") from exc


class BaseScreen(Screen):
    BINDINGS = [Binding("escape", "back", "Voltar / Back")]

    def t(self, key, **values):
        return self.app.t(key, **values)

    def error(self, error):
        self.app.notify(plain(error), title=self.t("error"), severity="error", timeout=10)

    def action_back(self):
        if self.app.operation_busy:
            self.app.notify(self.t("busy"))
            return
        # Textual keeps its own empty default screen below our Home screen.
        if len(self.app.screen_stack) > 2:
            self.app.pop_screen()


class PromptScreen(ModalScreen[str | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, title, hint, value="", expected=None):
        super().__init__()
        self.prompt_title, self.hint, self.value, self.expected = title, hint, value, expected

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-box"):
            yield Label(self.prompt_title, classes="screen-title")
            yield Static(self.hint, markup=False, classes="hint")
            yield Input(value=self.value, id="prompt-value")
            yield Static("", id="prompt-error", classes="error-text", markup=False)
            with Horizontal(classes="buttons"):
                yield Button(self.app.t("confirm"), id="prompt-confirm", variant="primary")
                yield Button(self.app.t("cancel"), id="prompt-cancel")

    def on_mount(self):
        self.query_one("#prompt-value", Input).focus()

    @on(Input.Submitted, "#prompt-value")
    @on(Button.Pressed, "#prompt-confirm")
    def confirm(self):
        value = self.query_one("#prompt-value", Input).value.strip()
        if not value or (self.expected is not None and value != self.expected):
            self.query_one("#prompt-error", Static).update(self.app.t("label_mismatch") if self.expected else self.app.t("path"))
            return
        self.dismiss(value)

    @on(Button.Pressed, "#prompt-cancel")
    def action_cancel(self):
        self.dismiss(None)


class TextScreen(BaseScreen):
    def __init__(self, title: str, text: str):
        super().__init__()
        self.heading, self.text = title, text

    def compose(self):
        yield Header()
        with VerticalScroll(classes="page"):
            yield Label(self.heading, classes="screen-title")
            yield Static(plain(self.text), markup=False, id="text-content")
            yield Button(self.t("back"), id="back")
        yield Footer()

    @on(Button.Pressed, "#back")
    def back(self):
        self.action_back()


class LanguageScreen(ModalScreen[str]):
    def compose(self):
        with Vertical(id="language-box"):
            yield Label("Radio Lote", classes="screen-title")
            yield Static("Escolha o idioma / Choose your language", markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Português", id="language-pt", variant="primary")
                yield Button("English", id="language-en")

    @on(Button.Pressed)
    def choose(self, event):
        if event.button.id.startswith("language-"):
            self.dismiss(event.button.id[-2:])


class SettingsScreen(BaseScreen):
    def compose(self):
        settings = self.app.catalog.settings()
        yield Header()
        with VerticalScroll(classes="form-page"):
            yield Label(self.t("settings_title"), classes="screen-title")
            yield Label(self.t("language"))
            yield Select([("Português", "pt"), ("English", "en")], value=self.app.language, allow_blank=False, id="language")
            yield Label(self.t("theme"))
            yield Select([(self.t("dark"), "dark"), (self.t("light"), "light")],
                         value="light" if settings.get("theme") == "light" else "dark", allow_blank=False, id="theme-choice")
            yield Checkbox(self.t("no_color"), value=bool(settings.get("no_color", False) or "NO_COLOR" in os.environ), id="no-color")
            with Horizontal(classes="buttons"):
                yield Button(self.t("save"), id="save-settings", variant="primary")
                yield Button(self.t("back"), id="back")
        yield Footer()

    @on(Button.Pressed, "#save-settings")
    def save(self):
        try:
            language = str(self.query_one("#language", Select).value)
            theme = str(self.query_one("#theme-choice", Select).value)
            no_color = self.query_one("#no-color", Checkbox).value
            self.app.catalog.save_settings(lang=language, theme=theme, no_color=no_color)
            self.app.language = normalize_language(language)
            self.app.apply_appearance(theme, no_color)
            self.app.pop_screen()
            self.app.switch_screen(HomeScreen())
            self.app.notify(self.app.t("settings_saved"))
        except Exception as exc:
            self.error(exc)

    @on(Button.Pressed, "#back")
    def back(self):
        self.action_back()


class HomeScreen(BaseScreen):
    BINDINGS = [Binding("n", "new", "Novo / New"), Binding("r", "refresh", "Atualizar / Refresh")]

    def __init__(self):
        super().__init__()
        self.batch_rows = {}

    def compose(self):
        yield Header()
        with Vertical(id="home-content", classes="page"):
            with Horizontal(classes="title-row"):
                yield Label(self.t("batches"), classes="screen-title")
                yield Checkbox(self.t("all_batches"), id="show-archived")
            yield Input(placeholder=self.t("search"), id="batch-search")
            yield DataTable(id="batch-table", cursor_type="row", zebra_stripes=True)
            yield Static("", id="home-summary", markup=False, classes="hint")
            with HorizontalScroll(classes="toolbar"):
                for key, variant in (("new_batch", "primary"), ("open", "default"), ("duplicate", "default"),
                                     ("archive", "default"), ("export", "default"), ("import", "default"),
                                     ("migrate", "default"), ("doctor", "default"), ("settings", "default")):
                    yield Button(self.t(key), id=f"home-{key}", variant=variant)
        yield Footer()

    def on_mount(self):
        table = self.query_one("#batch-table", DataTable)
        table.add_columns(self.t("name"), self.t("client"), self.t("status"), self.t("verified"), self.t("voice"))
        self.refresh_batches()
        table.focus()

    def on_screen_resume(self):
        if self.is_mounted:
            self.refresh_batches()

    def refresh_batches(self):
        try:
            needle = self.query_one("#batch-search", Input).value.casefold()
            rows = self.app.catalog.list_batches(include_archived=self.query_one("#show-archived", Checkbox).value)
            table = self.query_one("#batch-table", DataTable)
            table.clear()
            self.batch_rows = {}
            for batch in rows:
                if needle and needle not in f"{batch['name']} {batch.get('client', '')}".casefold():
                    continue
                counts = batch.get("counts", {})
                quantity = batch.get("quantity")
                total = f"{counts.get('verified', 0)}/{quantity}" if quantity else str(counts.get("verified", 0))
                table.add_row(plain(batch["name"]), plain(batch.get("client", "")), self.t(batch.get("status", "draft")),
                              total, str(counts.get("voice", 0)), key=batch["id"])
                self.batch_rows[batch["id"]] = batch
            self.query_one("#home-summary", Static).update(self.t("no_batches") if not self.batch_rows else f"{len(self.batch_rows)} {self.t('batches').lower()}")
        except Exception as exc:
            self.error(exc)

    @on(Input.Changed, "#batch-search")
    @on(Checkbox.Changed, "#show-archived")
    def filter_batches(self):
        if self.is_mounted:
            self.refresh_batches()

    def selected_batch(self):
        table = self.query_one("#batch-table", DataTable)
        if not table.row_count:
            raise core.RadioLoteError(self.t("no_selected"))
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return self.batch_rows[str(row_key.value)]

    @on(DataTable.RowSelected, "#batch-table")
    def open_row(self, event):
        self.app.push_screen(BatchScreen(str(event.row_key.value)))

    def action_new(self):
        self.app.push_screen(NewBatchScreen())

    def action_refresh(self):
        self.refresh_batches()

    @on(Button.Pressed)
    def button(self, event):
        action = event.button.id or ""
        try:
            if action == "home-new_batch":
                self.action_new()
            elif action == "home-open":
                self.app.push_screen(BatchScreen(self.selected_batch()["id"]))
            elif action == "home-duplicate":
                batch = self.selected_batch()
                def duplicate(name):
                    if name:
                        self.safe_catalog(lambda: self.app.catalog.duplicate_batch(batch["id"], name))
                self.app.push_screen(PromptScreen(self.t("duplicate"), self.t("rename_copy"), batch["name"] + " (2)"), duplicate)
            elif action == "home-archive":
                batch = self.selected_batch()
                self.app.catalog.archive_batch(batch["id"], archived=batch.get("status") != "archived")
                self.refresh_batches()
            elif action == "home-export":
                batch = self.selected_batch()
                def export(path):
                    if path:
                        try:
                            output = self.app.catalog.export_batch(batch["id"], Path(path).expanduser())
                            self.app.notify(self.t("export_ok", path=output))
                        except Exception as exc:
                            self.error(exc)
                self.app.push_screen(PromptScreen(self.t("export"), self.t("zip_hint"), str(Path.home() / f"radio-lote-{batch['id'][:8]}.zip")), export)
            elif action in ("home-import", "home-migrate"):
                migration = action == "home-migrate"
                def import_data(path):
                    if path:
                        call = self.app.catalog.migrate_legacy if migration else self.app.catalog.import_batch
                        self.safe_catalog(lambda: call(Path(path).expanduser()))
                self.app.push_screen(PromptScreen(self.t("migrate" if migration else "import"),
                                                  self.t("migration_hint" if migration else "path")), import_data)
            elif action == "home-doctor":
                self.app.push_screen(ConnectionScreen())
            elif action == "home-settings":
                self.app.push_screen(SettingsScreen())
        except Exception as exc:
            self.error(exc)

    def safe_catalog(self, action):
        try:
            action()
            self.refresh_batches()
        except Exception as exc:
            self.error(exc)


class ConnectionScreen(BaseScreen):
    def compose(self):
        yield Header()
        with VerticalScroll(classes="page"):
            yield Label(self.t("connection_title"), classes="screen-title")
            yield Static(self.t("port_hint"), classes="hint", markup=False)
            yield Static("", id="port-list", markup=False)
            with Horizontal(classes="buttons"):
                yield Button(self.t("refresh"), id="ports-refresh", variant="primary")
                yield Button(self.t("back"), id="back")
        yield Footer()

    def on_mount(self):
        self.refresh_ports()

    @on(Button.Pressed, "#ports-refresh")
    def refresh_ports(self):
        try:
            ports = core.list_usb_ports()
            self.query_one("#port-list", Static).update("\n\n".join(
                f"{plain(port['device'])}\n{plain(port.get('description', ''))}\n{plain(port.get('hwid', ''))}"
                for port in ports) if ports else self.t("no_ports"))
        except Exception as exc:
            self.error(exc)

    @on(Button.Pressed, "#back")
    def back(self):
        self.action_back()


class NewBatchScreen(BaseScreen):
    def compose(self):
        profiles = self.app.catalog.list_profiles()
        yield Header()
        with VerticalScroll(classes="form-page"):
            yield Label(self.t("new_batch"), classes="screen-title")
            yield Label(self.t("name"))
            yield Input(id="new-name")
            yield Label(self.t("client"))
            yield Input(id="new-client")
            with Horizontal(classes="field-row"):
                with Vertical(classes="field"):
                    yield Label(self.t("quantity"))
                    yield Input(id="new-quantity", type="integer")
                with Vertical(classes="field"):
                    yield Label(self.t("groups"))
                    yield Input("4", id="new-groups", type="integer")
            yield Label(self.t("goal"))
            yield Select([(self.t("new_groups"), "new"), (self.t("existing_groups"), "existing")],
                         value="new", allow_blank=False, id="new-goal")
            yield Label(self.t("source"))
            yield Select([(self.t("source_radio"), "radio"), (self.t("source_image"), "image"),
                          (self.t("source_saved"), "saved"), (self.t("source_draft"), "draft")],
                         value="radio", allow_blank=False, id="new-source")
            yield Label(self.t("image_path"), id="image-label")
            yield Input(id="new-image")
            yield Label(self.t("saved_profile"), id="saved-label")
            yield Select([(f"{p['name']} · v{p.get('revision', 1)}", p["id"]) for p in profiles], id="new-profile")
            yield Static(self.t("guided_hint"), classes="hint", markup=False)
            yield Static("", id="new-error", classes="error-text", markup=False)
            with Horizontal(classes="buttons"):
                yield Button(self.t("create"), id="new-create", variant="primary")
                yield Button(self.t("cancel"), id="new-cancel")
        yield Footer()

    def on_mount(self):
        self.source_changed()
        self.query_one("#new-name", Input).focus()

    @on(Select.Changed, "#new-source")
    def source_changed(self):
        if not self.is_mounted:
            return
        source = self.query_one("#new-source", Select).value
        for selector in ("#new-image", "#image-label"):
            self.query_one(selector).display = source == "image"
        for selector in ("#new-profile", "#saved-label"):
            self.query_one(selector).display = source == "saved"

    @on(Button.Pressed, "#new-create")
    def create(self):
        try:
            name = self.query_one("#new-name", Input).value.strip()
            if not name:
                raise core.RadioLoteError(self.t("name"))
            client = self.query_one("#new-client", Input).value.strip()
            quantity_text = self.query_one("#new-quantity", Input).value.strip()
            quantity = int(quantity_text) if quantity_text else None
            if quantity is not None and quantity < 1:
                raise core.RadioLoteError(self.t("quantity"))
            count = int(self.query_one("#new-groups", Input).value)
            if not 1 <= count <= 16:
                raise core.RadioLoteError(self.t("groups"))
            new_groups = self.query_one("#new-goal", Select).value == "new"
            source = self.query_one("#new-source", Select).value
            profile_id = None
            if source == "saved":
                value = self.query_one("#new-profile", Select).value
                if value is Select.BLANK:
                    raise core.RadioLoteError(self.t("profile_required"))
                profile_id = str(value)
            elif source == "image":
                path_text = self.query_one("#new-image", Input).value.strip()
                if not path_text:
                    raise core.RadioLoteError(self.t("image_path"))
                raw = core.load_image(Path(path_text).expanduser())
                config = core.make_profile(raw, client=client or "default", count=count, new_groups=new_groups)
                profile = self.app.catalog.save_profile(name, config, source={"kind": "image"})
                profile_id = profile["id"]
            batch = self.app.catalog.create_batch(name, client=client, quantity=quantity,
                                                   profile_id=profile_id, group_count=count, new_groups=new_groups)
            self.app.pop_screen()
            self.app.push_screen(BatchScreen(batch["id"]))
            if not profile_id:
                self.app.notify(self.t("draft_created"), timeout=8)
        except Exception as exc:
            self.query_one("#new-error", Static).update(plain(exc))

    @on(Button.Pressed, "#new-cancel")
    def cancel(self):
        self.action_back()


class ProfileChooser(ModalScreen[str | None]):
    def compose(self):
        with Vertical(id="prompt-box"):
            yield Label(self.app.t("choose_profile"), classes="screen-title")
            yield Select([(f"{p['name']} · v{p.get('revision', 1)}", p["id"])
                          for p in self.app.catalog.list_profiles()], id="profile-choice")
            with Horizontal(classes="buttons"):
                yield Button(self.app.t("confirm"), id="profile-confirm", variant="primary")
                yield Button(self.app.t("cancel"), id="profile-cancel")

    @on(Button.Pressed, "#profile-confirm")
    def confirm(self):
        value = self.query_one("#profile-choice", Select).value
        if value is not Select.BLANK:
            self.dismiss(str(value))

    @on(Button.Pressed, "#profile-cancel")
    def cancel(self):
        self.dismiss(None)


class BatchDetailsScreen(BaseScreen):
    def __init__(self, batch_id):
        super().__init__()
        self.batch_id = batch_id

    def compose(self):
        batch = self.app.catalog.get_batch(self.batch_id)
        yield Header()
        with VerticalScroll(classes="form-page"):
            yield Label(self.t("edit_batch"), classes="screen-title")
            yield Label(self.t("name"))
            yield Input(batch["name"], id="batch-edit-name")
            yield Label(self.t("client"))
            yield Input(batch.get("client", ""), id="batch-edit-client")
            yield Label(self.t("quantity"))
            yield Input(str(batch.get("quantity") or ""), type="integer", id="batch-edit-quantity")
            yield Static("", id="batch-edit-error", classes="error-text", markup=False)
            with Horizontal(classes="buttons"):
                yield Button(self.t("save"), id="batch-edit-save", variant="primary")
                yield Button(self.t("cancel"), id="batch-edit-cancel")
        yield Footer()

    @on(Button.Pressed, "#batch-edit-save")
    def save(self):
        try:
            quantity = self.query_one("#batch-edit-quantity", Input).value.strip()
            self.app.catalog.update_batch(self.batch_id,
                name=self.query_one("#batch-edit-name", Input).value.strip(),
                client=self.query_one("#batch-edit-client", Input).value.strip(),
                quantity=int(quantity) if quantity else None)
            self.app.notify(self.t("batch_saved"))
            self.action_back()
        except Exception as exc:
            self.query_one("#batch-edit-error", Static).update(plain(exc))

    @on(Button.Pressed, "#batch-edit-cancel")
    def cancel(self):
        self.action_back()


class BatchScreen(BaseScreen):
    BINDINGS = [Binding("escape", "back", "Voltar / Back"), Binding("f5", "program", "Programar / Program")]

    def __init__(self, batch_id: str):
        super().__init__()
        self.batch_id = batch_id
        self.batch = {}
        self.attempt_rows = {}
        self.radio_rows = {}
        self.operations = None
        self.active_label = None
        self.last_operation = None
        self.can_retry = False

    def compose(self):
        yield Header()
        with Vertical(classes="page", id="batch-content"):
            yield Static("", id="batch-title", classes="screen-title", markup=False)
            yield Static("", id="batch-summary", classes="hint", markup=False)
            with HorizontalScroll(classes="toolbar"):
                for key in ("back", "choose_profile", "advanced", "edit_batch", "complete", "doctor"):
                    yield Button(self.t(key), id=f"batch-{key}")
            with TabbedContent(id="batch-tabs"):
                with TabPane(self.t("radios"), id="radios-pane"):
                    yield DataTable(id="radio-table", cursor_type="row", zebra_stripes=True)
                    with Horizontal(classes="radio-labels"):
                        yield Label(self.t("connected_radio"), id="radio-label-heading")
                        yield Label(self.t("port"))
                    with Horizontal(classes="radio-fields"):
                        yield Input("R01", placeholder=self.t("radio_label"), id="radio-label")
                        yield Input(placeholder=self.t("port_auto"), id="radio-port")
                    yield Static(self.t("ready_hint"), id="operation-help", markup=False, classes="hint")
                    with HorizontalScroll(classes="toolbar"):
                        yield Button(self.t("program"), id="operation-program", variant="primary")
                        yield Button(self.t("retry"), id="operation-retry", disabled=True)
                        yield Button(self.t("backup"), id="operation-backup")
                        yield Button(self.t("capture"), id="operation-capture")
                        yield Button(self.t("pause"), id="operation-pause", disabled=True)
                        yield Button(self.t("voice_pass"), id="voice-pass")
                        yield Button(self.t("voice_fail"), id="voice-fail")
                with TabPane(self.t("backups"), id="attempts-pane"):
                    yield DataTable(id="attempt-table", cursor_type="row", zebra_stripes=True)
                    with HorizontalScroll(classes="toolbar"):
                        yield Button(self.t("preview"), id="attempt-preview")
                        yield Button(self.t("verify"), id="attempt-verify")
                        yield Button(self.t("restore"), id="attempt-restore", variant="warning")
                with TabPane(self.t("profile"), id="profile-pane"):
                    yield DataTable(id="profile-table", cursor_type="row", zebra_stripes=True)
                    yield Static(self.t("guided_hint"), markup=False, classes="hint")
                with TabPane(self.t("logs"), id="logs-pane"):
                    yield RichLog(id="batch-log", wrap=True, highlight=False, markup=False)
            yield Static("", id="operation-state", markup=False)
            yield ProgressBar(total=100, show_eta=False, id="operation-progress")
        yield Footer()

    def on_mount(self):
        self.operations = self.app.operations_factory(self.app.catalog, on_event=self.operation_event)
        self.query_one("#radio-table", DataTable).add_columns(self.t("radio"), self.t("status"), self.t("voice"))
        self.query_one("#attempt-table", DataTable).add_columns(self.t("radio"), self.t("kind"), self.t("status"), self.t("date"))
        self.query_one("#profile-table", DataTable).add_columns(self.t("channel"), "MHz", self.t("mode"), self.t("tone"), self.t("label"))
        self.refresh_batch()
        self.query_one("#radio-label", Input).focus()

    def on_screen_resume(self):
        if self.is_mounted:
            self.refresh_batch()

    def refresh_batch(self, *, advance=False):
        try:
            self.batch = self.app.catalog.get_batch(self.batch_id)
            counts = self.batch.get("counts", {})
            self.query_one("#batch-title", Static).update(f"{self.batch['name']}  ·  {self.t(self.batch.get('status', 'draft'))}")
            self.query_one("#batch-summary", Static).update(
                f"{self.t('client')}: {self.batch.get('client') or '—'}    "
                f"{self.t('verified')}: {counts.get('verified', 0)}    {self.t('voice')}: {counts.get('voice', 0)}    "
                f"{self.t('total')}: {self.batch.get('quantity') or '—'}")
            radios = self.app.catalog.list_radios(self.batch_id)
            table = self.query_one("#radio-table", DataTable)
            table.clear()
            self.radio_rows = {}
            for radio in radios:
                status = radio.get("status", "pending")
                table.add_row(radio["radio_id"], self.t("verificado" if status == "verified" else status),
                              self.t(radio.get("voice_test", "pending")), key=radio["radio_id"])
                self.radio_rows[radio["radio_id"]] = radio
            if advance or not self.active_label:
                self.query_one("#radio-label", Input).value = self.batch.get("next_radio") or ""
            if not self.batch.get("next_radio"):
                self.query_one("#operation-help", Static).update(self.t("quantity_reached"))
            attempts = self.app.catalog.list_attempts(self.batch_id)
            self.attempt_rows = {item["id"]: item for item in attempts}
            attempt_table = self.query_one("#attempt-table", DataTable)
            attempt_table.clear()
            for item in reversed(attempts):
                date = item.get("created_at", "")[:19].replace("T", " ")
                attempt_table.add_row(item["radio_id"], self.t(item["kind"]), self.t(item["status"]), date, key=item["id"])
            profile_table = self.query_one("#profile-table", DataTable)
            profile_table.clear()
            if self.batch.get("profile_id"):
                profile = self.app.catalog.get_profile(self.batch["profile_id"])
                config = core.normalize_profile(profile["config"])
                for channel in config["channels"]:
                    tone = channel.get("tone_mode", "")
                    detail = f"DCS {channel['dcs']:03d} {channel['polarity']}" if tone == "DTCS" else (
                        f"CTCSS {channel.get('ctcss')}" if tone == "TSQL" else self.t("no_tone"))
                    profile_table.add_row(str(channel["channel"]), f"{channel['rx_hz'] / 1_000_000:.6f}",
                                          channel["mode"], detail, plain(channel.get("label", "")))
            else:
                self.query_one("#operation-help", Static).update(self.t("no_profile"))
            log = self.query_one("#batch-log", RichLog)
            log.clear()
            for entry in self.app.catalog.logs(self.batch_id)[-200:]:
                log.write(plain(json.dumps(entry, ensure_ascii=False)))
        except Exception as exc:
            self.error(exc)

    @on(DataTable.RowSelected, "#radio-table")
    def choose_radio(self, event):
        if not self.app.operation_busy:
            self.query_one("#radio-label", Input).value = str(event.row_key.value)

    def radio_label(self):
        label = self.query_one("#radio-label", Input).value.strip().upper()
        if not re.fullmatch(r"R(?:0[1-9]|[1-9][0-9]{1,2})", label):
            raise core.RadioLoteError("Use R01…R999.")
        return label

    def port(self):
        return self.query_one("#radio-port", Input).value.strip() or None

    @on(Input.Submitted, "#radio-label")
    @on(Button.Pressed, "#operation-program")
    def action_program(self):
        if not self.batch.get("profile_id"):
            self.error(self.t("profile_required"))
            return
        self.start_operation("program")

    @on(Button.Pressed, "#operation-backup")
    def backup(self):
        self.start_operation("backup")

    @on(Button.Pressed, "#operation-capture")
    def capture(self):
        self.start_operation("capture")

    @on(Button.Pressed, "#operation-retry")
    def retry(self):
        if self.can_retry and self.last_operation:
            kind, attempt_id, radio_id = self.last_operation
            self.start_operation(kind, attempt_id=attempt_id, radio_id=radio_id)

    @on(Button.Pressed, "#operation-pause")
    def pause(self):
        if self.operations:
            self.operations.request_stop()
        self.query_one("#operation-state", Static).update(self.t("pause_requested"))

    def start_operation(self, kind, *, attempt_id=None, radio_id=None):
        if self.app.operation_busy:
            self.app.notify(self.t("busy"))
            return
        try:
            label, port = radio_id or self.radio_label(), self.port()
        except Exception as exc:
            self.error(exc)
            return
        self.active_label = label
        self.last_operation = (kind, attempt_id, label)
        self.can_retry = False
        self.app.operation_busy = True
        self.operations.reset_stop()
        self.set_busy(True)
        self.query_one("#operation-state", Static).update(f"{label}: {self.t('initializing')}")
        self.query_one("#operation-help", Static).update(self.t("working"))
        self.query_one("#operation-progress", ProgressBar).update(total=100, progress=0)

        def transfer():
            try:
                if kind == "capture":
                    value = self.operations.capture_profile(self.batch_id, self.batch["name"],
                        count=self.batch.get("group_count", 4), new_groups=self.batch.get("new_groups", True), port=port, radio_id=label)
                elif kind in ("verify", "restore"):
                    value = getattr(self.operations, kind)(self.batch_id, attempt_id, label, port=port)
                else:
                    value = getattr(self.operations, kind)(self.batch_id, label, port=port)
            except Exception as exc:
                self.app.call_from_thread(self.operation_finished, False, plain(exc), kind)
            else:
                self.app.call_from_thread(self.operation_finished, True, value, kind)

        self.run_worker(transfer, thread=True, group="serial", exit_on_error=False)

    def set_busy(self, value):
        for button in self.query(Button):
            button.disabled = value
        self.query_one("#operation-pause", Button).disabled = not value
        self.query_one("#operation-retry", Button).disabled = value or not self.can_retry
        self.query_one("#radio-label", Input).disabled = value
        self.query_one("#radio-port", Input).disabled = value

    def operation_event(self, event):
        self.app.call_from_thread(self.render_event, event)

    def render_event(self, event):
        stage = event.get("stage", "")
        translated_stage = self.t(stage)
        message = plain(translated_stage if translated_stage != stage else event.get("message") or stage)
        self.query_one("#operation-state", Static).update(message)
        total, current = event.get("total"), event.get("current")
        if total and current is not None:
            self.query_one("#operation-progress", ProgressBar).update(total=total, progress=current)
        self.query_one("#batch-log", RichLog).write(plain(event.get("message") or message))

    def operation_finished(self, succeeded, value, kind):
        self.app.operation_busy = False
        self.can_retry = not succeeded
        self.set_busy(False)
        self.refresh_batch(advance=succeeded and kind == "program")
        if succeeded:
            self.query_one("#operation-state", Static).update(f"{self.active_label}: {self.t('operation_ok')}")
            self.query_one("#operation-help", Static).update(self.t(
                "quantity_reached" if not self.batch.get("next_radio") else "done_hint"))
            self.query_one("#operation-progress", ProgressBar).update(total=100, progress=100)
        else:
            self.query_one("#operation-state", Static).update(plain(value))
            self.query_one("#operation-help", Static).update(self.t("operation_failed"))
            self.query_one("#radio-label", Input).value = self.active_label
        self.query_one("#radio-label", Input).focus()

    def selected_attempt(self):
        table = self.query_one("#attempt-table", DataTable)
        if not table.row_count:
            raise core.RadioLoteError(self.t("select_attempt"))
        key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return self.attempt_rows[str(key.value)]

    def backup_preview(self, attempt):
        from .storage import Attempt
        path = self.app.catalog.attempt_path(self.batch_id, attempt["id"])
        saved = Attempt.load(path)
        if "original" not in saved.record.get("artifacts", {}):
            raise core.RadioLoteError(self.t("empty_backup"))
        lines = [f"{self.t('radio')}: {attempt['radio_id']}", f"{self.t('attempt')}: {attempt['id']}", ""]
        for channel in core.summarize(saved.read_raw("original")):
            if channel.get("empty"):
                continue
            lines.append(f"{channel['channel']:>2}  RX {channel['rx_mhz']}  TX {channel['tx_mhz'] or 'OFF'}  {channel['mode']}  {channel['tone_mode']}  DCS {channel.get('dcs') or '—'}")
        return "\n".join(lines)

    @on(Button.Pressed)
    def buttons(self, event):
        action = event.button.id
        if self.app.operation_busy and action != "operation-pause":
            return
        try:
            if action == "batch-back":
                self.action_back()
            elif action == "batch-doctor":
                self.app.push_screen(ConnectionScreen())
            elif action == "batch-edit_batch":
                self.app.push_screen(BatchDetailsScreen(self.batch_id))
            elif action == "batch-complete":
                self.app.catalog.complete_batch(self.batch_id)
                self.refresh_batch()
            elif action == "batch-advanced":
                if not self.batch.get("profile_id"):
                    raise core.RadioLoteError(self.t("profile_required"))
                self.app.push_screen(ProfileEditorScreen(self.batch_id, self.batch["profile_id"]))
            elif action == "batch-choose_profile":
                def choose(profile_id):
                    if profile_id:
                        try:
                            self.app.catalog.activate_batch(self.batch_id, profile_id)
                            self.refresh_batch()
                        except Exception as exc:
                            self.error(exc)
                self.app.push_screen(ProfileChooser(), choose)
            elif action in ("voice-pass", "voice-fail"):
                self.app.catalog.set_voice(self.batch_id, self.radio_label(), passed=action == "voice-pass")
                self.refresh_batch()
                self.app.notify(self.t("voice_saved"))
            elif action == "attempt-preview":
                attempt = self.selected_attempt()
                self.app.push_screen(TextScreen(self.t("preview"), self.backup_preview(attempt)))
            elif action == "attempt-verify":
                attempt = self.selected_attempt()
                self.start_operation("verify", attempt_id=attempt["id"], radio_id=attempt["radio_id"])
            elif action == "attempt-restore":
                attempt = self.selected_attempt()
                preview = self.backup_preview(attempt)
                label = attempt["radio_id"]
                def restore(value):
                    if value == label:
                        self.start_operation("restore", attempt_id=attempt["id"], radio_id=label)
                self.app.push_screen(PromptScreen(self.t("restore_title"),
                    self.t("restore_hint") + "\n\n" + preview + "\n\n" + self.t("type_label", radio=label), expected=label), restore)
        except Exception as exc:
            self.error(exc)


class ProfileEditorScreen(BaseScreen):
    def __init__(self, batch_id: str, profile_id: str):
        super().__init__()
        self.batch_id, self.profile_id = batch_id, profile_id
        self.profile = None
        self.config = None

    def compose(self):
        self.profile = self.app.catalog.get_profile(self.profile_id)
        self.config = core.normalize_profile(self.profile["config"])
        yield Header()
        with VerticalScroll(classes="form-page", id="editor-page"):
            yield Label(self.t("advanced_title"), classes="screen-title")
            yield Static(self.t("advanced_hint"), classes="hint", markup=False)
            yield Static(self.t("apply_hint"), classes="hint", markup=False)
            yield DataTable(id="edit-table", cursor_type="row", zebra_stripes=True)
            with Horizontal(classes="field-row"):
                with Vertical(classes="field"):
                    yield Label(self.t("channel"))
                    yield Input("1", id="edit-number", type="integer")
                with Vertical(classes="field"):
                    yield Label(self.t("frequency"))
                    yield Input(id="edit-frequency")
            yield Label(self.t("label"))
            yield Input(id="edit-label")
            with Horizontal(classes="field-row"):
                with Vertical(classes="field"):
                    yield Label(self.t("mode"))
                    yield Select([("FM / 25 kHz", "FM"), ("NFM / 12.5 kHz", "NFM")], allow_blank=False, value="FM", id="edit-mode")
                with Vertical(classes="field"):
                    yield Label(self.t("tone"))
                    yield Select([(self.t("no_tone"), ""), ("CTCSS", "TSQL"), ("DCS", "DTCS")], allow_blank=False, value="DTCS", id="edit-tone")
            with Horizontal(classes="field-row"):
                with Vertical(classes="field"):
                    yield Label(self.t("dcs"))
                    yield Input("23", id="edit-dcs", type="integer")
                with Vertical(classes="field"):
                    yield Label(self.t("ctcss"))
                    yield Input("88.5", id="edit-ctcss")
                with Vertical(classes="field"):
                    yield Label(self.t("polarity"))
                    yield Select([(p, p) for p in ("NN", "NR", "RN", "RR")], allow_blank=False, value="NN", id="edit-polarity")
            yield Static("", id="edit-error", classes="error-text", markup=False)
            with HorizontalScroll(classes="toolbar"):
                yield Button(self.t("apply_channel"), id="edit-apply")
                yield Button(self.t("remove_channel"), id="edit-remove")
                yield Button(self.t("save_revision"), id="edit-save", variant="primary")
                yield Button(self.t("back"), id="edit-back")
        yield Footer()

    def on_mount(self):
        self.query_one("#edit-table", DataTable).add_columns(self.t("channel"), "MHz", self.t("mode"), self.t("tone"), self.t("label"))
        self.refresh_channels()
        if self.config["channels"]:
            self.load_channel(self.config["channels"][0]["channel"])

    def refresh_channels(self):
        table = self.query_one("#edit-table", DataTable)
        table.clear()
        for channel in sorted(self.config["channels"], key=lambda item: item["channel"]):
            tone = channel["tone_mode"]
            tone_text = f"DCS {channel['dcs']:03d}" if tone == "DTCS" else (f"CTCSS {channel['ctcss']}" if tone == "TSQL" else self.t("no_tone"))
            table.add_row(str(channel["channel"]), f"{channel['rx_hz'] / 1_000_000:.6f}", channel["mode"], tone_text,
                          plain(channel.get("label", "")), key=str(channel["channel"]))

    @on(DataTable.RowSelected, "#edit-table")
    def select_channel(self, event):
        self.load_channel(int(event.row_key.value))

    def load_channel(self, number):
        channel = next(row for row in self.config["channels"] if row["channel"] == number)
        self.query_one("#edit-number", Input).value = str(number)
        self.query_one("#edit-frequency", Input).value = f"{channel['rx_hz'] / 1_000_000:.6f}"
        self.query_one("#edit-label", Input).value = channel.get("label", "")
        self.query_one("#edit-mode", Select).value = channel["mode"]
        self.query_one("#edit-tone", Select).value = channel["tone_mode"]
        self.query_one("#edit-dcs", Input).value = str(channel.get("dcs", 23))
        self.query_one("#edit-ctcss", Input).value = str(channel.get("ctcss") or 88.5)
        self.query_one("#edit-polarity", Select).value = channel.get("polarity", "NN")

    @on(Button.Pressed, "#edit-apply")
    def apply_channel(self):
        try:
            number = int(self.query_one("#edit-number", Input).value)
            if not 1 <= number <= 16:
                raise core.RadioLoteError(self.t("invalid_channel"))
            hz = frequency_hz(self.query_one("#edit-frequency", Input).value)
            tone = str(self.query_one("#edit-tone", Select).value)
            row = {
                "channel": number, "rx_hz": hz, "tx_hz": hz,
                "mode": str(self.query_one("#edit-mode", Select).value), "tone_mode": tone,
                "dcs": int(self.query_one("#edit-dcs", Input).value or 23),
                "ctcss": float(self.query_one("#edit-ctcss", Input).value.replace(",", ".")) if tone == "TSQL" else None,
                "polarity": str(self.query_one("#edit-polarity", Select).value),
                "label": self.query_one("#edit-label", Input).value.strip(),
            }
            candidate = copy.deepcopy(self.config)
            candidate["channels"] = sorted([c for c in candidate["channels"] if c["channel"] != number] + [row], key=lambda c: c["channel"])
            core.validate_profile(candidate)
            self.config = candidate
            self.refresh_channels()
            self.query_one("#edit-error", Static).update("")
        except Exception as exc:
            self.query_one("#edit-error", Static).update(plain(exc))

    @on(Button.Pressed, "#edit-remove")
    def remove_channel(self):
        try:
            number = int(self.query_one("#edit-number", Input).value)
            candidate = copy.deepcopy(self.config)
            candidate["channels"] = [c for c in candidate["channels"] if c["channel"] != number]
            core.validate_profile(candidate)
            self.config = candidate
            self.refresh_channels()
            self.query_one("#edit-error", Static).update("")
        except Exception as exc:
            self.query_one("#edit-error", Static).update(plain(exc))

    @on(Button.Pressed, "#edit-save")
    def save_profile(self):
        try:
            core.validate_profile(self.config)
            saved = self.app.catalog.save_profile(self.profile["name"], self.config, source={"kind": "editor", "profile_id": self.profile_id})
            try:
                self.app.catalog.activate_batch(self.batch_id, saved["id"])
            except core.RadioLoteError:
                self.app.notify(self.t("profile_saved_future"), timeout=8)
            else:
                self.app.notify(self.t("profile_saved"))
            self.action_back()
        except Exception as exc:
            self.query_one("#edit-error", Static).update(plain(exc))

    @on(Button.Pressed, "#edit-back")
    def back(self):
        self.action_back()


class RadioLoteApp(App):
    TITLE = "Radio Lote"
    CSS_PATH = "ui.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding("ctrl+q", "quit", "Sair / Quit", priority=True),
                Binding("ctrl+c", "quit", show=False, priority=True),
                Binding("f1", "help", "Ajuda / Help")]

    def __init__(self, root: Path | None = None, lang: str | None = None, *, catalog=None, operations_factory=None):
        super().__init__()
        if catalog is None:
            from .catalog import Catalog
            catalog = Catalog(root)
        if operations_factory is None:
            from .operations import Operations
            operations_factory = Operations
        self.catalog, self.operations_factory = catalog, operations_factory
        preferences = self.catalog.settings()
        self.language = normalize_language(lang or preferences.get("lang") or preferences.get("language") or "pt")
        self.show_language_choice = lang is None and not (preferences.get("lang") or preferences.get("language"))
        self.operation_busy = False
        self.initial_appearance = (preferences.get("theme", "dark"), bool(preferences.get("no_color", False)))

    def t(self, key, **values):
        return translate(key, self.language, **values)

    def on_mount(self):
        self.apply_appearance(*self.initial_appearance)
        self.push_screen(HomeScreen())
        if self.show_language_choice:
            def chosen(language):
                self.language = normalize_language(language)
                self.catalog.save_settings(lang=self.language)
                self.switch_screen(HomeScreen())
            self.push_screen(LanguageScreen(), chosen)

    def apply_appearance(self, theme, no_color=False):
        monochrome = bool(no_color or "NO_COLOR" in os.environ)
        if monochrome:
            self.register_theme(Theme(name="radio-lote-mono", primary="#d8d8d8", secondary="#b0b0b0",
                accent="#ffffff", foreground="#ffffff", background="#000000", surface="#101010",
                panel="#181818", warning="#d8d8d8", error="#ffffff", success="#d8d8d8", dark=True))
            self.theme = "radio-lote-mono"
        else:
            self.theme = "textual-light" if theme == "light" else "textual-dark"
        self.set_class(monochrome, "no-color")

    def action_help(self):
        if not self.operation_busy:
            self.push_screen(TextScreen(self.t("help"), self.t("help_text")))

    def action_quit(self):
        if self.operation_busy:
            screen = self.screen
            if isinstance(screen, BatchScreen) and screen.operations:
                screen.operations.request_stop()
            self.notify(self.t("pause_requested"), timeout=8)
            return
        self.exit()


def run_ui(root: Path | None = None, lang: str | None = None):
    RadioLoteApp(root=root, lang=lang).run()
