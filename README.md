# got-anything-else

Ask every un-archived Claude Code session "got anything else?" before archiving.

For each session that is NOT archived in the VS Code extension, this resumes it
once, read-only, with one open-ended question, and saves the reply verbatim to
`responses/` (gitignored). It never archives anything and never writes to VS
Code's state.

```sh
python got_anything_else.py list                  # show candidates, change nothing
python got_anything_else.py run --only <id>       # ask one session
python got_anything_else.py run                   # ask all candidates, biggest first
python got_anything_else.py run --limit 5         # ask at most 5 this time
python got_anything_else.py run --max-session 75  # stop at 75% of the 5-hour limit
```

Resumable: sessions that already have a response file are skipped, and the run
stops cleanly when Claude reports a usage or rate limit.

Close any VS Code tabs on the sessions first. Resuming a big session re-sends
its whole conversation, so this uses a lot of tokens quickly.

Tested on Windows with VS Code and Claude Code 2.1.332. `python got_anything_else.py --help` lists every option.

## Gotchas

- **Relies on undocumented internals.** The archived list comes from the
  `hiddenSessionIds` field the VS Code extension keeps in its globalState
  (`state.vscdb`, key `Anthropic.claude-code`). Usage percentages come from
  the `rate_limit_event` messages in the CLI's `stream-json` output. Neither
  is a public interface, so a future version of Claude Code may break this.
- **Windows-first.** The script looks for VS Code's state in
  `%APPDATA%\Code\User\globalStorage\state.vscdb`. On macOS that's
  `~/Library/Application Support/Code/User/globalStorage/state.vscdb`, and on
  Linux `~/.config/Code/User/globalStorage/state.vscdb`. Change
  `VSCODE_STATE` in the script to match.

MIT licensed.
