"""Terminal em português para preparar, programar e conferir os aparelhos."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import shlex
import sys

from . import __version__, core
from .service import OperationFailed, Programmer
from .storage import Attempt, operation_lock, read_json
from .platforms import default_data_dir


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="radio-lote", description="Lote Baofeng 777/777S: backup, gravação e conferência.")
    result.add_argument("--version", action="version", version=f"radio-lote {__version__}")
    result.add_argument("--dados", type=Path, default=default_data_dir(),
                        help="Diretório de perfis, backups e registros.")
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("portas", help="Listar cabos USB seriais sem abrir conexão.")

    prepare = sub.add_parser("preparar", help="Ler o primeiro rádio e propor os quatro canais do cliente.")
    prepare.add_argument("--cliente", default="futebol")
    prepare.add_argument("--radio", default="R01")
    prepare.add_argument("--porta")
    prepare.add_argument("--somente-ler", action="store_true", help="Salvar a proposta sem ativar o perfil.")

    activate = sub.add_parser("ativar", help="Conferir e ativar uma proposta já lida do primeiro rádio.")
    activate.add_argument("--tentativa", type=Path, required=True)

    program = sub.add_parser("programar", help="Trocar o rádio e apertar Enter para cada ciclo do lote.")
    program.add_argument("--cliente", default="futebol")
    program.add_argument("--radio", help="Etiqueta inicial; por padrão sugere a primeira ainda não concluída.")
    program.add_argument("--porta")
    program.add_argument("--uma-vez", action="store_true", help="Encerrar após um aparelho.")

    for name, help_text in (("verificar", "Retomar somente a leitura de conferência, sem gravar."),
                            ("restaurar", "Restaurar o backup individual, guardando também o estado atual.")):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--tentativa", type=Path, required=True)
        command.add_argument("--radio", required=True, help="Etiqueta do aparelho conectado, por exemplo R01.")
        command.add_argument("--porta")

    simulate = sub.add_parser("simular", help="Preparar e conferir arquivos offline, sem abrir porta serial.")
    simulate.add_argument("--imagem", type=Path, required=True)
    simulate.add_argument("--cliente", default="teste")
    simulate.add_argument("--perfil", type=Path, help="Perfil JSON; padrão: derivar da imagem de teste.")

    status = sub.add_parser("status", help="Mostrar tentativas e resultados do lote.")
    status.add_argument("--cliente", default="futebol")
    status.add_argument("--historico", action="store_true", help="Incluir todas as tentativas anteriores.")

    voice = sub.add_parser("voz", help="Registrar o resultado do teste manual de voz nos quatro canais.")
    voice.add_argument("--tentativa", type=Path, required=True)
    voice.add_argument("--radio", required=True)
    voice.add_argument("--resultado", choices=("aprovado", "falhou"), required=True)
    return result


def ask(message: str) -> str:
    try:
        return input(message).strip()
    except EOFError as exc:
        raise core.RadioLoteError("Entrada encerrada; nenhuma nova gravação foi iniciada.") from exc


def print_profile(profile: dict) -> None:
    print(f"\nCliente: {profile['client']} | Baofeng 777/777S — perfil BF-888")
    print("Canal   Frequência RX/TX   Largura   DCS TX/RX   Polaridade")
    for channel in sorted(profile["channels"], key=lambda item: item["channel"]):
        print(f"{channel['channel']:>3}     {channel['rx_hz'] / 1_000_000:>10.6f} MHz"
              f"   {channel['mode']:<7}   {channel['dcs']:03d}/{channel['dcs']:03d}      {channel['polarity']}")
    print("Canais 5–16, potência e demais ajustes serão preservados em cada aparelho.\n")


def confirm_profile(programmer: Programmer, attempt: Attempt) -> bool:
    profile = read_json(attempt.path / "perfil-proposto.json")
    core.validate_profile(profile)
    print_profile(profile)
    if ask("Usar estas quatro frequências no lote deste cliente? [s/N] ").lower() not in ("s", "sim"):
        print(f"Proposta salva, sem ativar: {attempt.path}")
        print("Depois, use o comando ativar com --tentativa apontando para esse diretório.")
        return False
    destination = programmer.activate_profile(attempt)
    print(f"Perfil ativo: {destination}")
    print(f"O rádio {attempt.record['radio_id']} foi apenas lido. Agora execute programar.")
    return True


def success(attempt: Attempt) -> None:
    label = attempt.record["radio_id"]
    action = "Restaurado" if attempt.record["kind"] == "restauracao" else "Gravado"
    print(f"\n{label}: {action.lower()} e verificado.")
    print(f"Backup e conferência: {attempt.path}")
    print("Desligue antes de trocar o cabo. Ligue novamente para o teste de voz.")


def show_status(programmer: Programmer, client: str, *, history: bool = False) -> None:
    records = programmer.records(client)
    if not records:
        print(f"Nenhuma tentativa registrada para {client}.")
        return
    if not history:
        current = programmer._latest_physical_records(client)
        # Uma falha antes de gravar também precisa aparecer quando não há outro
        # estado físico conhecido para aquela etiqueta.
        for record in reversed(records):
            if record["kind"] in ("programacao", "restauracao"):
                current.setdefault(record["radio_id"], record)
        records = sorted(current.values(), key=lambda record: int(record["radio_id"][1:]))
        ready = [record for record in records
                 if record["kind"] == "programacao" and record["status"] == "verificado"]
        voice_ready = sum(record.get("voice_test") == "aprovado" for record in ready)
        print(f"Cliente: {client} | programados e conferidos: {len(ready)} | voz aprovada: {voice_ready}\n")
        if not records:
            print("Ainda não há programação registrada. Use status --historico para ver as preparações.")
    else:
        print(f"Cliente: {client} — histórico completo\n")
    for record in records:
        print(f"{record['radio_id']} | {record['kind']} | memória: {record['status']}"
              f" | voz: {record.get('voice_test', 'não testada')}")
        print(f"  {record['path']}")


def run_radio_with_retry(programmer: Programmer, client: str, label: str,
                         requested_port: str | None) -> Attempt | None:
    """Mantém o mesmo aparelho na tela; após upload só repete a conferência."""
    pending_attempt = None
    while True:
        try:
            # Reconectar o cabo pode mudar o nome da porta. Não reutilizar uma
            # conexão anterior nem perder a fase de conferência ao falhar aqui.
            port = core.resolve_port(requested_port)
            if pending_attempt is not None:
                return programmer.verify_attempt(pending_attempt, label, port)
            return programmer.program_one(client, label, port)
        except OperationFailed as exc:
            if isinstance(exc.__cause__, KeyboardInterrupt):
                raise KeyboardInterrupt from exc
            record = exc.attempt.record
            if record.get("write_started") or record.get("failed_stage") in (
                    "conferindo", "retomando_conferencia"):
                pending_attempt = exc.attempt.path
            print(f"\nFALHOU: {exc}", flush=True)
        except (core.RadioLoteError, OSError) as exc:
            print(f"\nFALHOU: {exc}", flush=True)

        print(f"\nMantenha o mesmo aparelho {label}. Confira o encaixe e desligue/ligue o rádio.")
        if pending_attempt is not None:
            print("A próxima tentativa somente relê e confere; não repete a gravação.")
            action = "repetir somente a conferência"
        else:
            print("Nenhuma gravação foi iniciada nesta tentativa. O lote permanece neste rádio.")
            action = "tentar a leitura novamente e continuar"
        while True:
            # EOF/Ctrl+C propagam normalmente; não virar um loop sem operador.
            answer = ask(f"Enter = {action} de {label} | q = encerrar: ").lower()
            if answer == "q":
                return None
            if answer == "":
                break
            print("Use Enter para repetir no mesmo rádio ou q para encerrar.")


def run(args) -> int:
    programmer = Programmer(args.dados, progress=lambda message: print(message, flush=True))
    if args.command == "portas":
        ports = core.list_usb_ports()
        if not ports:
            print("Nenhum cabo USB serial detectado. Conecte o cabo de programação.")
        for port in ports:
            print(f"{port['device']} — {port['description']} ({port['hwid']})")
        return 0
    if args.command == "status":
        show_status(programmer, args.cliente, history=args.historico)
        return 0

    with operation_lock(programmer.root):
        if args.command == "preparar":
            print(f"Leitura inicial de {args.radio}. Mantenha o CHIRP fechado e o rádio ligado.")
            port = core.resolve_port(args.porta)
            attempt = programmer.prepare(args.cliente, args.radio, port)
            print(f"Backup original: {attempt.path / 'original.img'}")
            if args.somente_ler:
                print_profile(attempt.record["profile"])
                print(f"Proposta salva sem ativar: {attempt.path}")
            else:
                confirm_profile(programmer, attempt)
            return 0

        if args.command == "ativar":
            confirm_profile(programmer, Attempt.load(args.tentativa))
            return 0

        if args.command == "programar":
            print_profile(programmer.load_profile(args.cliente))
            print("Mantenha o CHIRP fechado. Confira a etiqueta física de cada aparelho.")
            selected = args.radio
            while True:
                label = selected or programmer.next_radio_id(args.cliente)
                print(f"\nConecte o próximo rádio desligado, confira a etiqueta {label} e ligue-o.")
                answer = ask(f"Enter = programar {label} | Rxx = outra etiqueta | q = encerrar: ")
                if answer.lower() == "q":
                    break
                if answer:
                    label = answer.upper()
                attempt = run_radio_with_retry(programmer, args.cliente, label, args.porta)
                if attempt is None:
                    break
                success(attempt)
                if args.uma_vez:
                    break
                selected = None
            show_status(programmer, args.cliente)
            return 0

        if args.command in ("verificar", "restaurar"):
            source = Attempt.load(args.tentativa)
            programmer._check_physical_label(source, args.radio)
            print(f"Cliente: {source.record['client']} | aparelho: {args.radio}")
            print(f"Tentativa: {source.path}")
            if args.command == "restaurar":
                source.read_raw("original")
                print("Será recuperada a configuração original completa deste backup.")
                phrase = f"RESTAURAR {args.radio}"
                if ask(f"Confira a etiqueta do aparelho conectado e digite {phrase}: ") != phrase:
                    print("Restauração cancelada.")
                    return 0
            else:
                if ask(f"Conecte {args.radio} e pressione Enter para somente reler; q cancela: ").lower() == "q":
                    return 0
            port = core.resolve_port(args.porta)
            if args.command == "restaurar":
                attempt = programmer.restore(source.path, args.radio, port)
            else:
                attempt = programmer.verify_attempt(source.path, args.radio, port)
            success(attempt)
            return 0

        if args.command == "simular":
            attempt = programmer.simulate(args.imagem, args.cliente, args.perfil)
            print_profile(attempt.record["profile"])
            print(f"Simulação concluída: {attempt.record['changed_bytes']} bytes alterados.")
            print(f"Arquivos: {attempt.path}")
            print("Nenhuma porta serial foi aberta; nenhum perfil de produção foi ativado.")
            return 0

        if args.command == "voz":
            attempt = programmer.record_voice_test(args.tentativa, args.radio, args.resultado == "aprovado")
            print(f"{args.radio}: teste de voz registrado como {attempt.record['voice_test']}.")
            return 0
    return 0


def legacy_main(argv=None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    args = parser().parse_args(argv)
    try:
        return run(args)
    except OperationFailed as exc:
        print(f"\nFALHOU: {exc}", file=sys.stderr)
        record = exc.attempt.record
        if "preparada" in record.get("artifacts", {}) and record["kind"] in ("programacao", "restauracao"):
            print("Para retomar somente a conferência do mesmo aparelho:", file=sys.stderr)
            print(f"./radio-lote verificar --tentativa {shlex.quote(str(exc.attempt.path))}"
                  f" --radio {record['radio_id']}", file=sys.stderr)
        return 1
    except (core.RadioLoteError, OSError) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nEncerrado pelo operador.", file=sys.stderr)
        return 130


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    legacy = {"portas", "preparar", "ativar", "programar", "verificar", "restaurar", "simular", "voz"}
    # Existing command names remain available; explicitly selected legacy data
    # stays in place until the operator imports it into the new catalog.
    command = None
    skip_value = False
    for value in args:
        if skip_value:
            skip_value = False
            continue
        if value in {"--dados", "--data", "--lang"}:
            skip_value = True
            continue
        if not value.startswith("-"):
            command = value
            break
    if command in legacy or (command == "status" and ("--cliente" in args or "--historico" in args)):
        return legacy_main(args)
    from .console import main as public_main
    return public_main(args)
