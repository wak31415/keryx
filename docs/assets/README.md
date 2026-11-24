# Assets

Everything here is original work, made for this repository and covered by the project's
[Apache-2.0 licence](../../LICENSE). No third-party image or icon set is used, and no font
file is embedded, so there is no attribution or licence question to answer when the mark
is copied into a slide, a favicon or a social preview. The hero animation's few words are
drawn in Schibsted Grotesk and Fragment Mono, both under the SIL Open Font License, which
places no condition on images made with them.

| File | What it is |
|---|---|
| `keryx-mark.svg` | The mark: three rounded bars of a voice, the tallest in the middle. Geometry only — no text, so it needs no font and renders identically everywhere. |
| `keryx-hero-light.webp`, `keryx-hero-dark.webp` | The README's hero animation: the mark opens into three dots (you, the assistant, the agent), the call ends while the agent keeps working, and the call comes back. Animated WebP with a transparent background, one per colour scheme, picked with `<picture>`. Drawn on a canvas and recorded frame by frame. |
| `keryx-incoming-call.jpg` | A photo of a call-back arriving on a watch. |
| `keryx-call-flow-light.svg`, `keryx-call-flow-dark.svg` | A sequence diagram of one task across three calls: dispatched, a question answered on a call-back, and the result. The README showed it until the hero animation replaced it. |
| [`../../src/keryx/setup/slack_app_icon.png`](../../src/keryx/setup/slack_app_icon.png) | The mark as a 1024×1024 PNG on white, for the icon of your Slack app. It lives in the package, not here, so an installed Keryx has it: the connect steps print its path. |

The colours are the "sunset" set — orange `#f97316`, pink `#db2777`, violet `#7c3aed` — and
the file carries its own dark-mode tones (`#fb923c`, `#f472b6`, `#a78bfa`) behind a
`prefers-color-scheme` query, so one file reads on light and dark backgrounds without a
`<picture>` swap.

## The Slack app icon

A Slack app manifest cannot carry an icon, so it is uploaded by hand (Basic Information,
Display Information), and Slack takes a square image between 512 and 2000 pixels. The
file is the mark in its light colours on a solid white background, 1024×1024, the mark
centred and about 53% of the width and 67% of the height. It is drawn from the numbers
in `keryx-mark.svg` (three round-capped lines, 6 units wide in a 64-unit box) at four
times the size and scaled down, with ImageMagick:

```sh
convert -size 4096x4096 xc:white -fill none -strokewidth 432 \
  -stroke '#f97316' -draw "stroke-linecap round line 1184,1472 1184,2624" \
  -stroke '#7c3aed' -draw "stroke-linecap round line 2912,1472 2912,2624" \
  -stroke '#db2777' -draw "stroke-linecap round line 2048,896 2048,3200" \
  -filter Lanczos -resize 1024x1024 -depth 8 -strip \
  -define png:compression-level=9 -define png:exclude-chunk=all \
  src/keryx/setup/slack_app_icon.png
```

The app's `background_color` in the manifest is the mark's violet, `#7c3aed`: Slack sets
white text on it, and it is the one of the three with the most contrast against white.
