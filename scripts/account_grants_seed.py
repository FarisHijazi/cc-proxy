#!/usr/bin/env python3
"""Move one cswap slot's Claude login into the shared grants table (docs/postgres-grants.md).

Reads CCP_GRANTS_DSN, CCP_GRANTS_NAMESPACE and CCP_GRANTS_KEY_FILE like the proxy. It never
deletes the cswap copy: it prints the command that does, which must run before any proxy
serves the account from PostgreSQL, or two holders spend the same refresh grant.
"""

import argparse
import os
import runpy
import sys
from pathlib import Path

grants = runpy.run_path(str(Path(__file__).with_name("account_grants.py")))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("slot", help="cswap slot number")
    parser.add_argument("alias", help="the proxy accounts file alias (an email)")
    parser.add_argument("--replace", action="store_true", help="overwrite an existing shared login")
    args = parser.parse_args()
    from claude_swap.switcher import ClaudeAccountSwitcher

    names = ("CCP_GRANTS_DSN", "CCP_GRANTS_NAMESPACE", "CCP_GRANTS_KEY_FILE")
    if not all(os.environ.get(n) for n in names):
        sys.exit("set " + ", ".join(names))
    dsn, namespace, key_file = (os.environ[n] for n in names)
    try:
        switcher = ClaudeAccountSwitcher()
        if switcher.current_account_number() == args.slot:
            raise grants["GrantsUnavailable"]("that slot is this host's live Claude login; Claude Code refreshes it")
        email = switcher._get_sequence_data()["accounts"][args.slot]["email"]
        credentials, _ = switcher._read_account_credentials_ex(args.slot, email)
        store = grants["PostgresGrants"](dsn, namespace, grants["read_key"](key_file), Path(key_file).parent / "grants-journal")
        generation = store.seed(args.alias, credentials, args.replace)
    except (KeyError, TypeError):
        sys.exit(f"cswap has no slot {args.slot}")
    except (ValueError, OSError) as error:
        sys.exit(f"not seeded: {error if isinstance(error, grants['GrantsUnavailable']) else type(error).__name__}")
    print(f"{args.alias} stored as generation {generation} in namespace {namespace}.")
    print("Before any proxy serves it, remove every other holder of this grant, starting with cswap:")
    print(f"  cswap remove {args.slot}")


if __name__ == "__main__":
    main()
