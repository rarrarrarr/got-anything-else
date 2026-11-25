"""Ask every un-archived Claude Code session "got anything else?" before archiving.

For each session that is NOT archived in the VS Code extension, this resumes it
once, read-only, with one open-ended question, and saves the reply verbatim to
responses/. It never archives anything and never writes to VS Code's state.

Usage:
    python got_anything_else.py list              # show candidates, change nothing
    python got_anything_else.py run --only <id>   # ask one session
    python got_anything_else.py run              # ask all candidates, biggest first
    python got_anything_else.py run --limit 5     # ask at most 5 this time
    python got_anything_else.py run --max-session 75  # stop at 75% of the 5-hour limit

Resumable: sessions that already have a response file are skipped, and the run
stops cleanly when Claude reports a usage or rate limit.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

PROMPT = (
    "I'm about to archive this session. Before I do, is there anything else\n"
    "you'd like to say? (You can read files this turn, but you can't change\n"
    "anything.)"
)

HERE = Path(__file__).resolve().parent
RESPONSES = HERE / "responses"
RUN_LOG = RESPONSES / "run.log"
PROJECTS = Path.home() / ".claude" / "projects"
VSCODE_STATE = Path(os.environ.get("APPDATA", "")) / "Code" / "User" / "globalStorage" / "state.vscdb"
VSCODE_KEY = "Anthropic.claude-code"

# Sessions whose transcript changed this recently may still be open somewhere.
RECENT_MINUTES = 10
TIMEOUT_SECONDS = 20 * 60

# Read-only lockdown, tested on a throwaway session:
#   --tools            only these built-in tools exist at all (no Write/Edit/Bash...)
#   --disallowedTools  deny again explicitly; deny beats any allow in a repo's settings
#   --strict-mcp-config with no --mcp-config: no MCP servers load
#   ENABLE_CLAUDEAI_MCP_SERVERS=false: no claude.ai connectors either
#   --permission-mode dontAsk + --permission-prompts none: anything not allowed is
#       refused, overriding a repo's auto/bypass default mode
#   --disable-slash-commands: no skills
#   disableAllHooks: repo hooks (which can run shell commands) don't fire
READ_TOOLS = "Read,Glob,Grep"
DENY_TOOLS = ",".join([
    "Bash", "PowerShell", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch",
    "Agent", "Task", "Skill", "ScheduleWakeup", "CronCreate", "CronDelete",
    "RemoteTrigger", "SendMessage", "ListAgents", "ToolSearch", "mcp__*",
])
LOCKDOWN_ARGS = [
    "--tools", READ_TOOLS,
    "--allowedTools", READ_TOOLS,
    "--disallowedTools", DENY_TOOLS,
    "--strict-mcp-config",
    "--permission-mode", "dontAsk",
    "--permission-prompts", "none",
    "--disable-slash-commands",
    "--settings", json.dumps({"disableAllHooks": True}),
]

LIMIT_PATTERN = re.compile(
    # e.g. "You've hit your 5-hour limit · resets 9:10pm" (missed by the first version)
    r"usage limit|rate limit|limit reached|hit your [\w -]{0,20}limit|hour limit|weekly limit|"
    r"limit will reset|resets at|resets \d|"
    r"\b429\b|overloaded|quota|credit balance",
    re.IGNORECASE,
)
# A session on a model that bills extra credits (e.g. Fable). Checked before LIMIT_PATTERN.
PAID_MODEL_PATTERN = re.compile(r"requires usage credits", re.IGNORECASE)


# ---------------------------------------------------------------- discovery

def folder_key(path: str) -> str:
    """The name Claude Code gives a working folder under ~/.claude/projects."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


@dataclass
class Session:
    id: str
    transcript: Path
    size: int
    modified: float
    title: str
    cwds: list[str]  # every cwd seen, most recent last
    run_dir: str | None = None
    skip: str | None = None  # reason, if it won't be asked
    rank: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def project_key(self) -> str:
        return self.transcript.parent.name

    @property
    def folder_name(self) -> str:
        return Path(self.run_dir or (self.cwds[-1] if self.cwds else self.project_key)).name


def read_transcript(path: Path) -> tuple[str, list[str]]:
    ai_title = custom_title = ""
    cwds: list[str] = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"ai-title"' in line or '"custom-title"' in line or '"cwd"' in line:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "ai-title" and obj.get("aiTitle"):
                    ai_title = obj["aiTitle"]
                elif obj.get("type") == "custom-title" and obj.get("customTitle"):
                    custom_title = obj["customTitle"]
                cwd = obj.get("cwd")
                if cwd and (not cwds or cwds[-1] != cwd):
                    cwds.append(cwd)
    return custom_title or ai_title or "(untitled)", cwds


