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

The colours are the "sunset" set — orange `#f97316`, pink `#db2777`, violet `#7c3aed` — and
the file carries its own dark-mode tones (`#fb923c`, `#f472b6`, `#a78bfa`) behind a
`prefers-color-scheme` query, so one file reads on light and dark backgrounds without a
`<picture>` swap.
