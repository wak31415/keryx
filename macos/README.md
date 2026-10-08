# Keryx for Mac

A menu bar app and a window for the Keryx install on this Mac. While it is open, Keryx
answers calls: it starts Keryx once setup allows, and quitting it stops Keryx. It shows what
the agent sessions are doing, which local models fit and are running, and every setting,
and it can change them. It is a front end to `keryx serve`, not a replacement: `keryx serve`,
under launchd, still answers the phone, runs the agents and serves the models, and the app
reads and changes Keryx by running `keryx` commands.

## Requirements

- macOS 14 or later. It is styled for macOS 26.
- A Swift 6 toolchain. The Command Line Tools are enough; Xcode is not needed.
- A Keryx checkout on this Mac. The app's setup wizard walks the setup to a first call,
  and the app starts Keryx under launchd by itself: there is nothing to install by hand.

## Build, test, and install

```bash
macos/scripts/make-signing-identity.sh  # once per Mac: the identity builds are signed with
macos/scripts/build-app.sh              # builds Keryx.app and puts it in /Applications
macos/scripts/build-app.sh --no-install # builds macos/build/Keryx.app and stops there
macos/scripts/test.sh                   # the scripts' tests, then the Swift tests
```

`build-app.sh` makes a release build and wraps it in a bundle by hand, so no Xcode project
is involved. The version is the one in `pyproject.toml`, and the build number is the commit
count. Nothing is notarised: the app is built from the checkout, for the Mac it runs on,
like the service.

It then puts the bundle in /Applications, where the Finder, Spotlight and Launchpad find it,
and touches nothing there but `/Applications/Keryx.app`. The new bundle is copied in beside
the old one, into a temporary folder of its own (`mktemp`), and its signature checked; the old one is renamed aside,
the new one renamed into place, and only then is the old one deleted, so a failure part way
leaves the old app where it was. An app called Keryx.app that is not Keryx (another bundle
identifier) is refused. If Keryx is running, quit it and open it again to run the new build.

**Signing.** Each build is signed with **Keryx Local Signing**, a self-signed code-signing
identity in your login keychain, when there is one and it is trusted for code signing; else
ad hoc, with a warning (and over ssh, where there is no login keychain). An ad-hoc signature is different on every build, and macOS ties
Open at Login and its background approval to the signature, so after each ad-hoc build they
must be turned off and on again. With the identity every build has the same designated
requirement, and they stay as they were.

