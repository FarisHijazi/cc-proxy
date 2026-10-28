"""Command-line interface for the Claude Remote Control API.

    claude-rc whoami                 # show login / org / token status
    claude-rc auth                   # which auth source is used (cookie names only)
    claude-rc list                   # list your Remote Control sessions
    claude-rc get <session_id>       # session details
    claude-rc events <session_id>    # recent events (history)
    claude-rc watch <session_id>     # stream events live (read-only)
    claude-rc info <session_id>      # summary, context, plan usage, git changes
    claude-rc ask <session_id> "what are you stuck on?"   # side question
    claude-rc send <session_id> "run the tests"   # send a message, print the reply
    claude-rc repl <session_id>      # interactive chat with a live session
    claude-rc rename <session_id> "new title"
    claude-rc web                    # browser control panel for all sessions

Claude Code on the web (cloud sessions):

    claude-rc envs list              # environments (cloud, bridge, self-hosted)
    claude-rc create --env env_… --model claude-haiku-5-5 --repo owner/name "fix the bug"
    claude-rc pending <session_id>   # prompts waiting for an answer
    claude-rc answer <session_id> allow | always | deny
    claude-rc answer <session_id> question --select "Blue" [--other TEXT | --skip]
    claude-rc commands <session_id> list | run compact
    claude-rc control <session_id> get_session_cost --field k=v
    claude-rc model|effort|mode <session_id> VALUE ; claude-rc interrupt <session_id>
    claude-rc tag|share|mark-read|unarchive|delete <session_id> …
    claude-rc triggers list | get | create | update | run | enable | disable | runs
    claude-rc watch                  # account-wide session feed (no session id)
    claude-rc speak "hello" -o hi.ogg   # claude.ai text to speech
    claude-rc upload <path> [--send <session_id> "look at this"]

Credentials: --token, --cswap-slot, --session-key, --org (else env
CLAUDE_AI_ACCESS_TOKEN / CLAUDE_AI_SESSION_KEY / CLAUDE_RC_CSWAP_SLOT, else
``~/.claude/.credentials.json``). Tokens are never printed.

`send`, `repl`, `rename`, and `web` steer live sessions — only use them on
sessions you started with `claude remote-control`. `ask` and `info` query the
worker (a side question costs a model call; neither touches the transcript).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from typing import Any, Optional

from . import __version__
from .client import APIError, ControlRejected, ControlTimeout, RemoteControlClient
from .credentials import CredentialsError, load_org_uuid, resolve_auth
from . import webauth

try:  # optional pretty output
    from rich.console import Console
    from rich.markup import escape as _escape
    from rich.table import Table

    _console: Optional["Console"] = Console()
except Exception:  # pragma: no cover - rich optional
    _console = None


def _print(*a, **k):
    if _console:
        _console.print(*a, **k)
    else:
        print(*[str(x) for x in a], **k)


def _esc(text: object) -> str:
    """Session content for _print: rich would read `[...]` in it as markup."""
    return _escape(str(text)) if _console else str(text)


def _fmt_time(iso: Optional[str]) -> str:
    if not iso:
        return "-"
    try:
        t = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        delta = _dt.datetime.now(_dt.timezone.utc) - t
        secs = int(delta.total_seconds())
        for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
            if secs >= n:
                return f"{secs // n}{unit} ago"
        return f"{secs}s ago"
    except Exception:
        return iso


# --- commands --------------------------------------------------------------
def cmd_whoami(args) -> int:
    try:
        auth = resolve_auth(
            getattr(args, "token", None),
            session_key=getattr(args, "session_key", None),
            cswap_slot=getattr(args, "cswap_slot", None),
        )
    except CredentialsError as e:
        _print(f"not logged in: {e}")
        return 1
    org = getattr(args, "org", None) or load_org_uuid()
    _print(f"auth source  : {auth.origin}")
    if auth.is_cookie:
        _print("mode         : claude.ai sessionKey cookie")
    else:
        creds = auth.credentials
        _print(f"access token : {'present' if creds.access_token else 'MISSING'} (len {len(creds.access_token)})")
        _print(f"expired      : {creds.is_expired()}")
        _print(f"scopes       : {', '.join(creds.scopes) or '(unknown)'}")
        _print(f"subscription : {creds.subscription_type or '-'}")
    _print(f"org uuid     : {'present' if org else 'not configured (looked up from /api/oauth/profile on use)'}")
    return 0


def cmd_auth(args) -> int:
    """Offline diagnostic: the auth source a client would use. Never prints a
    secret — cookies by name only."""
    try:
        auth = resolve_auth()
    except CredentialsError as e:
        _print(f"no auth source: {e}")
        return 1
    _print(f"source       : {auth.origin}")
    jar = auth.jar
    if jar is None:
        _print("mode         : bearer OAuth (api.anthropic.com)")
        return 0
    _print("mode         : claude.ai cookies")
    _print(f"cookies      : {', '.join(jar.names())}")
    _print(f"org (cookie) : {jar.org_uuid or '-'}")
    _print(f"owns refresh : {jar.owns_refresh()}")
    _print(f"state file   : {webauth.state_path()}{'' if webauth.state_path().exists() else ' (absent)'}")
    return 0


def _context_pct(session: dict) -> str:
    usage = (session.get("external_metadata") or {}).get("context_usage") or {}
    if not usage.get("max_tokens"):
        return "-"
    return f"{round(100 * usage.get('used_tokens', 0) / usage['max_tokens'])}%"


def cmd_list(args) -> int:
    with _rc(args) as rc:
        sessions = rc.sessions(limit=args.limit)
    if args.json:
        _dump(sessions)
    elif _console:
        table = Table(title=f"{len(sessions)} Remote Control sessions")
        for col in ("id", "title", "status", "worker", "model", "effort", "ctx", "last activity"):
            table.add_column(col, overflow="fold")
        for s in sessions:
            table.add_row(
                s.get("id", ""),
                (s.get("title") or "")[:50],
                str(s.get("status", "")),
                str(s.get("worker_status", "")),
                (s.get("config") or {}).get("model", ""),
                (s.get("external_metadata") or {}).get("effort_level") or "-",
                _context_pct(s),
                _fmt_time(s.get("last_event_at")),
            )
        _console.print(table)
    else:
        for s in sessions:
            print(f"{s.get('id')}\t{s.get('status')}\t{(s.get('title') or '')[:50]}")
    return 0


def cmd_get(args) -> int:
    import json

    with _rc(args) as rc:
        print(json.dumps(rc.get_session(args.session_id), indent=2))
    return 0


def cmd_events(args) -> int:
    with _rc(args) as rc:
        # The newest `limit` events, oldest first (`asc` would be the session's FIRST ones).
        evs = rc.list_events(args.session_id, limit=args.limit, sort_order="desc")
    if args.json:
        _dump([e.raw or e.payload for e in reversed(evs)])
        return 0
    for e in reversed(evs):
        _render_event(e)
    return 0


def cmd_watch(args) -> int:
    if not args.session_id:
        return _watch_account(args)
    _print("streaming (Ctrl-C to stop)…")
    with _rc(args) as rc:
        try:
            for e in rc.stream_events(args.session_id, from_sequence_num=args.from_seq):
                _render_event(e)
        except KeyboardInterrupt:
            return 0
    return 0


def cmd_info(args) -> int:
    with _rc(args) as rc:
        s = rc.get_session(args.session_id)
        meta = s.get("external_metadata") or {}
        summary = meta.get("post_turn_summary") or {}
        _print(_esc(f"{s.get('title')}  ({s.get('status')} · {s.get('worker_status')})"))
        _print(f"model   : {(s.get('config') or {}).get('model', '-')}  effort {meta.get('effort_level') or '-'}")
        _print(f"context : {_context_pct(s)}")
        if summary:
            _print(_esc(f"summary : {summary.get('needs_action') or summary.get('status_detail') or '-'}"))
        if s.get("connection_status") != "connected":
            return 0  # the controls below need a connected worker
        usage = rc.get_usage(args.session_id)
        for limit in (usage.get("rate_limits") or {}).get("limits") or []:
            _print(f"plan    : {limit.get('kind')} {limit.get('percent')}% (resets {limit.get('resets_at')})")
        diff = rc.get_workspace_diff(args.session_id)
        if diff:
            st = diff.get("stats") or {}
            _print(f"changes : {st.get('filesCount', 0)} files +{st.get('linesAdded', 0)} -{st.get('linesRemoved', 0)}")
        _print(f"worker  : Claude Code {meta.get('container_cc_version', '?')}")
    return 0


def cmd_ask(args) -> int:
    with _rc(args) as rc:
        answer = rc.side_question(args.session_id, args.question)
    _print(_esc(answer) if answer is not None else "(no answer — the session has no conversation yet)")
    return 0


def cmd_rename(args) -> int:
    with _rc(args) as rc:
        _print(_esc(f"renamed → {rc.rename_session(args.session_id, args.title).get('title')}"))
    return 0


def cmd_send(args) -> int:
    with _rc(args) as rc:
        for e in rc.send_and_collect(args.session_id, args.message):
            _render_event(e)
    return 0


def cmd_repl(args) -> int:
    _print("REPL — type a message and press enter; Ctrl-D to quit.")
    with _rc(args) as rc:
        while True:
            try:
                text = input("you> ")
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not text.strip():
                continue
            for e in rc.send_and_collect(args.session_id, text):
                _render_event(e)


def cmd_web(args) -> int:
    from .webui import serve

    with _rc(args) as rc:
        serve(
            host=args.host,
            port=args.port,
            open_browser=not args.no_open,
            client=rc,
            verbose=args.verbose,
        )
    return 0


# --- shared plumbing ---------------------------------------------------------
def _rc(args) -> RemoteControlClient:
    """A client from the global credential flags (unset flags fall through to
    env / ``~/.claude``, see :func:`~claude_rc.credentials.resolve_auth`)."""
    return RemoteControlClient(
        org_uuid=getattr(args, "org", None),
        token=getattr(args, "token", None),
        session_key=getattr(args, "session_key", None),
        cswap_slot=getattr(args, "cswap_slot", None),
    )


def _dump(obj: Any) -> None:
    """JSON to stdout (plain print: rich would re-highlight and wrap it)."""
    print(json.dumps(obj, indent=2, default=str))


def _value(text: str) -> Any:
    """A command-line value: JSON when it parses (``true``, ``3``, ``{…}``),
    else the string itself."""
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


def _fields(pairs: Optional[list[str]]) -> dict:
    """``["k=v", …]`` -> ``{k: _value(v)}``."""
    out: dict[str, Any] = {}
    for pair in pairs or []:
        key, sep, val = pair.partition("=")
        if not sep or not key:
            raise SystemExit(f"expected key=value, got {pair!r}")
        out[key] = _value(val)
    return out


def _targeted(values: Optional[list[str]], default: Any) -> list[tuple[Any, str]]:
    """``--select``-style values: ``"2:Blue"`` targets question index 2, a bare
    ``"Blue"`` targets ``default`` (``--question``)."""
    out = []
    for v in values or []:
        head, sep, rest = v.partition(":")
        out.append((int(head), rest) if sep and head.isdigit() else (default, v))
    return out


def _question_key(text: Optional[str]) -> Any:
    """``--question`` as an index when numeric, else a question's text/header."""
    if text is None:
        return 0
    return int(text) if text.isdigit() else text


