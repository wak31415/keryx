# Assets

Everything here is original work, drawn by hand as SVG for this repository and covered by
the project's [Apache-2.0 licence](../../LICENSE). No third-party image, icon set or font
is used or embedded — deliberately, so there is no attribution or licence question to
answer when the mark is copied into a slide, a favicon or a social preview.

| File | What it is |
|---|---|
| `keryx-mark.svg` | The mark: three rounded bars of a voice, the tallest in the middle. Geometry only — no text, so it needs no font and renders identically everywhere. |
| `keryx-mark-animated.html` | The mark in motion, one self-contained page to open in a browser. Idle is the still mark; speaking moves the bars with a voice; listening squashes them into one line, where the three trace the same voice line a little out of step; working bends them into a spinning loader. Each state morphs into the next. It is drawn in script rather than by hand, and pauses its looping motion for a viewer who asks for reduced motion. |

The colours are the "sunset" set — orange `#f97316`, pink `#db2777`, violet `#7c3aed` — and
the file carries its own dark-mode tones (`#fb923c`, `#f472b6`, `#a78bfa`) behind a
`prefers-color-scheme` query, so one file reads on light and dark backgrounds without a
`<picture>` swap.
