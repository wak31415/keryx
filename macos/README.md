# Keryx for Mac

A menu bar app and a window for the Keryx install on this Mac. It shows whether the service
is running, what its agent sessions are doing, which local models fit and are running, and
every setting, and it can change them. It is a companion to `keryx serve`, not a
replacement: the service still answers the phone, runs the agents and serves the models,
and the app reads and changes Keryx by running `keryx` commands.

## Requirements

- macOS 14 or later. It is styled for macOS 26.
- A Swift 6 toolchain. The Command Line Tools are enough; Xcode is not needed.
- Keryx installed on this Mac, ideally as its launchd service (`scripts/install-launchd.sh`).

## Build, test, and install

```bash
macos/scripts/build-app.sh            # builds macos/build/Keryx.app
macos/scripts/build-app.sh --install  # and copies it to /Applications, replacing any copy
macos/scripts/test.sh                 # the Swift tests
```

`build-app.sh` makes a release build and wraps it in a bundle by hand, so no Xcode project
is involved. The version is the one in `pyproject.toml`, and the build number is the commit
count. The bundle is signed ad hoc and checked with `codesign --verify`. Nothing is
notarised: the app is built from the checkout, for the Mac it runs on, like the service.

To have Keryx open at login, install it to /Applications and turn on Open at Login in the
app's Settings (⌘,). It opens in the menu bar only, with no window. The login item is tied
to the copy's signature, so after a rebuild, turn it off and on again.

`test.sh` runs `swift test`. With only the Command Line Tools installed, Swift Testing lives
in a framework directory that `swift` does not search, so the script adds it then, and only
then. CI runs `swift build` and `test.sh` on `macos-latest`, where Xcode is the developer
directory.

### Snapshots

```bash
macos/scripts/snapshots.sh [--offscreen]
```

This draws every screen in the catalog, in light and dark, to
`macos/build/snapshots/<screen>-light.png` and `-dark.png`, with an `index.html` that lays
them side by side. With Screen Recording allowed for your terminal, each screen is shown in
a real window and photographed by its window id, so the glass sidebar and toolbar are in
the picture; only the app's own windows are captured, never the screen or another app.
Without the permission, or with `--offscreen`, the screens are drawn offscreen in borderless
windows, which needs no permission but leaves out the window chrome.

### The icon

```bash
swift macos/scripts/make-icon.swift  # from the repository root
```

This draws the mark and writes two files to `macos/Resources`. `AppIcon.icns` is full-bleed
square art for macOS 26 and later, which cuts it to its own shape and adds the edge light
and shadow. `AppIcon-legacy.icns` is an already-shaped tile for macOS 14 and 15. One file
cannot serve both: macOS 26 sets an already-shaped icon, shrunk, inside a second grey tile,
and a layered `.icon`, which can say which system it is for, needs Xcode to compile.
`build-app.sh` picks the file for the macOS it builds on. `--preview DIR` also writes each
size as a PNG.

## How it finds Keryx

An app opened from the Finder inherits no shell environment, and the service runs with its
installer's `KERYX_HOME` and XDG directories. A `keryx` found on some other PATH could read
different settings from the service's, so the app looks in this order:

1. **The folder chosen in the app's Settings**, when its `pyproject.toml` names `keryx`.
2. **The service's launchd plist**, `~/Library/LaunchAgents/dev.keryx.agent.plist`: its
   program, working directory and environment. A `keryx` installed on its own (a
   `uv tool install`) runs directly; a checkout is optional. A checkout runs its own
   `.venv/bin/keryx`, or else `uv run --project CHECKOUT --no-sync keryx`, never a plain
   `uv run`, which may sync the environment. `uv` is the plist's own, or the one in
   `~/.local/bin`, `/opt/homebrew/bin` or `/usr/local/bin`.
3. **The login shell**: the environment of a login `zsh`, then `command -v keryx` there,
   then `~/.local/bin/keryx`.

Each command runs in the checkout, because the CLI refuses to run while an old `.env` is in
its working directory; with no checkout it runs in `/`, as launchd runs the service, and
never in your home folder, where an unrelated `.env` would make the CLI refuse. When nothing
is found, the window says where it looked and offers to choose the folder.

`KERYX_APP_ENVIRONMENT=inherit`, in the environment the app is started with, is for sandbox
and development runs, and is never set in normal use. The app then uses its own process
environment in place of the login shell's, both to run each command and to find `keryx` on
its `PATH`, and it skips the service's launchd plist, whose environment is the real
installation's. Started from a shell that exports a sandbox's `KERYX_HOME` and XDG
directories, it reaches that sandbox and nothing else.

## What it shows

- **The menu bar**: the mark, with a dot when something needs you and a slash when Keryx is
  stopped or not found. The menu says which, and lists up to three sessions that are
  running or have results not yet told, the local model and any download. It has Open
  Keryx, Run Setup Wizard…, Restart Keryx, Pause Approval Calls, Settings, and Quit. The app has no Dock icon until a window is open.