def _short(s: dict) -> str:
    return f"{s.get('id')}\t{s.get('status', '')}\t{(s.get('title') or '')[:60]}"


# --- session lifecycle ---------------------------------------------------------
def cmd_create(args) -> int:
    from .environments import default_environment
    from .lifecycle import session_web_url

    with _rc(args) as rc:
        env_id = args.env
        if not env_id:
            env = default_environment(rc.list_environments())
            if not env:
                _print("no environment found — pass --env")
                return 1
            env_id = env.get("environment_id") or env.get("id")
        config = _fields(args.config)
        s = rc.create_session(
            env_id,
            args.prompt,
            title=args.title,
            repo=args.repo,
            revision=args.revision,
            branch=args.branch,
            model=args.model,
            effort_level=args.effort,
            append_system_prompt=args.append_system_prompt,
            permission_mode=args.permission_mode,
            tags=args.tag or None,
            defer_events=args.defer_events,
            **config,
        )
    if args.json:
        _dump(s)
    else:
        print(_short(s))
        print(session_web_url(s.get("id", "")))
    return 0


def cmd_archive(args) -> int:
    with _rc(args) as rc:
        done = rc.archive_session(args.session_id)
    print(f"archived {args.session_id}" if done else f"{args.session_id} was already archived")
    return 0


