# Roadmap

## v0.2.0: local models

Run the work and the voice on your own hardware, so a call and what it starts can stay on
your machine.

- [x] A local model as a third agent, next to Claude and Codex, chosen per task
- [x] A local voice in place of the Realtime API: speech recognition, a language model, and
      speech synthesis joined together (Hugging Face's speech-to-speech)
- [ ] The PIN, the keypad, the call-backs, and interruptions working on the local voice
      (tool calls, announcements and interruptions work over the phone's audio path; a real
      call through Twilio is next)
- [x] `keryx setup` and `keryx doctor` set up and check the local models, and pick and
      download one that fits the machine

See [local-models.md](local-models.md).

## v0.3.0: talking to it at a Mac

Say a wake word and talk to the assistant through the Mac's microphone and speaker, with no
phone and no Twilio. The work is on the
[`feat/local-wakeword`](https://github.com/wak31415/keryx/tree/feat/local-wakeword) branch.

- [ ] Test the wake word and the local channel on real hardware
- [ ] `keryx setup` and `keryx doctor` cover the local channel
- [ ] Merge `feat/local-wakeword` into `main` (macOS only at first)

## Keryx for Mac

A menu bar app and a window for the Keryx install on a Mac, built from the checkout. It reads
and changes Keryx through the `keryx` CLI. See [macos/README.md](../macos/README.md).

- [x] The menu bar: whether Keryx is running or needs you, the sessions running or not yet
      told, the local model, a restart, and pausing the approval calls
- [x] An overview: the service, what `keryx doctor` says is left to set up, the approval
      calls, and where the memory is
- [x] Agent sessions as a timeline: what you asked, the agent's steps, the follow-ups, and
      the result
- [x] Local models: what fits the Mac, downloading one, choosing it, and starting its server
- [x] Every setting by its title, with secrets set on stdin and the PIN left to
      `keryx setup`
- [x] Finding Keryx from its launchd service, with or without a checkout, and opening at
      login
- [x] Icons for macOS 26 and for 14 and 15, and CI that builds and tests the app
- [ ] Talking to the assistant at the Mac, once the local channel above is merged
- [ ] Live updates in place of polling
- [ ] Continuing a session from the app
- [ ] The setup wizard in the app, rather than in Terminal
- [ ] Answering approvals from the Mac, which needs an owner ruling first: today only the
      phone's keypad approves
- [ ] Plugins
- [ ] A hosted relay sign-in
- [ ] A signed and notarised download, with a layered `.icon`

## Not scheduled: GPT-Live

[GPT-Live](https://developers.openai.com/api/docs/guides/live) listens while it speaks and
keeps talking while a backend works, which suits Keryx. I'm looking into moving onto it
without losing features. It isn't a model-name change:

- [ ] Tools go through GPT-Live's delegated backend, with the PIN, the keypad, and the
      call-backs intact, and quick answers still quick
- [ ] Keryx can tell when a reply ends, so it can hang up after a goodbye and stop when
      you interrupt, and the voice keeps the exact wording it's given
- [ ] Giving the PIN still changes the voice's instructions mid-call, though GPT-Live
      only accepts additions after the call starts
