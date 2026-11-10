<p align="center">
  <img src="docs/assets/keryx-mark.svg" width="72" alt="">
</p>

<h1 align="center">Keryx</h1>

<p align="center"><strong>Call your computer. Hang up. It keeps working.</strong></p>

<p align="center">
  <a href="https://github.com/wak31415/keryx/actions/workflows/ci.yml"><img src="https://github.com/wak31415/keryx/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-6366f1.svg" alt="Apache-2.0"></a>
  <a href="https://pypi.org/project/keryx-voice/"><img src="https://img.shields.io/pypi/v/keryx-voice?color=6366f1" alt="PyPI"></a>
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/keryx-hero-dark.webp">
    <img src="docs/assets/keryx-hero-light.webp" width="880" alt="You speak, the word is carried to the agent, you hang up while it keeps working, and the call comes back to you.">
  </picture>
</p>

Call Keryx from your phone or watch and give it a task. Claude Code or Codex does the work on your
own machine, and Keryx calls you back when it's done or needs a decision.

> *"In the imaging project, run a learning-rate sweep. Call me when it's done, with your proposed next steps."*
>
> *"CI is red on main. Find out why and open a fix."*
>
> *"Add a tool so I can control my home automation setup from Keryx."*

## Quick install

```bash
curl -fsSL https://raw.githubusercontent.com/wak31415/keryx/main/install.sh | sh
```

This installs the [`keryx-voice`](https://pypi.org/project/keryx-voice/) package from PyPI
into an environment of its own, then opens the setup wizard, which asks only for what's
missing and starts the background service. Call your Twilio number to get started.
To update Keryx, run `keryx upgrade` or ask for an upgrade on a call. If a coding agent
is setting it up, point it to `keryx setup --agent-instructions`, or, in a clone, give
Claude Code the `skills/keryx-setup` skill.

To use the package in a project of your own, install it there with `pip install keryx-voice`.

**You'll need:**

- A Linux or macOS machine that stays on
- An OpenAI API key for voice
- A Claude or ChatGPT plan, or an API key, for the agent
- A Twilio phone number
- A tunnel so Twilio can reach your machine: Cloudflare Tunnel on Linux, ngrok on macOS

## What it does

- **Keeps working after you hang up.** Ask for status or add instructions on a later call.
- **Calls you back** with the result or when the agent needs an answer to proceed.
- **Answers Claude Code prompts on your screen** with a keypress on your phone.
- **Checks your email and calendar** by voice and sends you links and reports on Slack.
- **Grows by voice.** Ask for a new tool, or report a bug and Keryx files the issue.

<p align="center">
  <img src="docs/assets/keryx-incoming-call.jpg" width="360" alt="A Garmin watch showing an incoming call from Lyra.">
  <br>
  <sub>A call-back from Lyra on a watch.</sub>
</p>

<details>
<summary>How it compares with the Realtime API and ChatGPT Voice</summary>

| | Realtime API | ChatGPT Voice | Keryx |
| --- | :---: | :---: | :---: |
| Phone calls, including from watches | 🟡 SIP | 🟡 US/CA | ✅ |
| Tasks that continue after you hang up | — | ✅ | ✅ |
| Follow-ups in a later call | — | 🟡 | ✅ |
| Call-backs and questions mid-task | — | — | ✅ |
| Answer Claude Code prompts by phone | — | — | ✅ |
| Add a feature to itself during a call | — | — | ✅ |

</details>

> [!NOTE]
> We take security seriously. Keryx accepts calls only from your own phone numbers, and
> any action on your behalf requires your PIN. Please read the
> [security brief](SECURITY.md) before using it with anything confidential.

## Your own tools

Ask for a tool on a call, like *"add a tool that tells me how my website did this week"*,
and a coding agent writes it. Or write it yourself: one Python file with a decorated
function.

```python
import httpx
from keryx.tools.custom import custom_tool

STATS = "https://analytics.example.com/api/visitors?days=7"  # your provider
AUTH = {"Authorization": "Bearer your-api-key"}


@custom_tool(description="Site visitors this week, vs last week, by country.")
def site_traffic(ctx, args):
    week = httpx.get(STATS, headers=AUTH).json()
    return {"visitors": week["total"], "change": week["change_percent"],
            "top_countries": week["countries"][:3]}
```

Tools live in `~/.local/share/keryx/tools/`, outside the repository and readable only by
you. Keryx reads that folder on every call, so new tools are available on your next call
without a restart. A tool responds only after you enter your PIN unless it declares
`needs_pin=False`. Run `keryx tools` to list installed tools and any that would be rejected.
No clone needed. See [docs/tools.md](docs/tools.md#your-own-tools) for details.

## Developer Setup

Installed from a clone, the service runs your checkout. A call can change Keryx's
own code, restart it with the changes, and call you back to report whether it started.
The agent reads the code before filing bug reports. This requires [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/wak31415/keryx.git && cd keryx
uv sync
uv run keryx setup
```

To update, pull and run `uv run keryx restart`. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Docs

[Setup guide](https://github.com/wak31415/keryx/wiki/Setup),
[configuration](docs/configuration.md),
[coding agents](docs/agents.md),
[voice tools](docs/tools.md),
[roadmap](docs/roadmap.md),
[changelog](CHANGELOG.md).

Stuck? Run `keryx doctor`, then see
[Troubleshooting](https://github.com/wak31415/keryx/wiki/Troubleshooting).

<sub>*Keryx* (κῆρυξ) is Greek for herald. Apache 2.0. [Contributing](CONTRIBUTING.md).</sub>