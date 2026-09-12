"""Read local Teams v2 caches; JSON stdout, diagnostics stderr; no network."""
import argparse
import contextlib
import dataclasses
import json
import pathlib
import sys
import traceback

from .reader import TeamsCacheReader, default_cache_globs


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("--limit must be a positive integer")
    return number


def main(argv=None):
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
    args = parser.parse_args(argv)
    limit = getattr(args, "limit", 50)
    account = getattr(args, "account", None)
    leveldb = getattr(args, "leveldb", None)
    try:
        if args.command == "diagnose":
            # Shows candidate patterns only: never reads actual cached messages.
            output = {"python": sys.version, "executable": sys.executable,
                      "third_party_install_required": False,
                      "cache_patterns": default_cache_globs(),
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
                        for item in reader.conversations(account=account):
                            output.append(item)
                            if len(output) >= limit:
                                break
                    else:
                        output = []
                        query = args.query.casefold()
                        for message in reader.messages(account=account):
                            if query in message.content.casefold() or query in message.sender.casefold():
                                output.append(dataclasses.asdict(message))
                                if len(output) >= limit:
                                    break
                    skipped = reader.skipped
            print(f"# {len(output)} result(s), {skipped} record(s) skipped", file=sys.stderr)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except BrokenPipeError:
        return 1
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("Check --leveldb path and file permissions. Close Teams and retry if copying fails.", file=sys.stderr)
        if getattr(args, "debug", False):
            traceback.print_exc(file=sys.stderr)
        return 1