def cmd_unarchive(args) -> int:
    with _rc(args) as rc:
        out = rc.unarchive_session(args.session_id)
    _dump(out) if args.json else print(f"unarchived {args.session_id}")
    return 0


def cmd_delete(args) -> int:
    with _rc(args) as rc:
        rc.delete_session(args.session_id)
    print(f"deleted {args.session_id}")
    return 0


def cmd_tag(args) -> int:
    with _rc(args) as rc:
        s = rc.tag_session(args.session_id, add=args.add or (), remove=args.remove or ())
    _dump(s) if args.json else print(f"tags: {', '.join(s.get('tags') or []) or '-'}")
    return 0


def cmd_share(args) -> int:
    with _rc(args) as rc:
        if args.set:
            rc.set_share(args.session_id, **_fields(args.set))
        _dump(rc.get_share(args.session_id))
    return 0


def cmd_mark_read(args) -> int:
    with _rc(args) as rc:
        rc.mark_read(args.session_id, args.sequence_num, event_id=args.event_id)
    print("marked read")
    return 0


# --- environments ----------------------------------------------------------------
def cmd_envs(args) -> int:
    from .environments import redact_environment

    with _rc(args) as rc:
        if args.action == "list":
            envs = rc.list_environments(kind=args.kind)
            if args.json:
                _dump([redact_environment(e) for e in envs])
            else:
                for e in envs:
                    eid = e.get("environment_id") or e.get("id")
                    print(f"{eid}\t{e.get('kind', '')}\t{e.get('name', '')}")
            return 0
        if args.action == "get":
            if not args.id:
                raise SystemExit("envs get needs an environment id")
            _dump(redact_environment(rc.get_environment(args.id)))
            return 0
        # create
        env = rc.create_cloud_environment(args.id or "Default", **_fields(args.option))
        _dump(redact_environment(env))
    return 0