- **Overview**: the service, calls in progress, the version running and whether a restart
  would load a newer one, the last restart, and a restart button; the Setup Checklist, what
  `keryx doctor` says is still to set up, each with the button that mends it, and Set Up
  Keryx…, which opens the setup wizard; the approval calls switch and its history; and where
  the assistant's memory is.
- **The setup wizard**: one frame at a time, from Welcome to You're Ready — moving Jarvis's
  files when there are any, who the assistant works for, where the voice runs and who does
  the work, the PIN, the phone line, then Checking, which finishes the downloads and installs
  the background service. It opens by itself once at launch while Keryx is not ready for a
  first call (Welcome's "Don't show this again" turns that off), skipping what is set; from
  the menu it shows every frame prefilled. Its last frame offers what can wait: Google,
  plugins, telling the assistant about yourself, briefs of your projects, issue reports.
- **Sessions**: every agent session, newest first. Each one opens as a timeline: what you
  asked, the agent's steps, any follow-ups, and the result. A running one is read again
  every 10 seconds while it is on screen. The last 30 days' totals are under the list.
- **Local Models**: what this Mac can run, the model the local agent uses and its server,
  every model in the catalog to download or choose, and the voice server. A download shows
  its progress, can be cancelled, and carries on while the window is closed.
- **Configuration**: every setting by its title, with its environment key as a caption, and
  a control for its type. Once a change is saved, a banner offers the restart that loads it.
  The PIN shows only whether one is set; it is changed with `keryx setup` in Terminal.

The status is polled every 30 seconds, and `keryx doctor` every five minutes. View ›
Refresh (⌘R) reads everything again.

## What it never does

- **It opens no port.** It talks to Keryx only through the CLI. The service's port 8080 is
  on the internet through the tunnel, so a second way in is ruled out, and the CLI already
  enforces the rules about who may change what.
- **It writes no Keryx file itself.** Every change is a `keryx` command. The one exception
  is starting the model server, which runs the checkout's `scripts/install-launchd.sh
  --llm`. The one program it installs that is not Keryx's is Cloudflare Tunnel, with
  `brew install cloudflared`, only when Homebrew is on this Mac and only on a click.
- **It never sets or shows the PIN.** `keryx setup`, which it opens in Terminal, is the only
  way to change one from the keyboard.
- **It never answers an approval.** Only the phone's keypad can. The app can pause the
  approval calls, which is `keryx approvals --disable`.
- **A secret goes on stdin.** It reaches `keryx config set KEY --stdin`, never argv or a
  log, and the app never shows it again.
- **It runs as the owner, never as the service.** It removes `KERYX_ACTOR` and
  `JARVIS_ACTOR` from every command's environment, because the service may change less
  than the owner at a terminal.

Commands are logged to the unified log (subsystem `dev.keryx.app`, category `commands`) as
the subcommand, its verb and its flags from fixed lists, with every other word shown as
`…`, plus the exit code and how long it took. A setting's key or value, a model name, a
task id, stdin, the environment and the output are never logged.

## The CLI's JSON surface

The app reads and changes Keryx through these commands:

- `keryx status --json`
- `keryx doctor --json`
- `keryx tasks list --json`, `keryx tasks show ID --json`, `keryx tasks usage --json`
- `keryx approvals --json`, and `--enable` / `--disable`
- `keryx models list --json`, `keryx models pull NAME --json` (JSON lines),
  `keryx models use NAME [--default-agent] --json`
- `keryx memory --json`
- `keryx config list --json`, `keryx config set`, `keryx config unset`
- `keryx setup plan --json`, `keryx setup mark STEP --json`
- `keryx setup profile [--preview|--apply] --json` (the answers as one JSON object on stdin)
- `keryx migrate --json`, and `keryx migrate --yes --json` (JSON lines)
- `keryx setup project-context --describe --json`, `--explore|--folder PATH --json` (JSON
  lines), and `--apply --json` (what is kept, on stdin)
- `keryx restart`

`tests/test_macos_contract.py` checks that each JSON command still prints the keys the
Swift decoders require, so renaming a field fails the Python tests rather than the app.

## Now and later

| Now | Later |
| --- | --- |
| The menu bar: status, running and untold sessions, the local model, restart, pausing approval calls | Talking to the assistant at the Mac, once the local channel is merged |
| Overview: the service, the setup checks, approval calls, the memory's location | Live updates in place of polling |
| Sessions as a timeline, from what you asked to the result | Continuing a session from the app |
| Local models: download, choose, start the server | A GGUF file of your own as the local model, from the setup wizard |
| The setup wizard, from Welcome to a first call | The voice's own model, the pipelines and the agents' test task, once the CLI has them |
| Every setting by its title, with secrets on stdin | Answering approvals from the Mac, which needs an owner ruling first |
| Open at Login, and finding Keryx from its launchd service | Plugins |
| Built from the checkout and signed ad hoc | A hosted relay sign-in |
| Two `.icns` icons, one for macOS 26 and one for 14 and 15 | A signed and notarised download, with a layered `.icon` |
