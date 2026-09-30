# Roadmap

## Moving to GPT-Live

[GPT-Live](https://developers.openai.com/api/docs/guides/live), released in September 2026,
is a promising fit for Keryx. It listens while it speaks, and it keeps the conversation
going while a backend works. It also splits the job the way Keryx already does: a voice in
front, and an agent behind it that does the reasoning and calls the tools.

I'm looking into moving Keryx onto it, and working out whether that can be done without
losing any features. Until then, Keryx runs on the Realtime API. It isn't as simple as
changing the model name, for three reasons.

### Tools

On GPT-Live, the voice doesn't call tools itself. Every tool goes through the delegated
backend. The PIN, the keypad, and the call-backs have to keep working there, and quick
answers such as "what's running?" have to stay quick.

### Timing and wording

Keryx needs to know when a spoken reply ends, so that it can hang up after a goodbye and
stop talking when you interrupt. GPT-Live has no event for the end of a reply. Keryx also
relies on exact wording, and GPT-Live paraphrases the text it's given.

### Instructions during a call

Giving the PIN changes what a call may do, and Keryx rewrites the voice's instructions to
match. GPT-Live fixes its instructions when the call starts, and after that it only accepts
additions.