# --- scheduled tasks (triggers) -------------------------------------------------------
def cmd_triggers(args) -> int:
    from .triggers import trigger_prompt, trigger_url

    with _rc(args) as rc:
        a = args.action
        if a == "list":
            items = rc.list_triggers()
            if args.json:
                _dump(items)
            else:
                for t in items:
                    when = t.get("cron_expression") or t.get("run_once_at") or "-"
                    print(f"{t.get('id')}\t{'on ' if t.get('enabled') else 'off'}\t{when}\t{t.get('name', '')}")
            return 0
        if a == "create":
            if not args.env or not (args.cron or args.at):
                raise SystemExit("triggers create needs --env and --cron or --at")
            job: dict[str, Any] = {}
            if args.model:
                job["model"] = args.model
            if args.repo:
                job["repos"] = args.repo
            t = rc.create_trigger(
                args.target, environment_id=args.env, prompt=args.prompt,
                cron_expression=args.cron, run_once_at=args.at, **job,
            )
            _dump(t) if args.json else print(f"{t.get('id')}\t{trigger_url(t.get('id', ''))}")
            return 0
        if not args.target:
            raise SystemExit(f"triggers {a} needs a trigger id")
        if a == "get":
            t = rc.get_trigger(args.target)
            if args.json:
                _dump(t)
            else:
                print(f"{t.get('id')}\t{t.get('name', '')}\t{t.get('cron_expression') or t.get('run_once_at')}")
                print(f"prompt: {trigger_prompt(t) or '-'}")
            return 0
        if a == "update":
            fields = _fields(args.field)
            if args.name:
                fields["name"] = args.name
            if args.cron:
                fields["cron_expression"] = args.cron
            if args.at:
                fields["run_once_at"] = args.at
            _dump(rc.update_trigger(args.target, **fields))
            return 0
        if a == "enable":
            _dump(rc.enable_trigger(args.target))
        elif a == "disable":
            _dump(rc.disable_trigger(args.target))
        elif a == "run":
            _dump(rc.run_trigger(args.target))
        elif a == "runs":
            _dump(rc.trigger_runs(args.target, limit=args.limit or 10))
    return 0


