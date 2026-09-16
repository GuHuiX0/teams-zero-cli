"""Read local Teams v2 caches; JSON stdout, diagnostics stderr; no network."""
import argparse
import contextlib
import dataclasses
import json
import logging
from logging.handlers import RotatingFileHandler
import pathlib
import sys
import traceback

from .reader import TeamsCacheReader, default_cache_globs, find_caches
from .ingest import Monitor, message_order, timestamp


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("--limit must be a positive integer")
    return number


def make_parser():
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS prevents child parser defaults erasing options before subcommand.
    common.add_argument("--leveldb", default=argparse.SUPPRESS, help="IndexedDB .leveldb directory")
    common.add_argument("--account", default=argparse.SUPPRESS, help="tenantId:userId")
    common.add_argument("--limit", type=positive, default=argparse.SUPPRESS, help="positive maximum results (default 50)")
    common.add_argument("--debug", action="store_true", default=argparse.SUPPRESS)
    parser = argparse.ArgumentParser(description=__doc__, parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("accounts", "conversations", "diagnose"):
        sub.add_parser(name, parents=[common])
    search = sub.add_parser("search", parents=[common])
    search.add_argument("query")
    search.add_argument('--conversation-id')
    search.add_argument('--sender')
    search.add_argument('--since')
    search.add_argument('--until')
    search.add_argument('--order', choices=['oldest', 'newest'], default='oldest')
    for name in ('messages', 'mentions'):
        command = sub.add_parser(name, parents=[common])
        command.add_argument('--conversation-id', required=name == 'messages')
        command.add_argument('--offset', type=int, default=0)
        if name == 'messages':
            command.add_argument('--since')
    serve = sub.add_parser('serve', help='stdio MCP tools server')
    serve.add_argument('--config', help='Bind monitor operations to this configuration')
    serve.add_argument('--debug', action='store_true')
    config_parent = argparse.ArgumentParser(add_help=False)
    config_parent.add_argument('--config', default=argparse.SUPPRESS)
    monitor = sub.add_parser('monitor', parents=[config_parent])
    operations = monitor.add_subparsers(dest='operation', required=True)
    for name in ('configure', 'bootstrap', 'run', 'status', 'batches', 'show', 'ack',
                 'install-task', 'remove-task'):
        command = operations.add_parser(name, parents=[config_parent, common])
        if name == 'configure':
            selection = command.add_mutually_exclusive_group(required=True)
            selection.add_argument('--conversation')
            selection.add_argument('--conversation-id')
            command.add_argument('--state', default='state/dev-team.json')
            command.add_argument('--output-dir', default='digests')
        if name == 'run':
            command.add_argument('--scheduled', action='store_true', help='Write rotating operational logs')
        if name in ('batches', 'show'):
            command.add_argument('--offset', type=int, default=0)
        if name == 'batches':
            command.add_argument('--all', action='store_true', help='Include delivered batches')
        if name in ('show', 'ack'):
            command.add_argument('batch_id')
        if name == 'ack':
            command.add_argument('--receipt', required=True)
        if name in ('install-task', 'remove-task'):
            command.add_argument('--task-name', default='TeamsCacheMonitor')
        if name == 'install-task':
            command.add_argument('--interval-minutes', type=positive, default=15)
            command.add_argument('--dry-run', action='store_true', help='Return XML without installing')
    return parser


def monitor_command(args):
    from .service import TeamsService, configure_monitor
    from . import scheduler
    operation = args.operation
    config = getattr(args, 'config', None)
    if operation == 'remove-task':
        return scheduler.remove_task(args.task_name)
    if not config:
        raise ValueError('monitor commands require --config')
    if operation == 'configure':
        return configure_monitor(config, conversation=args.conversation,
            conversation_id=args.conversation_id, account=getattr(args, 'account', None),
            leveldb=getattr(args, 'leveldb', None), state_path=args.state, output_path=args.output_dir)
    if operation == 'install-task':
        if not Monitor(config).status()['bootstrap_complete']:
            raise ValueError('Bootstrap this monitor before installing the scheduled task')
        options = dict(python_path=sys.executable,
                       script_path=str(pathlib.Path(__file__).resolve().parent.parent / 'teams_cli.py'),
                       config_path=str(pathlib.Path(config).resolve()),
                       interval_minutes=args.interval_minutes, task_name=args.task_name)
        if args.dry_run:
            return {'task_name': args.task_name, 'xml': scheduler.task_xml(**options)}
        return scheduler.install_task(**options)
    service = TeamsService(config)
    tool = {'bootstrap': 'teams_bootstrap', 'run': 'teams_ingest',
            'status': 'teams_get_checkpoint', 'batches': 'teams_list_batches',
            'show': 'teams_get_batch', 'ack': 'teams_ack_batch'}[operation]
    arguments = {}
    if operation in ('batches', 'show'):
        arguments.update(offset=args.offset, limit=getattr(args, 'limit', 100))
    if operation == 'batches':
        arguments['pending_only'] = not args.all
    if operation in ('show', 'ack'):
        arguments['batch_id'] = args.batch_id
    if operation == 'ack':
        arguments['receipt'] = args.receipt
    return service.call(tool, arguments)


def scheduled_logger(config):
    monitor = Monitor(config)
    path = pathlib.Path(monitor.config['log_path'])
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.Logger('teams-monitor')
    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger.addHandler(handler)
    return logger


def main(argv=None):
    parser = make_parser()
    args = parser.parse_args(argv)
    limit = getattr(args, "limit", 50)
    account = getattr(args, "account", None)
    leveldb = getattr(args, "leveldb", None)
    logger = None
    try:
        if getattr(args, 'scheduled', False):
            if not getattr(args, 'config', None):
                raise ValueError('--scheduled requires --config')
            logger = scheduled_logger(args.config)
        if args.command == 'serve':
            from .service import TeamsService
            from .mcp_server import serve
            return serve(TeamsService(args.config))
        if args.command == 'monitor':
            with contextlib.redirect_stdout(sys.stderr):
                output = monitor_command(args)
            if logger:
                logger.info('run completed batch=%s new=%s updated=%s', output.get('batch_id'),
                            output.get('messages_new'), output.get('messages_updated'))
        elif args.command in ('messages', 'mentions'):
            from .service import TeamsService
            arguments = {'offset': args.offset, 'limit': limit}
            for key, value in [('account', account), ('leveldb', leveldb),
                               ('conversation_id', args.conversation_id)]:
                if value is not None:
                    arguments[key] = value
            if args.command == 'messages' and args.since:
                arguments.update(mode='since', after=args.since)
            with contextlib.redirect_stdout(sys.stderr):
                output = TeamsService().call('teams_load_messages' if args.command == 'messages'
                                             else 'teams_get_mentions', arguments)
        elif args.command == "diagnose":
            # Shows candidate patterns only: never reads actual cached messages.
            output = {"python": sys.version, "executable": sys.executable,
                      "third_party_install_required": False,
                      "target_python": '3.12', "target_runtime_available": sys.version_info[:2] == (3, 12),
                      "cache_patterns": default_cache_globs(),
                      "cache_candidates": find_caches(),
                      "explicit_path_is_directory": pathlib.Path(leveldb).is_dir() if leveldb else None}
        else:
            # Upstream forensic parsers occasionally print diagnostics to stdout.
            with contextlib.redirect_stdout(sys.stderr):
                with TeamsCacheReader(leveldb) as reader:
                    if args.command == "accounts":
                        output = [dataclasses.asdict(a) | {"key": a.key}
                                  for a in reader.accounts() if not account or a.key == account][:limit]
                    elif args.command == "conversations":
                        output = []
                        output = sorted(reader.conversations(account=account),
                                        key=lambda item: (item.get('account', ''), item.get('id', '')))[:limit]
                    else:
                        output = []
                        query = args.query.casefold()
                        bounds = {}
                        for key in ('since', 'until'):
                            value = getattr(args, key, None)
                            bounds[key] = timestamp(value) if value else None
                            if value and bounds[key] is None:
                                raise ValueError(f'Invalid --{key} timestamp')
                        for message in reader.messages(account=account):
                            if args.conversation_id and message.conversation_id != args.conversation_id:
                                continue
                            if args.sender and args.sender.casefold() not in message.sender.casefold():
                                continue
                            dt = timestamp(message.timestamp)
                            if bounds['since'] and (dt is None or dt < bounds['since']):
                                continue
                            if bounds['until'] and (dt is None or dt > bounds['until']):
                                continue
                            if query in message.content.casefold() or query in message.sender.casefold():
                                output.append(dataclasses.asdict(message))
                        output.sort(key=message_order, reverse=args.order == 'newest')
                        output = output[:limit]
                    skipped = reader.skipped
            print(f"# {len(output)} result(s), {skipped} record(s) skipped", file=sys.stderr)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except BrokenPipeError:
        return 1
    except Exception as exc:
        if logger:
            logger.error('%s: %s', type(exc).__name__, exc)
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("Use --debug for details. For cache copy errors, close Teams and retry.", file=sys.stderr)
        if getattr(args, "debug", False):
            traceback.print_exc(file=sys.stderr)
        return 1
    finally:
        if logger:
            for handler in logger.handlers:
                handler.close()