def archived_ids() -> set[str]:
    """Session IDs the VS Code extension has archived (hidden). Read-only, from a copy."""
    if not VSCODE_STATE.exists():
        sys.exit(f"VS Code state file not found: {VSCODE_STATE}")
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "state.vscdb"
        shutil.copy2(VSCODE_STATE, copy)
        con = sqlite3.connect(f"file:{copy.as_posix()}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT value FROM ItemTable WHERE key = ?", (VSCODE_KEY,)).fetchone()
        finally:
            con.close()
    if not row:
        return set()
    value = row[0] if isinstance(row[0], str) else row[0].decode("utf-8")
    return set(json.loads(value).get("hiddenSessionIds", []))


def resolve_run_dirs(sessions: list[Session]) -> None:
    """Pick the folder to run each session from: one that exists AND whose key
    matches the folder holding the transcript (resume looks sessions up by it)."""
    known: dict[str, str] = {}
    parents: set[Path] = set()
    for s in sessions:
        for cwd in s.cwds:
            parents.add(Path(cwd).parent)
            if Path(cwd).is_dir():
                known.setdefault(folder_key(cwd), cwd)
    # Renamed folders: look at siblings of every folder seen, keeping the
    # parent's spelling (drive-letter case matters for the key).
    for parent in parents:
        if parent.is_dir():
            for child in parent.iterdir():
                if child.is_dir():
                    p = str(parent).rstrip("\\/") + os.sep + child.name
                    known.setdefault(folder_key(p), p)
    for s in sessions:
        s.run_dir = known.get(s.project_key)
        # The transcript's folder comes from where the session started; later cwds
        # may be subfolders it cd'd into. Drive-letter case varies, so ignore case.
        if s.run_dir and s.cwds and folder_key(s.cwds[0]).lower() != s.project_key.lower():
            s.notes.append(f"started in {s.cwds[0]}, which has since moved; running from {s.run_dir}")


def response_file_for(session_id: str) -> Path | None:
    hits = sorted(RESPONSES.glob(f"*-{session_id[:8]}.md"))
    return hits[0] if hits else None


def discover(exclude: set[str], trust_recent: bool = False) -> list[Session]:
    hidden = archived_ids()
    own_key = folder_key(str(HERE))
    sessions: list[Session] = []
    for transcript in PROJECTS.glob("*/*.jsonl"):  # top level only: no subagents
        sid = transcript.stem
        st = transcript.stat()
        title, cwds = read_transcript(transcript)
        sessions.append(Session(sid, transcript, st.st_size, st.st_mtime, title, cwds))
    resolve_run_dirs(sessions)
    sessions.sort(key=lambda s: s.size, reverse=True)
    now = time.time()
    for rank, s in enumerate(sessions, 1):
        s.rank = rank
        if s.id in hidden:
            s.skip = "archived"
        elif s.project_key.lower() == own_key.lower():
            s.skip = "this tool's own session"
        elif any(s.id.startswith(x) for x in exclude):
            s.skip = "excluded"
        elif response_file_for(s.id):
            s.skip = "already answered"
        elif not s.run_dir:
            s.skip = "no folder found for it (deleted or renamed?)"
        elif now - s.modified < RECENT_MINUTES * 60 and not trust_recent:
            s.skip = f"changed in the last {RECENT_MINUTES} min (still open?)"
    return sessions


# ---------------------------------------------------------------- running