# --- worker prompts ------------------------------------------------------------------
def _pick_pending(rc: RemoteControlClient, session_id: str, request_id: Optional[str], kinds: tuple[str, ...]):
    """The prompt to answer: ``request_id`` if given, else the newest pending
    one of ``kinds``. None when nothing matches."""
    pending = rc.pending_requests(session_id)
    if request_id:
        return next((p for p in pending if p.request_id == request_id), None)
    matching = [p for p in pending if p.kind in kinds]
    return matching[-1] if matching else None


def cmd_pending(args) -> int:
    with _rc(args) as rc:
        pending = rc.pending_requests(args.session_id)
    if args.json:
        _dump([{"request_id": p.request_id, "kind": p.kind, "request": p.request} for p in pending])
        return 0
    if not pending:
        print("nothing pending")
    for p in pending:
        print(f"{p.request_id}\t{p.kind}\t{p.tool_name or p.subtype}")
        for i, q in enumerate(p.questions):
            mode = "multi" if q.multi_select else "single"
            print(f"  [{i}] {q.question} ({mode}): {' | '.join(q.labels)}")
    return 0


def cmd_answer(args) -> int:
    kinds = ("question",) if args.action == "question" else ("permission",)
    with _rc(args) as rc:
        req = _pick_pending(rc, args.session_id, args.request_id, kinds)
        if req is None:
            if args.request_id and args.action in ("allow", "deny"):
                req = args.request_id  # answer blind: no input/tool name to echo back
            else:
                _print("no matching pending request")
                return 1
        if args.action == "allow":
            rc.allow_once(args.session_id, req, updated_input=_value(args.input) if args.input else None)
        elif args.action == "always":
            rc.always_allow(args.session_id, req)
        elif args.action == "deny":
            rc.deny_permission(args.session_id, req, args.message, interrupt=args.interrupt or None)
        else:
            if args.skip:
                rc.skip_questions(args.session_id, req)
            else:
                default = _question_key(args.question)
                selections: dict[Any, Any] = {}
                for key, label in _targeted(args.select, default):
                    selections[key] = label
                for key, label in _targeted(args.multi, default):
                    prior = selections.get(key)
                    selections[key] = [*(prior if isinstance(prior, list) else [prior] if prior else []), label]
                other = dict(_targeted(args.other, default)) or None
                if not selections and not other:
                    raise SystemExit("answer question needs --select, --multi, --other or --skip")
                rc.answer_questions(args.session_id, req, selections, other=other)
    print(f"answered {getattr(req, 'request_id', req)}")
    return 0


# --- commands, controls, setters ----------------------------------------------------------
def cmd_commands(args) -> int:
    with _rc(args) as rc:
        if args.action == "run":
            if not args.name:
                raise SystemExit("commands run needs a command name")
            rc.run_command(args.session_id, args.name, " ".join(args.args))
            print(f"sent /{args.name.lstrip('/')}")
            return 0
        cmds = rc.list_commands(args.session_id)
    if args.json:
        _dump([c.raw for c in cmds])
    else:
        for c in cmds:
            hint = f" {c.argument_hint}" if c.argument_hint else ""
            print(f"/{c.name}{hint}\t{c.description}")
    return 0


def cmd_control(args) -> int:
    fields = _fields(args.field)
    if args.data:
        fields.update(json.loads(args.data))
    with _rc(args) as rc:
        if args.no_wait:
            print(rc.send_control(args.session_id, args.subtype, **fields))
        else:
            _dump(rc.request_control(args.session_id, args.subtype, timeout=args.timeout, **fields))
    return 0


def cmd_model(args) -> int:
    with _rc(args) as rc:
        rc.set_model(args.session_id, args.value)
    print(f"model → {args.value}")
    return 0


def cmd_effort(args) -> int:
    effort = None if args.value in ("auto", "none") else args.value
    with _rc(args) as rc:
        out = rc.set_effort(args.session_id, effort, wait=6.0, command_fallback=True)
    print(f"effort → {args.value} (via {out.get('via')})")
    return 0


def cmd_mode(args) -> int:
    with _rc(args) as rc:
        rc.set_permission_mode(args.session_id, args.value)
    print(f"permission mode → {args.value}")
    return 0


