#!/usr/bin/env python3
"""Move one shared login out of PostgreSQL, back to where a non-shared proxy reads it.

The rollback of account_grants_seed.py (docs/postgres-grants.md, "Rolling back"). The row is first
marked `exported`, once no host holds a lease on it, so no proxy serves or refreshes that
generation again; then its credentials are written to a cswap slot, or with --codex to a full
auth.json. `account_grants_seed.py --replace` puts it back in service.
"""

import argparse
import json
import runpy
import sys
from pathlib import Path

grants = runpy.run_path(str(Path(__file__).with_name("account_grants.py")))
Unavailable = grants["GrantsUnavailable"]


def cswap_slot(slot):
    """A writer into an existing, non-live cswap slot through cswap's own locked backup write."""
    from claude_swap.switcher import ClaudeAccountSwitcher

    switcher = ClaudeAccountSwitcher()
    if switcher.current_account_number() == slot:
        raise Unavailable("that slot is this host's live Claude login; Claude Code refreshes it")
    email = switcher.account_email(slot)
    if not email:
        raise Unavailable(f"cswap has no slot {slot}; restore it first (cswap import)")
    return lambda credentials: switcher.persist_backup_credentials(slot, email, credentials), f"cswap slot {slot} ({email})"


def auth_file(path, force):
    path = Path(path)
    if path.exists() and not force:
        raise Unavailable(f"{path} exists; pass --force to overwrite it")
    return lambda credentials: grants["write_private_json"](path, json.loads(credentials)), str(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("account", help="the proxy accounts file alias, or with --codex the Codex name")
    parser.add_argument("destination", help="cswap slot number, or with --codex the auth.json to write")
    parser.add_argument("--codex", action="store_true", help="export codex:<account> as a full auth.json")
    parser.add_argument("--force", action="store_true", help="with --codex, overwrite an existing file")
    args = parser.parse_args()
    if args.codex and not grants["CODEX_NAME"].fullmatch(args.account):
        sys.exit("a Codex name is letters, digits and . _ @ -")
    account = grants["CODEX_PREFIX"] + args.account if args.codex else args.account
    try:
        write, where = auth_file(args.destination, args.force) if args.codex else cswap_slot(args.destination)
        store = grants["store_from_env"](grants["codex_login"] if args.codex else grants["claude_login"])
        credentials, generation = store.export(account)
    except (ValueError, OSError) as error:
        sys.exit(f"not exported: {error if isinstance(error, Unavailable) else type(error).__name__}")
    try:
        write(credentials)
    except Exception as error:  # noqa: BLE001 - name the type only; the message could carry a path or token
        sys.exit(f"{account} is out of service but {where} was not written ({type(error).__name__}); "
                 "fix that and run this again")
    print(f"{account} generation {generation} exported to {where}.")
    print("No proxy serves or refreshes it from PostgreSQL now. Turn shared mode off for it,")
    print("or put it back in service with account_grants_seed.py --replace.")


if __name__ == "__main__":
    main()