def find_claude(override: str | None) -> str:
    if override:
        return override
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    # Fall back to the copy bundled with the VS Code extension (newest version).
    exts = sorted((Path.home() / ".vscode" / "extensions").glob("anthropic.claude-code-*"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    for ext in exts:
        exe = ext / "resources" / "native-binary" / ("claude.exe" if os.name == "nt" else "claude")
        if exe.exists():
            return str(exe)
    sys.exit("Can't find the claude CLI. Pass --claude <path>.")


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"
    # Don't let the child think it's nested in, or attached to, this terminal's session/IDE.
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"):
        env.pop(k, None)
    return env


def log(msg: str) -> None:
    RESPONSES.mkdir(exist_ok=True)
    line = f"{dt.datetime.now().isoformat(timespec='seconds')}  {msg}"
    print(line)
    with RUN_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:n].rstrip("-") or "untitled"


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


# Latest utilization seen, 0-100: {"five_hour": 42.0, "seven_day": 7.0}
USAGE: dict[str, float] = {}


def parse_stream(out: str) -> dict:
    """Read stream-json lines: return the final result message, and record utilization."""
    result: dict = {}
    for line in out.splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("type") == "result":
            result = msg
        elif msg.get("type") == "rate_limit_event":
            windows = (msg.get("rate_limit_info") or {}).get("unifiedWindows") or {}
            for name, w in windows.items():
                if isinstance(w, dict) and w.get("utilization") is not None:
                    USAGE[name] = round(float(w["utilization"]) * 100, 1)
    return result


def usage_text() -> str:
    if not USAGE:
        return "usage unknown"
    return ", ".join(f"{k.replace('_', '-')} {v:g}%" for k, v in sorted(USAGE.items()))


def probe_usage(claude: str) -> None:
    """One tiny Haiku call, not saved as a session, just to read current utilization."""
    cmd = [claude, "-p", "--model", "haiku", "--output-format", "stream-json", "--verbose",
           "--no-session-persistence", "--tools", "", "--strict-mcp-config",
           "--settings", json.dumps({"disableAllHooks": True})]
    try:
        proc = subprocess.run(cmd, input="Reply with just: ok", cwd=tempfile.gettempdir(),
                              env=child_env(), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
        parse_stream(proc.stdout)
    except subprocess.TimeoutExpired:
        pass


def over_budget(max_session: float | None, max_weekly: float | None) -> str | None:
    if max_session is not None and USAGE.get("five_hour", 0) >= max_session:
        return f"5-hour session usage {USAGE['five_hour']:g}% >= --max-session {max_session:g}%"
    if max_weekly is not None and USAGE.get("seven_day", 0) >= max_weekly:
        return f"weekly usage {USAGE['seven_day']:g}% >= --max-weekly {max_weekly:g}%"
    return None


def ask(claude: str, s: Session, deadline: dt.datetime | None = None,
        model: str | None = None) -> str:
    """Ask one session. Returns 'ok', 'error', 'limit' or 'deadline'."""
    # stream-json (needs --verbose) instead of json: same final "result" message,
    # plus rate_limit_event messages that carry the session/weekly utilization.
    cmd = [claude, "-p", "--resume", s.id, "--output-format", "stream-json", "--verbose",
           *LOCKDOWN_ARGS]
    if model:  # else the session's own model; some (e.g. Fable) need paid credits
        cmd += ["--model", model]
        s.notes.append(f"answered with --model {model}, not the session's own model")
    asked_at = dt.datetime.now().astimezone()
    timeout = TIMEOUT_SECONDS
    if deadline:
        timeout = min(timeout, max(1, (deadline - dt.datetime.now()).total_seconds()))
    log(f"ASK   #{s.rank} {s.id} [{s.folder_name}] {s.title!r} ({human_size(s.size)}) from {s.run_dir}")
    try:
        proc = subprocess.run(cmd, input=PROMPT, cwd=s.run_dir, env=child_env(),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        if deadline and dt.datetime.now() >= deadline:
            log(f"STOPPED #{s.rank} {s.id}: still running at the --stop-at time, killed; no response saved")
            return "deadline"
        log(f"ERROR #{s.rank} {s.id}: timed out after {TIMEOUT_SECONDS // 60} min")
        return "error"

    out, err = proc.stdout.strip(), proc.stderr.strip()
    data = parse_stream(out)
    reply = data.get("result") or ""
    failed = proc.returncode != 0 or data.get("is_error") or not reply or data.get("subtype") != "success"
    if failed:
        detail = (reply or err or out or f"exit code {proc.returncode}")[:500].replace("\n", " ")
        if PAID_MODEL_PATTERN.search(detail):
            log(f"PAID  #{s.rank} {s.id}: its model needs paid usage credits; nothing spent")
            return "paid-model"
        if LIMIT_PATTERN.search(detail):
            log(f"LIMIT #{s.rank} {s.id}: {detail}")
            return "limit"
        log(f"ERROR #{s.rank} {s.id}: exit {proc.returncode}, {detail}")
        return "error"
    if data.get("session_id") and data["session_id"] != s.id:
        log(f"NOTE  #{s.rank} {s.id}: reply went to a different session id {data['session_id']}")

    cost = data.get("total_cost_usd")
    usage = data.get("usage") or {}
    RESPONSES.mkdir(exist_ok=True)
    out_path = RESPONSES / f"{s.rank:03d}-{slug(s.folder_name, 25)}-{slug(s.title)}-{s.id[:8]}.md"
    header = [
        f"# {s.title}",
        "",
        f"- Session ID: `{s.id}`",
        f"- Folder: `{s.run_dir}`",
        f"- Transcript: {human_size(s.size)}, last modified "
        f"{dt.datetime.fromtimestamp(s.modified).isoformat(timespec='minutes')}",
        f"- Asked: {asked_at.isoformat(timespec='seconds')}",
    ]
    if cost is not None:
        header.append(f"- Cost: ${cost:.4f} (API-equivalent)")
    if usage:
        header.append(
            f"- Tokens: {usage.get('input_tokens', 0)} in, "
            f"{usage.get('cache_read_input_tokens', 0)} cache read, "
            f"{usage.get('cache_creation_input_tokens', 0)} cache write, "
            f"{usage.get('output_tokens', 0)} out")
    for note in s.notes:
        header.append(f"- Note: {note}")
    header += ["", "## Prompt", "", "```text", PROMPT, "```", "", "## Reply (verbatim)", "", ""]
    out_path.write_text("\n".join(header) + reply + "\n", encoding="utf-8")
    log(f"SAVED #{s.rank} {s.id} -> {out_path.name}"
        + (f" (cost ${cost:.4f})" if cost is not None else "") + f"; now {usage_text()}")
    return "ok"


# ---------------------------------------------------------------- commands

def print_table(sessions: list[Session], show_skipped: bool) -> None:
    rows = [s for s in sessions if show_skipped or not s.skip]
    for s in rows:
        when = dt.datetime.fromtimestamp(s.modified).strftime("%Y-%m-%d %H:%M")
        status = f"  SKIP: {s.skip}" if s.skip else ""
        print(f"{s.rank:>3}  {human_size(s.size):>9}  {when}  {s.id[:8]}  "
              f"{s.folder_name[:22]:<22}  {s.title[:60]}{status}")
        for note in s.notes:
            print(f"{'':>17}note: {note}")
    todo = [s for s in sessions if not s.skip]
    print(f"\n{len(todo)} to ask, {human_size(sum(s.size for s in todo))} of transcripts; "
          f"{len(sessions) - len(todo)} skipped.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["list", "run"])
    ap.add_argument("--only", help="ask just this session (ID or ID prefix)")
    ap.add_argument("--exclude", nargs="*", default=[], help="session IDs (or prefixes) to leave out")
    ap.add_argument("--limit", type=int, help="ask at most this many sessions this time")
    ap.add_argument("--all", action="store_true", help="list: also show skipped sessions")
    ap.add_argument("--claude", help="path to the claude CLI")
    ap.add_argument("--closed", action="store_true",
                    help=f"I've closed every session tab: don't skip sessions changed in the last {RECENT_MINUTES} min")
    ap.add_argument("--model", help="run: answer with this model instead of the session's own")
    ap.add_argument("--paid-model-fallback", default="opus", metavar="MODEL",
                    help="run: if a session's model needs paid credits, ask again with this model "
                         "(default: opus; pass '' to just skip those)")
    ap.add_argument("--max-session", type=float, metavar="PCT",
                    help="run: stop once 5-hour session usage reaches PCT%% (checked between sessions)")
    ap.add_argument("--max-weekly", type=float, metavar="PCT",
                    help="run: stop once weekly usage reaches PCT%% (checked between sessions)")
    ap.add_argument("--stop-at", metavar="HH:MM",
                    help="run: start nothing new after this local time today, and kill a session still running then")
    args = ap.parse_args()
    deadline = None
    if args.stop_at:
        h, m = map(int, args.stop_at.split(":"))
        deadline = dt.datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)

    sessions = discover(set(args.exclude), trust_recent=args.closed)
    if args.only:
        sessions = [s for s in sessions if s.id.startswith(args.only)]
        if not sessions:
            sys.exit(f"No session matches {args.only}")

    if args.command == "list":
        print_table(sessions, show_skipped=args.all or bool(args.only))
        return

    claude = find_claude(args.claude)
    todo = [s for s in sessions if not s.skip]
    for s in sessions:
        if s.skip and (args.only or s.skip not in ("archived",)):
            log(f"SKIP  #{s.rank} {s.id} [{s.folder_name}] {s.title!r}: {s.skip}")
    if args.limit is not None:
        todo = todo[: args.limit]
    log(f"START {len(todo)} session(s) to ask, using {claude}"
        + (f", stopping at {deadline:%H:%M}" if deadline else ""))
    budgeted = args.max_session is not None or args.max_weekly is not None
    if budgeted and todo:
        probe_usage(claude)
        log(f"USAGE before starting: {usage_text()}")
    done = 0
    for s in todo:
        if deadline and dt.datetime.now() >= deadline:
            log(f"STOP  reached --stop-at {deadline:%H:%M}; run again later to continue")
            break
        reason = over_budget(args.max_session, args.max_weekly)
        if reason:
            log(f"STOP  {reason}; run again later to continue")
            break
        result = ask(claude, s, deadline, args.model)
        if result == "paid-model" and args.paid_model_fallback:
            result = ask(claude, s, deadline, args.paid_model_fallback)
        if result == "limit":
            log("STOP  usage/rate limit reported; run again later to continue")
            break
        if result == "deadline":
            log(f"STOP  reached --stop-at {deadline:%H:%M}; run again later to continue")
            break
        done += result == "ok"
    log(f"END   {done} saved")


if __name__ == "__main__":
    main()
