# Roadmap

## v0.2.0: local models

Run the work and the voice on your own hardware, so a call and what it starts can stay on
your machine.

- [ ] A local model as a third agent, next to Claude and Codex, chosen per task
- [ ] A local voice in place of the Realtime API: one speech-to-speech model, or speech
      recognition, a language model, and speech synthesis joined together
- [ ] The PIN, the keypad, the call-backs, and interruptions working on the local voice
- [ ] `keryx setup` and `keryx doctor` set up and check the local models

## v0.3.0: talking to it at a Mac

Say a wake word and talk to the assistant through the Mac's microphone and speaker, with no
phone and no Twilio. The work is on the
[`feat/local-wakeword`](https://github.com/wak31415/keryx/tree/feat/local-wakeword) branch.

- [ ] Test the wake word and the local channel on real hardware
- [ ] `keryx setup` and `keryx doctor` cover the local channel
- [ ] Merge `feat/local-wakeword` into `main` (macOS only at first)

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