def cmd_interrupt(args) -> int:
    with _rc(args) as rc:
        rc.interrupt_turn(args.session_id, cancel_queued=args.cancel_queued, reason=args.reason)
    print("interrupt sent")
    return 0


# --- account feed, TTS, uploads --------------------------------------------------------------
def _watch_account(args) -> int:
    with _rc(args) as rc:
        try:
            for w in rc.watch():
                if args.json:
                    print(json.dumps({"kind": w.kind, "session": w.session}, default=str), flush=True)
                elif w.kind in ("added", "changed", "removed"):
                    s = w.session
                    print(f"{w.kind:8} {s.get('id')}\t{s.get('status', '')}\t{(s.get('title') or '')[:60]}", flush=True)
                elif w.kind == "resync":
                    print("resync   (feed gap: re-list sessions)", flush=True)
        except KeyboardInterrupt:
            return 0
    return 0


def cmd_speak(args) -> int:
    text = args.text if args.text != "-" else sys.stdin.read()
    with _rc(args) as rc:
        ogg = rc.synthesize_speech(text, voice=args.voice, speed=args.speed)
    if args.output and args.output != "-":
        with open(args.output, "wb") as f:
            f.write(ogg)
        from .tts import ogg_duration_ms

        print(f"wrote {args.output} ({len(ogg)} bytes, {ogg_duration_ms(ogg) / 1000:.1f}s)", file=sys.stderr)
    else:
        sys.stdout.buffer.write(ogg)
        sys.stdout.buffer.flush()
    return 0


def cmd_upload(args) -> int:
    with _rc(args) as rc:
        if args.send:
            out = rc.send_message_with_files(args.send, args.message or "", [args.path])
        elif args.files_api:
            out = rc.upload_file(args.path)
        else:
            out = rc.upload_attachment(args.path)
    _dump(out)
    return 0


def _render_event(e) -> None:
    t = e.type
    if e.role == "assistant":
        body = _esc(e.text().strip())
        if body:
            _print(f"[green]claude[/green]> {body}" if _console else f"claude> {body}")
        for tu in e.tool_uses():
            name = _esc(tu.get("name"))
            _print(f"  [yellow]· tool[/yellow] {name}" if _console else f"  · tool {name}")
    elif e.role == "user":
        body = _esc(e.text().strip())
        if body:
            _print(f"[cyan]user[/cyan]> {body}" if _console else f"user> {body}")
    elif t == "result":
        _print(f"[dim]— turn complete ({e.subtype})[/dim]" if _console else f"— turn complete ({e.subtype})")
    elif t == "system" and e.subtype == "init":
        model = e.payload.get("model", "?")
        _print(f"[dim]· session init (model={model})[/dim]" if _console else f"· session init (model={model})")
    elif e.is_blocking_control:
        what = _esc((e.payload.get("request") or {}).get("tool_name") or e.control_subtype)
        _print(f"[magenta]⚠ needs you: {what}[/magenta]" if _console else f"⚠ needs you: {what}")


def _creds_parent() -> argparse.ArgumentParser:
    """Credential flags, accepted before or after the subcommand."""
    c = argparse.ArgumentParser(add_help=False)
    g = c.add_argument_group("credentials")
    g.add_argument("--token", default=argparse.SUPPRESS, help="claude.ai OAuth access token (else env CLAUDE_AI_ACCESS_TOKEN)")
    g.add_argument("--cswap-slot", dest="cswap_slot", default=argparse.SUPPRESS, help="claude-swap slot (access token only)")
    g.add_argument("--session-key", dest="session_key", default=argparse.SUPPRESS, help="claude.ai sessionKey cookie value")
    g.add_argument("--org", default=argparse.SUPPRESS, help="organization UUID (else looked up)")
    return c


