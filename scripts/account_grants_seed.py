#!/usr/bin/env python3
"""Move one login into the shared grants table (docs/postgres-grants.md).

A Claude login comes from a cswap slot; with --codex, a Codex CLI auth.json becomes the row
`codex:<name>`. Reads CCP_GRANTS_DSN, CCP_GRANTS_NAMESPACE and CCP_GRANTS_KEY_FILE like the proxy.
It never deletes the source: it prints what does, which must happen before any proxy serves the
account from PostgreSQL, or two holders spend the same refresh grant.
"""

import argparse
import runpy
import sys
from pathlib import Path

grants = runpy.run_path(str(Path(__file__).with_name("account_grants.py")))


def claude_login(slot):
    from claude_swap.switcher import ClaudeAccountSwitcher

    switcher = ClaudeAccountSwitcher()
    if switcher.current_account_number() == slot:
        raise grants["GrantsUnavailable"]("that slot is this host's live Claude login; Claude Code refreshes it")
    email = switcher._get_sequence_data()["accounts"][slot]["email"]
    credentials, _ = switcher._read_account_credentials_ex(slot, email)
    return credentials


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", help="cswap slot number, or with --codex the Codex auth.json to move")
    parser.add_argument("alias", help="the proxy accounts file alias (an email), or with --codex the Codex name")
    parser.add_argument("--codex", action="store_true", help="seed a Codex CLI login as codex:<alias>")
    parser.add_argument("--file", action="store_true",
                        help="source is a Claude Code .credentials.json (a host's live login), not a cswap slot")
    parser.add_argument("--replace", action="store_true", help="overwrite an existing shared login")
    args = parser.parse_args()
    if args.codex and not grants["CODEX_NAME"].fullmatch(args.alias):
        sys.exit("a Codex name is letters, digits and . _ @ -")
    account = grants["CODEX_PREFIX"] + args.alias if args.codex else args.alias
    try:
        credentials = Path(args.source).read_text() if args.codex or args.file else claude_login(args.source)
        store = grants["store_from_env"](grants["codex_login"] if args.codex else grants["claude_login"])
        generation = store.seed(account, credentials, args.replace)
    except (KeyError, TypeError):
        sys.exit(f"cswap has no slot {args.source}")
    except (ValueError, OSError) as error:
        sys.exit(f"not seeded: {error if isinstance(error, grants['GrantsUnavailable']) else type(error).__name__}")
    print(f"{account} stored as generation {generation} in namespace {store.namespace}.")
    print("Before any proxy serves it, remove every other holder of this grant, starting with the source:")
    if args.codex:
        print(f"  rm {args.source}   # a Codex CLI on that host must not keep this login")
    elif args.file:
        print(f"  move {args.source} aside and drop oauthAccount from ~/.claude.json   # Claude Code must not keep it")
    else:
        print(f"  cswap remove {args.source}")


if __name__ == "__main__":
    main()