`make-signing-identity.sh` makes the identity, once. It says what it is about to do and
asks first (`--yes` skips the question). It makes a key and a ten-year certificate for code
signing with `/usr/bin/openssl` in a private temporary folder, imports both into the login
keychain with the key non-extractable and usable by `/usr/bin/codesign`, and trusts the
certificate, chosen by its SHA-1, for code signing only (`security add-trusted-cert -p
codeSign`). The trust is needed: `codesign` refuses an untrusted self-signed identity ("no
identity found"). It is a setting of your user account's trust domain, so it applies to
every keychain, and macOS asks your password for it. The temporary files are deleted. Run
again, it changes nothing. The first build after it asks whether codesign may use the key:
choose Always Allow. `make-signing-identity.sh --remove` undoes it, the trust and then the
identity; by hand, that is `security remove-trusted-cert` on the certificate, then
`security delete-identity -Z SHA-1` in the login keychain.

`test.sh` runs `test-scripts.sh` first, which tests both scripts in a temporary folder:
which identity a build chooses (against a stand-in `security`), how Keryx.app is replaced
(with `KERYX_APP_DESTINATION` standing in for /Applications), and the identity script
against a keychain of its own (`KEYCHAIN`), with the trust step skipped
(`KERYX_SIGNING_TRUST=0`) since trust settings are per user, not per keychain. It never
touches /Applications or your keychains. Then it runs `swift test`. With only the Command
Line Tools installed, Swift Testing lives in a framework directory that `swift` does not
search, so the script adds it then, and only then. CI runs `swift build` and `test.sh` on
`macos-latest`, where Xcode is the developer directory.

To have Keryx open at login, turn on Open at Login in the app's Settings (⌘,). It opens in
the menu bar and the Dock, with no window, and starts answering calls.

## Opening, closing and quitting

**While Keryx for Mac is open, Keryx answers calls.** The launchd agents under it —
`dev.keryx.agent`, the tunnel's, and the model and voice servers' — are the app's to manage,
and never shown to you as such: the app says "Answering calls" and "Start Keryx", never
"service".

- **Opening it** starts Keryx, once setup has reached the step that needs it running. One
  rule decides, whoever asks — the launch, a plan read (the setup wizard's phone frame), the
  status poll, the wizard's Checking, the Setup Checklist's Start Keryx and the Overview's
  Start: the app reads `keryx setup plan` and `keryx status`, and when the plan's service
  step is reachable and Keryx is not running it starts it. An agent already installed (the
  last Quit stopped it) is started again with `keryx service start --all --json`, which
  starts the model and voice servers Quit stopped too; otherwise `keryx service install
  --json` installs the agents and starts them. Never before `keryx migrate` has run, never
  while a step it waits for (the phone, the voice, the agents) is left, never beside a
  `keryx serve` started by hand, never without a service manager, and never while a model
  server is being started or stopped (one `launchctl` at a time). A second request while a
  start is under way joins it. By itself it tries once a launch; until it has tried (the
  first `keryx status` could not be read at a login launch, say), the status poll looks
  again every 30 seconds. If the start fails, the Overview's "Answering calls" row says why,
  with what Keryx printed under Show Details, and offers Start.
- **Opening it again soon after quitting** waits for the last Quit's stop, if it is still
  finishing (see below): the app records that stop's process in its own preferences as it
  starts it, and the first start of the next launch waits for it, a minute at most, with
  "Finishing stopping…" in the Overview, so the start and the stop never run side by side.
  The two output files a stop that outlived the app left in the app's temporary folder are
  removed at launch.
- **Closing the window** (the red button, ⌘W) closes only the window. The app stays in the
  Dock, with its running dot, and in the menu bar, and Keryx goes on answering. Clicking
  the Dock icon opens the window again.
- **Quitting** (⌘Q, or Quit Keryx in the menu) quits the app and stops Keryx. It first ends
  editing in any field still being typed in, so a setting typed and not yet committed is
  saved, and waits for that like any other write. Then it runs `keryx service stop --all
  --json`, which stops every unit Keryx runs — the agent, the tunnel, the model server, the
  voice server and the voice LLM — and keeps each from starting at login, but leaves them
  installed, so macOS asks nothing and the next launch only starts them again. The stop runs
  detached: in a process group of its own, its output written to two owner-only files in the
  app's own temporary folder, with no timeout. The app waits up to 10 seconds for its answer
  and then quits whatever it says; the CLI finishes stopping on its own, however long each
  unit takes (up to a minute, its `ExitTimeOut`). Quitting asks first, "Quit Keryx?", saying
  that the assistant stops answering until Keryx is opened again, until you tick "Don't ask
  again"; with a call in progress it always asks, since quitting ends the call. A restart
  pending when you quit (waiting for a call to end) is settled by the stop as stopped by its
  owner, and the next launch's start is an ordinary start.
- **Logging out, restarting or shutting down** quits the app without asking and without
  stopping Keryx: launchd ends the session's agents itself, and a stop would only hold the
  logout up. The app still gives a write in progress a moment (5 seconds at most) and stops
  its own child processes. The agents stay installed and enabled, so they load again at the
  next login, with or without the app.

The app keeps a copy of the last launch agent plist it read (in its own preferences), which
is where it finds Keryx, and reads that if the plist is gone (removed at a terminal); see
below.

A run with `KERYX_APP_ENVIRONMENT=inherit` (a sandbox) never starts Keryx by itself: its
`keryx` reads the sandbox's settings, but the launch agents are the real ones in
`~/Library/LaunchAgents`. A click on a model, voice or voice LLM server still acts on them;
the CLI refuses when they belong to another `KERYX_HOME`.

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
2. **Keryx's launchd plist**, `~/Library/LaunchAgents/dev.keryx.agent.plist`: its
   program, working directory and environment. While it is gone (removed at a terminal with
   `keryx service uninstall`), the copy of it the app last read, as long as the checkout it
   names is still there. A `keryx` installed on its own (a
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
installation's. It neither starts nor stops Keryx, whose launch agents are the real ones. Started from a shell that exports a sandbox's `KERYX_HOME` and XDG
directories, it reaches that sandbox and nothing else.

## What it shows

- **The menu bar**: the mark, with a dot when something needs you and a slash when Keryx is
  stopped or not found. The menu says which, and lists up to three sessions that are
  running or have results not yet told, the local model and any download. It has Open
  Keryx, Run Setup Wizard…, Restart Keryx, Pause Approval Calls, Settings, and Quit Keryx,
  with a footnote that the assistant stops answering calls when Keryx quits. The app is in the Dock too, for as
  long as it runs.
- **Overview**: whether Keryx is answering calls (Yes, No, Not yet while setup is left,
  Starting…), with Start only when a start failed or never ran and the unit's name only in
  the tooltip; calls in progress, the version running and whether a restart would load a
  newer one, the last restart, and a restart button; the Setup Checklist, what `keryx
  doctor` says is still to set up, each with the button that mends it, and Set Up Keryx…,
  which opens the setup wizard; the approval calls switch and its history; and where the
  assistant's memory is.
- **The setup wizard**: one frame at a time, from Welcome to You're Ready — moving Jarvis's
  files when there are any, who the assistant works for and the PIN, where the voice runs
  and who does the work, the phone line, then Checking, which finishes the downloads and
  sets Keryx answering calls. It opens by itself at launch only until setup has once been
  seen through (Welcome's "Don't show this again" turns that off), skipping what is set;
  from the menu it shows every frame prefilled. Its last frame offers what can wait: Google,
  plugins, telling the assistant about yourself, briefs of your projects, issue reports.
- **Sessions**: every agent session, newest first. Each one opens as a timeline: what you
  asked, the agent's steps, any follow-ups, and the result. A running one is read again
  every 10 seconds while it is on screen. The last 30 days' totals are under the list.
- **Local Models**: what this Mac can run, the model the local agent uses and its server,
  every model in the catalog to download or choose, and the voice server. A download shows
  its progress, can be cancelled, and carries on while the window is closed.
- **Configuration**: every setting by its title, with its environment key as a caption, and
  a control for its type. Once a change is saved, a banner offers the restart that loads it.
  The PIN shows only whether one is set; it is set or changed in its own sheet.

The status is polled every 30 seconds, and `keryx doctor` every five minutes. View ›
Refresh (⌘R) reads everything again.

## What it never does

- **It opens no port.** It talks to Keryx only through the CLI. The service's port 8080 is
  on the internet through the tunnel, so a second way in is ruled out, and the CLI already
  enforces the rules about who may change what.
- **It writes no Keryx file itself.** Every change is a `keryx` command, starting and
  stopping Keryx included (`keryx service start|stop --all`, `keryx service install`) and the
  model servers (`keryx models start|stop`); it never runs `launchctl`. The one command it
  runs that is not `keryx` is `brew install cloudflared`, to install Cloudflare Tunnel, only
  when Homebrew is on this Mac and only on a click. The only files it writes are its own: its
  preferences, the detached stop's output in its temporary folder, and the scripts it hands
  Terminal.
- **It never shows the PIN.** It sets one only through its PIN sheet (`keryx pin set
  --stdin`, the digits typed twice and compared, never shown or logged), and replaces one
  only after two confirmations.
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
- `keryx setup plan --json`, `keryx service --json`, `keryx service install --json` and
  `keryx service start|stop --all --json`, to start and stop Keryx; `keryx service uninstall
  --voice-llm --json`
- `keryx models start [--voice-llm] --json` (JSON lines) and `keryx models stop --json`
- `keryx voice --json`, `keryx voice use openai|server URL --json`, `keryx voice use here
  --json`, `keryx voice download --json` and `keryx voice llm shared|here NAME|remote URL
  --json` (JSON lines)
- `keryx auth login NAME --json` and `keryx auth smoke NAME --json` (JSON lines), `keryx auth
  logout NAME --json`, `keryx integrations --json`
- `keryx pin --json`, `keryx pin set --stdin --json`
- `keryx phone numbers|tunnel|webhooks --json`, `keryx phone webhooks --apply --json`
- `keryx plugins list|hosts --json`, `keryx plugins install|remove|set NAME --json`, `keryx
  tools --json`
- `keryx config list --json`, `keryx config options KEY --json`, `keryx config set`,
  `keryx config unset`, `keryx config verify KEY --stdin --json`
- `keryx setup mark STEP --json`
- `keryx setup profile [--preview|--apply] --json` (the answers as one JSON object on stdin)
- `keryx migrate --json`, and `keryx migrate --yes --json` (JSON lines)
- `keryx setup project-context --describe --json`, `--explore|--folder PATH --json` (JSON
  lines), and `--apply --json` (what is kept, on stdin)
- `keryx restart`

`tests/test_macos_contract.py` checks that each JSON command still prints the keys the
Swift decoders require, and each stream the events and exit codes the app reads, so
renaming a field fails the Python tests rather than the app. It also pins the one figure the
app draws without the CLI printing it: the speech models' 3 GB share of the memory bar is
`VOICE_SERVER_GB`.

## Now and later

| Now | Later |
| --- | --- |
| The menu bar: status, running and untold sessions, the local model, restart, pausing approval calls | Talking to the assistant at the Mac, once the local channel is merged |
| Overview: whether Keryx answers calls, the setup checks, approval calls, the memory's location | Live updates in place of polling |
| Sessions as a timeline, from what you asked to the result | Continuing a session from the app |
| Local models: download, choose, start the server | A GGUF file of your own as the local model, from the setup wizard |
| The setup wizard, from Welcome to a first call, with the agent's test task | |
| The voice: OpenAI, a server of yours, or this Mac, with a voice LLM of its own and its pipelines | |
| Every setting by its title, with secrets on stdin | Answering approvals from the Mac, which needs an owner ruling first |
| Plugins, Integrations, the phone line | |
| Open at Login, and finding Keryx from its launchd agent | |
| Keryx answering while the app is open: started by itself, stopped by Quit | Keryx's background item named Keryx, from a helper in the app (`SMAppService.agent`) |
| Built from the checkout, installed in /Applications, signed with a local identity | A hosted relay sign-in |
| Two `.icns` icons, one for macOS 26 and one for 14 and 15 | A signed and notarised download, with a layered `.icon` |