def build_parser() -> argparse.ArgumentParser:
    creds = _creds_parent()
    p = argparse.ArgumentParser(prog="claude-rc", description="Claude Remote Control API client", parents=[creds])
    p.add_argument("--version", action="version", version=f"claude-rc {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def cmd(name: str, func, help: str, *, json_out: bool = False, session: bool = False) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help, parents=[creds])
        if session:
            sp.add_argument("session_id")
        if json_out:
            sp.add_argument("--json", action="store_true", help="raw JSON output")
        sp.set_defaults(func=func)
        return sp

    cmd("whoami", cmd_whoami, "show login / org / token status")
    cmd("auth", cmd_auth, "which auth source is used (cookie names only, offline)")

    pl = cmd("list", cmd_list, "list sessions", json_out=True)
    pl.add_argument("--limit", type=int, default=None)

    cmd("get", cmd_get, "session details (JSON)", session=True)

    pe = cmd("events", cmd_events, "recent events (history)", json_out=True, session=True)
    pe.add_argument("--limit", type=int, default=30)

    pw = cmd("watch", cmd_watch, "stream a session live, or (no id) the account-wide session feed", json_out=True)
    pw.add_argument("session_id", nargs="?")
    pw.add_argument("--from-seq", type=int, default=0, dest="from_seq")

    cmd("info", cmd_info, "summary, context, plan usage, git changes", session=True)

    pa = cmd("ask", cmd_ask, "ask a side question (no interruption, not in the transcript)", session=True)
    pa.add_argument("question")

    pn = cmd("rename", cmd_rename, "retitle a session", session=True)
    pn.add_argument("title")

    ps = cmd("send", cmd_send, "send a message and print the reply", session=True)
    ps.add_argument("message")

    cmd("repl", cmd_repl, "interactive chat with a live session", session=True)

    # -- lifecycle --
    pc = cmd("create", cmd_create, "start a cloud session", json_out=True)
    pc.add_argument("prompt", nargs="?", help="first message")
    pc.add_argument("--env", help="environment id (env_… or ccpool_…); default: the CLI's pick")
    pc.add_argument("--model")
    pc.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    pc.add_argument("--repo", help="owner/name or GitHub URL")
    pc.add_argument("--revision", help="branch/ref to check out")
    pc.add_argument("--branch", help="branch Claude pushes to (default claude/<slug>)")
    pc.add_argument("--title")
    pc.add_argument("--permission-mode", dest="permission_mode",
                    choices=["default", "plan", "acceptEdits", "bypassPermissions", "auto"])
    pc.add_argument("--append-system-prompt", dest="append_system_prompt")
    pc.add_argument("--tag", action="append", help="session tag (repeatable)")
    pc.add_argument("--config", action="append", metavar="K=V", help="extra config field (JSON value)")
    pc.add_argument("--defer-events", action="store_true", help="send the first message after create, like the web")

    cmd("archive", cmd_archive, "archive a session", session=True)
    cmd("unarchive", cmd_unarchive, "restore an archived session", json_out=True, session=True)
    cmd("delete", cmd_delete, "delete a session (route unverified)", session=True)

    pt = cmd("tag", cmd_tag, "add/remove session tags", json_out=True, session=True)
    pt.add_argument("--add", action="append")
    pt.add_argument("--remove", action="append")

    psh = cmd("share", cmd_share, "show (or --set) sharing", session=True)
    psh.add_argument("--set", action="append", metavar="K=V", help="share field to write (unverified)")

    pm = cmd("mark-read", cmd_mark_read, "mark a session read", session=True)
    pm.add_argument("--event-id", dest="event_id")
    pm.add_argument("--sequence-num", dest="sequence_num", type=int)

    # -- environments / triggers --
    pv = cmd("envs", cmd_envs, "environments: list | get ID | create NAME", json_out=True)
    pv.add_argument("action", choices=["list", "get", "create"])
    pv.add_argument("id", nargs="?", help="environment id (get) or name (create)")
    pv.add_argument("--kind", help="filter: anthropic_cloud, bridge, …")
    pv.add_argument("--option", action="append", metavar="K=V", help="create option (JSON value)")

    ptr = cmd("triggers", cmd_triggers, "scheduled tasks", json_out=True)
    ptr.add_argument("action", choices=["list", "get", "create", "update", "run", "enable", "disable", "runs"])
    ptr.add_argument("target", nargs="?", help="trigger id (name for create)")
    ptr.add_argument("--env")
    ptr.add_argument("--prompt")
    ptr.add_argument("--cron", help="5-field cron (UTC)")
    ptr.add_argument("--at", help="RFC3339 one-shot time")
    ptr.add_argument("--model")
    ptr.add_argument("--repo", action="append", help="git URL (repeatable)")
    ptr.add_argument("--name")
    ptr.add_argument("--field", action="append", metavar="K=V", help="update field (JSON value)")
    ptr.add_argument("--limit", type=int)

    # -- prompts / steering --
    cmd("pending", cmd_pending, "prompts waiting for an answer", json_out=True, session=True)

    pan = cmd("answer", cmd_answer, "answer a permission prompt or AskUserQuestion", session=True)
    pan.add_argument("action", choices=["allow", "always", "deny", "question"])
    pan.add_argument("--request-id", dest="request_id", help="default: the newest pending prompt of that kind")
    pan.add_argument("--input", help="allow: replacement tool input (JSON)")
    pan.add_argument("--message", default="The user denied this action.", help="deny: what the model is told")
    pan.add_argument("--interrupt", action="store_true", help="deny: also stop the turn")
    pan.add_argument("--question", help="question index/text the bare --select/--multi/--other apply to (default 0)")
    pan.add_argument("--select", action="append", metavar="[N:]LABEL", help="single-select choice")
    pan.add_argument("--multi", action="append", metavar="[N:]LABEL", help="multi-select choice (repeatable)")
    pan.add_argument("--other", action="append", metavar="[N:]TEXT", help='"Other" free text')
    pan.add_argument("--skip", action="store_true", help="skip the whole card")

    pco = cmd("commands", cmd_commands, "slash commands: list | run NAME [ARGS…]", json_out=True, session=True)
    pco.add_argument("action", choices=["list", "run"])
    pco.add_argument("name", nargs="?")
    pco.add_argument("args", nargs="*")

    pct = cmd("control", cmd_control, "send any control_request subtype", session=True)
    pct.add_argument("subtype")
    pct.add_argument("--field", action="append", metavar="K=V", help="request field (JSON value)")
    pct.add_argument("--data", help="request fields as one JSON object")
    pct.add_argument("--timeout", type=float, default=15.0)
    pct.add_argument("--no-wait", dest="no_wait", action="store_true", help="fire and print the request id")

    for name, func, help_ in (
        ("model", cmd_model, "set the model"),
        ("effort", cmd_effort, "set effort (low..max, or auto)"),
        ("mode", cmd_mode, "set permission mode (default, plan, acceptEdits, bypassPermissions)"),
    ):
        cmd(name, func, help_, session=True).add_argument("value")

    pi2 = cmd("interrupt", cmd_interrupt, "stop the running turn", session=True)
    pi2.add_argument("--cancel-queued", dest="cancel_queued", action="store_true")
    pi2.add_argument("--reason")

    # -- TTS / files --
    psp = cmd("speak", cmd_speak, "claude.ai text to speech (Ogg Opus)")
    psp.add_argument("text", help='text to speak, or "-" for stdin')
    psp.add_argument("-o", "--output", help="output .ogg file (default: stdout)")
    psp.add_argument("--voice", default="mellow")
    psp.add_argument("--speed", type=float, default=1.0)

    pu = cmd("upload", cmd_upload, "upload a file (chat attachment by default)")
    pu.add_argument("path")
    pu.add_argument("--send", metavar="SESSION_ID", help="also send it to this session")
    pu.add_argument("--message", help="text to send with --send")
    pu.add_argument("--files-api", dest="files_api", action="store_true", help="use /v1/files instead")

    pweb = cmd("web", cmd_web, "launch the browser control panel")
    pweb.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    pweb.add_argument("--port", type=int, default=8765, help="port (default: 8765)")
    pweb.add_argument("--no-open", action="store_true", help="don't open a browser window")
    pweb.add_argument("--verbose", action="store_true", help="log HTTP requests")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)  # live output when piped (watch/stream)
    try:
        return args.func(args)
    except CredentialsError as e:
        _print(f"auth error: {e}")
        return 1
    except (ControlRejected, ControlTimeout) as e:
        _print(f"worker: {e}")
        return 1
    except APIError as e:
        _print(_esc(f"API error {e.status}{f' ({e.error_type})' if e.error_type else ''}: {e.body[:300]}"))
        return 1
    except ValueError as e:
        _print(_esc(f"error: {e}"))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
