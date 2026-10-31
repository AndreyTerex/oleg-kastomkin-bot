"""Offline migration/backup. Dry run by default; no credentials on the command line."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path

import config
import datasync
from shared_state import SharedState, SharedStateError, digest
from stats import ELO_VERSION


def load_export(path: Path) -> dict:
    files, meta = datasync.read_archive(path.read_bytes())
    unknown = set(meta.get("files", [])) - set(datasync.SYNC_FILES)
    if unknown:
        raise ValueError("Export contains newer, unsupported stores: " + ", ".join(sorted(unknown)))
    entries = {name: json.loads(content) for name, content in files.items()}
    if "stats.json" not in entries:
        raise ValueError("Export has no stats.json")
    if "_sheets_journal" in entries["stats.json"]:
        raise ValueError("Reconcile the Sheets experiment separately before PostgreSQL migration")
    return entries


def stats_summary(entries: dict) -> dict:
    result = {}
    for guild_id, data in entries["stats.json"].items():
        if not guild_id.isdigit():
            continue  # opaque metadata is preserved, not interpreted or removed
        if not isinstance(data, dict) or not isinstance(data.get("games"), list) or not isinstance(data.get("players"), dict):
            raise ValueError("Unsupported live stats schema; obtain the current container schema before migration")
        if data.get("elo_version", 0) > ELO_VERSION:
            raise ValueError("Export uses newer Elo code; review the current container before migration")
        games = data["games"]
        ids = [game.get("id") for game in games]
        if not all(type(i) is int for i in ids) or len(set(ids)) != len(ids):
            raise ValueError("Game IDs must be distinct integers")
        next_id = data.get("next_id")
        if type(next_id) is not int or next_id <= max(ids, default=0):
            raise ValueError("next_id would collide with retained games")
        result[guild_id] = {"games": len(games), "players": len(data["players"]),
                            "next_id": next_id, "game_ids": sorted(ids)}
    if not result:
        raise ValueError("No guild statistics found")
    return result


def check_expectations(summary, guild_id=None, min_games=0, min_next_id=0, required_ids=()):
    if guild_id is None:
        if min_games or min_next_id or required_ids:
            raise ValueError("Specify --guild-id with migration assertions")
        return
    row = summary.get(str(guild_id))
    if row is None or row["games"] < min_games or row["next_id"] < min_next_id or not set(required_ids) <= set(row["game_ids"]):
        raise ValueError("Export does not contain the required recovered games/counters; obtain a fresh export")


def write_backup(path: Path, entries: dict):
    guilds = [int(g) for g in entries.get("stats.json", {}) if g.isdigit()]
    payload, _ = datasync.archive_entries(entries, guilds)
    # Exclusive creation prevents an accidental overwrite of the rollback copy.
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
        out.write(payload)
        out.flush()
        os.fsync(out.fileno())


@contextlib.contextmanager
def database_tunnel():
    """Порт туннеля к базе через VPN бота (как у работающего бота), или None — напрямую.
    Бот при этом остановлен, поэтому Xray поднимаем сами на время операции."""
    from vpn import VPN_CLIENT, database_target
    if not (VPN_CLIENT.enabled and database_target()):
        yield None
        return
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(VPN_CLIENT.start())
        port = VPN_CLIENT.database_port()
        print("Подключение к базе:", "через VPN бота" if port else "напрямую (VPN бота не поднялся)")
        yield port
    finally:
        loop.run_until_complete(VPN_CLIENT.close())
        loop.close()


def open_backend(tunnel_port: int | None) -> SharedState:
    return SharedState(config.DATABASE_URL, config.SHARED_STATE_KEY, config.DATA_DIR, datasync.SYNC_FILES,
                       tunnel_port=tunnel_port)


def export_local() -> None:
    """Возврат на локальные файлы: снимок общей базы → data/*.json. Прежние локальные файлы
    сохраняются в data/backups-import/<время>. Бот должен быть остановлен (берём его замок)."""
    with database_tunnel() as port:
        current = read_for_export(open_backend(port))
    # Все файлы общей базы, даже пустые: иначе остался бы старый локальный файл (сборы, кошельки, память).
    files = {name: json.dumps(current.get(name, {}), ensure_ascii=False, indent=2).encode("utf-8")
             for name in datasync.SYNC_FILES}
    saved = datasync.apply_archive(config.DATA_DIR, files)
    print("Перенесено в", config.DATA_DIR + ":", ", ".join(sorted(current)) or "—")
    print("Прежние локальные файлы сохранены в", saved)
    print("Теперь уберите DATABASE_URL из .env и запустите бота: docker compose up -d")


def read_for_export(backend: SharedState) -> dict:
    try:
        if not backend.acquire():
            raise SharedStateError("Бот ещё работает с базой — остановите его (docker compose stop bot) и повторите")
        backend.require_initialized()
        current = backend.snapshot()
    finally:
        backend.close()
    if not current:
        raise SharedStateError("В общей базе нет данных — переносить нечего")
    return current


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["init", "restore", "backup", "export-local"])
    parser.add_argument("--source", type=Path)
    parser.add_argument("--backup", type=Path, help="New ZIP path; never overwritten")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-digest", help="Current DB digest, required for restore")
    parser.add_argument("--guild-id", type=int)
    parser.add_argument("--min-games", type=int, default=0)
    parser.add_argument("--min-next-id", type=int, default=0)
    parser.add_argument("--require-game-id", type=int, action="append", default=[])
    args = parser.parse_args(argv)
    if args.operation not in ("backup", "export-local"):
        if args.source is None:
            parser.error("--source is required")
        entries = load_export(args.source)
        summary = stats_summary(entries)
        check_expectations(summary, args.guild_id, args.min_games, args.min_next_id, args.require_game_id)
        print(json.dumps({"source_digest": digest(entries), "stats": summary}, ensure_ascii=False))
    if not args.apply and args.operation != "export-local":
        print("Dry run: no database connection or mutation. Keep the source export and stop all bots before applying.")
        return
    if args.operation == "export-local":
        if not config.DATABASE_URL:
            parser.error("DATABASE_URL is required for export-local")
        export_local()
        return
    if not config.DATABASE_URL or args.backup is None:
        parser.error("DATABASE_URL and --backup are required for --apply")
    with database_tunnel() as port:
        apply_operation(args, entries if args.operation != "backup" else None, port)


def apply_operation(args, entries, tunnel_port):
    backend = open_backend(tunnel_port)
    try:
        if args.operation == "backup":
            backend.require_initialized()
            current = backend.snapshot()
            write_backup(args.backup, current)
            print("Database backup digest:", digest(current))
            return
        if not backend.acquire():
            raise SharedStateError("Active bot owns the database; stop it before migration/restore")
        if args.operation == "init":
            # Back up and validate source before creating the first database objects.
            write_backup(args.backup, entries)
            backend.ensure_schema()
            backend.initialize(entries)
        else:
            backend.require_initialized()
            current = backend.snapshot()
            if not args.expected_digest or digest(current) != args.expected_digest:
                raise SharedStateError("Database digest differs; take a fresh backup and review before restore")
            write_backup(args.backup, current)
            backend.save(entries)
        readback = backend.snapshot()
        if any(readback.get(name) != data for name, data in entries.items()):
            raise SharedStateError("Readback differs; keep bots stopped and reconcile from backups")
        print("Verified database digest:", digest(readback))
        print(json.dumps({"stats": stats_summary(readback)}, ensure_ascii=False))
    finally:
        backend.close()


if __name__ == "__main__":
    try:
        main()
    except (SharedStateError, ValueError, OSError) as error:
        raise SystemExit(str(error)) from None
