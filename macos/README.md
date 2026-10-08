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
- A Keryx checkout on this Mac. The app's Setup Assistant walks the setup to a first call,
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

**While Keryx for Mac is open, Keryx answers calls.** The service under it, its launchd
agent `dev.keryx.agent` and the tunnel's, is the app's to manage, and never shown as one.

- **Opening it** starts Keryx, once setup has reached the step that needs it running. The
  app reads `keryx setup plan` and `keryx status`, and when the plan's service step is
  reachable and Keryx is not running, it runs `keryx service install --json`, which
  installs the agents (or loads them again) and starts them. Never before `keryx migrate`
  has run, never while a step it waits for (the phone, the voice, the agents) is left, never
  beside a `keryx serve` started by hand, and never without a service manager. It tries once
  a launch; a plan read later that reaches the step (the phone just set up) starts it then.
  If the start fails, the Overview's "Answering calls" row says why and offers Start.
- **Closing the window** (the red button, ⌘W) closes only the window. The app stays in the
  Dock, with its running dot, and in the menu bar, and Keryx goes on answering. Clicking
  the Dock icon opens the window again.
- **Quitting** (⌘Q, or Quit Keryx in the menu) quits the app and stops Keryx: `keryx service
  uninstall --json`, which unloads and removes both agents. The CLI has no stop that keeps
  them installed; removing them also means nothing starts Keryx at the next login unless
  the app opens then. The local model and voice servers are left as they are. Quitting asks
  first, "Quit Keryx?", saying that the assistant stops answering until Keryx is opened
  again, until you tick "Don't ask again"; with a call in progress it always asks, since
  quitting ends the call. A stop that takes longer than 30 seconds is cut short, and the
  app quits anyway. Quitting while a restart is pending (waiting for a call to end) stops
  Keryx before it restarts: `restart.json` stays pending until the next launch starts Keryx,
  whose start is taken as the restart, and confirmed with the usual call-back.
- **Logging out, restarting or shutting down** quits the app without asking and without
  stopping Keryx: launchd ends the session's agents itself, and an uninstall would only hold
  the logout up. The agents stay installed, so they load again at the next login, with or
  without the app.

Since quitting removes the agent's plist, which is where the app finds Keryx, the app keeps
a copy of the last one it read (in its own preferences) and reads that while the plist is
gone; see below.

A run with `KERYX_APP_ENVIRONMENT=inherit` (a sandbox) neither starts nor stops Keryx: its
`keryx` reads the sandbox's settings, but the launch agents it would install are the real
ones in `~/Library/LaunchAgents`.

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
   program, working directory and environment. While it is gone (the app quit, and removed
   it with the agent), the copy of it the app last read, as long as the checkout it names is
   still there. A `keryx` installed on its own (a
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
  Keryx, Restart Keryx, Pause Approval Calls, Settings, and Quit Keryx, with a footnote that
  the assistant stops answering calls when Keryx quits. The app is in the Dock too, for as
  long as it runs.
- **Overview**: whether Keryx is answering calls (Yes, No, Not yet while setup is left,
  Starting…), with Start only when a start failed or never ran and the unit's name only in
  the tooltip; calls in progress, the version running and whether a restart would load a
  newer one, the last restart, and a restart button; what `keryx doctor` says is still to
  set up, with Open Setup in Terminal; the approval calls switch and its history; and where
  the assistant's memory is.
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
- **It writes no Keryx file itself.** Every change is a `keryx` command, starting and
  stopping Keryx included (`keryx service install|uninstall`); it never runs `launchctl`.
  The one exception is starting the model server, which runs the checkout's
  `scripts/install-launchd.sh --llm`.
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
- `keryx setup plan --json`, `keryx service --json`, and `keryx service install|uninstall
  --json`, to start and stop Keryx
- `keryx config list --json`, `keryx config set`, `keryx config unset`
- `keryx restart`

`tests/test_macos_contract.py` checks that each JSON command still prints the keys the
Swift decoders require, so renaming a field fails the Python tests rather than the app.

## Now and later

| Now | Later |
| --- | --- |
| The menu bar: status, running and untold sessions, the local model, restart, pausing approval calls | Talking to the assistant at the Mac, once the local channel is merged |
| Overview: whether Keryx answers calls, the setup checks, approval calls, the memory's location | Live updates in place of polling |
| Sessions as a timeline, from what you asked to the result | Continuing a session from the app |
| Local models: download, choose, start the server | The setup wizard in the app; today it opens in Terminal |
| Every setting by its title, with secrets on stdin | Answering approvals from the Mac, which needs an owner ruling first |
| Open at Login, and finding Keryx from its launchd service | Plugins |
| Keryx answering while the app is open: started by itself, stopped by Quit | Keryx's background item named Keryx, from a helper in the app (`SMAppService.agent`) |
| Built from the checkout, installed in /Applications, signed with a local identity | A hosted relay sign-in |
| Two `.icns` icons, one for macOS 26 and one for 14 and 15 | A signed and notarised download, with a layered `.icon` |
