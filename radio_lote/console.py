"""Public CLI for the same local services used by the terminal application."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import logging
import os
from pathlib import Path
import sys

from . import __version__, core
from .catalog import Catalog
from .operations import Operations, StopRequested
from .platforms import default_data_dir
from .service import OperationFailed
from .storage import Attempt, read_json


def make_parser():
    parser = argparse.ArgumentParser(prog="radio-lote", description="Local radio batch manager / Gestão local de lotes de rádios")
    parser.add_argument("--version", action="version", version=f"radio-lote {__version__}")
    parser.add_argument("--dados", "--data", type=Path, default=default_data_dir())
    parser.add_argument("--lang", choices=("pt", "en"))
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("ui", help="Open interactive terminal / Abrir painel")
    commands.add_parser("doctor", help="Check versions, paths and USB ports without opening them")
    migrate = commands.add_parser("migrate", help="Copy and validate legacy data; preserve the source")
    migrate.add_argument("source", type=Path)
    group = commands.add_parser("batch", help="Manage batches / Gerenciar lotes").add_subparsers(dest="action", required=True)
    listing = group.add_parser("list")
    listing.add_argument("--all", action="store_true", help="Include archived batches")
    create = group.add_parser("create")
    create.add_argument("name")
    create.add_argument("--client", default="")
    create.add_argument("--quantity", type=int)
    create.add_argument("--profile")
    create.add_argument("--groups", type=int, default=4)
    create.add_argument("--existing-team", action="store_true")
    for action in ("show", "archive", "reopen", "complete"):
        group.add_parser(action).add_argument("batch_id")
    duplicate = group.add_parser("duplicate")
    duplicate.add_argument("batch_id")
    duplicate.add_argument("name")
    export = group.add_parser("export")
    export.add_argument("batch_id")
    export.add_argument("destination", type=Path)
    group.add_parser("import").add_argument("source", type=Path)
    profiles = commands.add_parser("profile", help="Saved configurations / Perfis").add_subparsers(dest="action", required=True)
    profiles.add_parser("list")
    profiles.add_parser("show").add_argument("profile_id")
    imp = profiles.add_parser("import")
    imp.add_argument("source", type=Path, help="Compatible CHIRP .img or profile JSON")
    imp.add_argument("--name", required=True)
    imp.add_argument("--groups", type=int, default=4)
    imp.add_argument("--existing-team", action="store_true")
    imp.add_argument("--batch", help="Activate on a draft batch")
    activate = profiles.add_parser("activate")
    activate.add_argument("profile_id")
    activate.add_argument("--batch", required=True)
    capture = profiles.add_parser("capture")
    capture.add_argument("--batch", required=True)
    capture.add_argument("--name", required=True)
    capture.add_argument("--groups", type=int, default=4)
    capture.add_argument("--existing-team", action="store_true")
    capture.add_argument("--radio", default="R01")
    capture.add_argument("--port")
    for name in ("program", "backup", "verify", "restore"):
        command = commands.add_parser(name)
        command.add_argument("--batch", required=True)
        command.add_argument("--radio", required=name != "program")
        command.add_argument("--port")
        if name in ("verify", "restore"):
            command.add_argument("--attempt", required=True)
        if name == "program":
            command.add_argument("--once", action="store_true")
        if name == "restore":
            command.add_argument("--confirm-label", help="Noninteractive physical label confirmation")
    status = commands.add_parser("status")
    status.add_argument("--batch")
    status.add_argument("--cliente", help=argparse.SUPPRESS)
    status.add_argument("--historico", action="store_true", help=argparse.SUPPRESS)
    logs = commands.add_parser("logs")
    logs.add_argument("--batch", required=True)
    logs.add_argument("--limit", type=int, default=100)
    voice = commands.add_parser("voice")
    voice.add_argument("--batch", required=True)
    voice.add_argument("--radio", required=True)
    voice.add_argument("--result", choices=("passed", "failed"), required=True)
    return parser


def _display(value, args):
    # JSON is also the plain/no-color fallback, keeping CLI output useful in
    # scripts and terminals that cannot render the full-screen application.
    if args.json or not sys.stdout.isatty() or not isinstance(value, list) or not value:
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
        return
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text
    table = Table(show_header=True)
    keys = [key for key in ("id", "name", "client", "radio_id", "status", "voice_test", "revision") if key in value[0]]
    if not keys:
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
        return
    for key in keys:
        table.add_column(key)
    for row in value:
        table.add_row(*(Text(str(row.get(key, ""))) for key in keys))
    Console(no_color=args.no_color or "NO_COLOR" in os.environ).print(table)


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError as exc:
        raise StopRequested("Input closed / Entrada encerrada.") from exc


def _program_loop(catalog, operations, args, language):
    pt = language == "pt"
    label = args.radio
    while True:
        batch = catalog.get_batch(args.batch)
        label = label or batch["next_radio"]
        if label is None:
            return catalog.list_radios(args.batch)
        if not args.radio or not args.once:
            prompt = (f"Conecte e ligue {label}. Enter = iniciar/repetir; q = encerrar: " if pt else
                      f"Connect and power on {label}. Enter = start/retry; q = quit: ")
            if _ask(prompt).lower() == "q":
                return catalog.list_radios(args.batch)
        operations.reset_stop()
        try:
            attempt = operations.program(args.batch, label, args.port)
            print(f"{label}: " + ("gravado e verificado" if attempt.record['kind'] == 'programacao' and pt else
                                 "programmed and verified" if attempt.record['kind'] == 'programacao' else
                                 "restauração verificada / restoration verified"), file=sys.stderr)
            if args.once:
                return {"attempt": attempt.path.name, "radio": label, "status": attempt.record["status"]}
            label = None
        except (core.RadioLoteError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            if args.once or not sys.stdin.isatty():
                raise
            print("Mesmo rádio; uma escrita pendente será apenas conferida." if pt else
                  "Keep the same radio; a pending write will only be verified.")


def execute(args):
    catalog = Catalog(args.dados)
    language = args.lang or catalog.settings().get("lang", "pt")
    if args.command is None or args.command == "ui":
        if not sys.stdin.isatty():
            make_parser().print_help()
            return None
        if args.no_color:
            os.environ["NO_COLOR"] = "1"
        from .tui import run_ui
        run_ui(catalog.root, lang=language if args.lang else None)
        return None
    # Progress goes to stderr, preserving structured command output on stdout.
    def progress(event):
        if event.get("current", 0) == 0 or event.get("current") == 100:
            print(f"[{event['stage']}] {event['message']}", file=sys.stderr)
    operations = Operations(catalog, on_event=progress)
    if args.command == "doctor":
        core._check_installed_revision()
        return {"version": __version__, "chirp_revision": core.CHIRP_REVISION,
                "data_directory": str(catalog.root), "data_inside_environment": catalog.root.is_relative_to(Path(sys.prefix)),
                "ports": core.list_usb_ports(), "model": core.DRIVER, "hardware_validation": {"macOS": "validated", "Windows": "experimental", "Linux": "experimental"}}
    if args.command == "migrate":
        return catalog.migrate_legacy(args.source)
    if args.command == "batch":
        action = args.action
        if action == "list": return catalog.list_batches(include_archived=args.all)
        if action == "create": return catalog.create_batch(args.name, args.client, args.quantity, args.profile, args.groups, not args.existing_team)
        if action == "show": return {**catalog.get_batch(args.batch_id), "radios": catalog.list_radios(args.batch_id)}
        if action == "archive": return catalog.archive_batch(args.batch_id)
        if action == "reopen": return catalog.archive_batch(args.batch_id, False)
        if action == "complete": return catalog.complete_batch(args.batch_id)
        if action == "duplicate": return catalog.duplicate_batch(args.batch_id, args.name)
        if action == "export": return {"file": str(catalog.export_batch(args.batch_id, args.destination))}
        if action == "import": return catalog.import_batch(args.source)
    if args.command == "profile":
        if args.action == "list": return catalog.list_profiles()
        if args.action == "show": return catalog.get_profile(args.profile_id)
        if args.action == "activate": return catalog.activate_batch(args.batch, args.profile_id)
        if args.action == "capture":
            return operations.capture_profile(args.batch, args.name, args.groups, not args.existing_team, args.port, args.radio)
        if args.action == "import":
            if args.source.suffix.lower() == ".img":
                config = core.make_profile(core.load_image(args.source), count=args.groups, new_groups=not args.existing_team)
            else:
                data = read_json(args.source)
                config = data.get("config", data)
            profile = catalog.save_profile(args.name, config, source={"kind": "file", "filename": args.source.name})
            if args.batch:
                catalog.activate_batch(args.batch, profile["id"])
            return profile
    if args.command == "program":
        return _program_loop(catalog, operations, args, language)
    if args.command in ("backup", "verify", "restore"):
        if args.command == "backup":
            attempt = operations.backup(args.batch, args.radio, args.port)
        elif args.command == "verify":
            attempt = operations.verify(args.batch, args.attempt, args.radio, args.port)
        else:
            source = Attempt.load(catalog.attempt_path(args.batch, args.attempt))
            operations.programmer._check_physical_label(source, args.radio)
            summary = core.summarize(source.read_raw("original"))
            print(json.dumps(summary, ensure_ascii=False, indent=2), file=sys.stderr)
            confirmation = args.confirm_label
            if confirmation is None:
                confirmation = _ask(f"Restaurar / Restore {args.radio}: digite a etiqueta / type the label: ")
            if confirmation != args.radio:
                raise core.RadioLoteError("Restauração cancelada: etiqueta não confirmada.")
            attempt = operations.restore(args.batch, args.attempt, args.radio, args.port)
        return {"attempt": attempt.path.name, "radio": args.radio, "status": attempt.record["status"]}
    if args.command == "status":
        if args.batch:
            return {**catalog.get_batch(args.batch), "radios": catalog.list_radios(args.batch)}
        return catalog.list_batches(include_archived=True)
    if args.command == "logs": return catalog.logs(args.batch)[-max(1, args.limit):]
    if args.command == "voice":
        catalog.set_voice(args.batch, args.radio, args.result == "passed")
        return {"radio": args.radio, "voice_test": args.result}
    raise core.RadioLoteError("Comando não reconhecido.")


def main(argv=None):
    args = make_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    try:
        value = execute(args)
        if value is not None:
            _display(value, args)
        return 0
    except (core.RadioLoteError, OSError, ValueError) as exc:
        print(f"Error / Erro: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrompido / Interrupted. A pending write must be verified before retrying.", file=sys.stderr)
        return 130
