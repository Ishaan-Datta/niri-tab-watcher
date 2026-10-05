# niri-tab-watcher

`niri-tab-watcher` is a small, dependency-free Python daemon that watches
niri's IPC event stream and restores the previous stable window. It remembers
the direction from which a tiled window was focused so it can recreate the
same scrolling-layout orientation instead of merely focusing the right window
ID.

## How It Works

The daemon has two notions of focus:

- **Observed focus** changes immediately with niri's `WindowFocusChanged`
  event. This makes `restore` responsive even during the debounce period.
- **Stable focus** changes only after the new window remains focused for 750
  ms. Short accidental focus changes do not enter the previous-window history.

The departing window is snapshotted immediately, before waiting for the
debounce timer. If the new focus survives the timer, that snapshot becomes the
previous window. If focus returns or moves elsewhere first, the uncommitted
window is discarded.

For a normal tiled window that was entered from the right, restoration first
focuses the closest suitable column on its right and then focuses the target.
Those actions are one daemon-managed transaction, so the temporary anchor
cannot pollute focus history. Floating and effectively full-width windows are
focused directly.

Niri also does not currently expose a single IPC boolean covering full-width
columns, maximize-to-edges, and fullscreen. The daemon treats a tile whose
width is at least 90% of its output's logical width as effectively expanded and
skips anchor correction for it. This ratio is configurable with
`--expanded-width-ratio`.

## Installation

<!-- TODO: add example of installation using overlay -->

## Configuration

Bind a normal niri action to the short-lived client command:

```kdl
binds {
    Mod+Tab { spawn "niri-tab-watcher" "restore"; }
}
```

`restore` exits nonzero and writes a short error to stderr if the daemon is not
running, no stable transition has been recorded, or niri rejects an action.

## Niri IPC Limitation

In niri 26.04, `WindowLayout.tile_pos_in_workspace_view` is always `null` for
tiled windows. Niri also does not expose the scrolling workspace's viewport
offset. Therefore, the daemon can record column/row positions and infer the
viewport orientation from observed focus transitions, but it cannot directly
observe an arbitrary viewport offset.

This matters in two cases:

- The daemon starts while the desired viewport is already open and has not
  observed how it was established.
- The viewport is manually panned without a focus change.

In those cases the daemon uses a safe direct-focus fallback, which may not
recreate the exact set of neighboring windows.

Keep an eye on [niri PR #4147](https://github.com/niri-wm/niri/pull/4147),
**ipc: expose workspace scrolling view position**. It proposes a
`Workspace::scrolling_view_pos` field and a `WorkspaceViewPosChanged` event,
which would let this daemon record tiled viewport positions accurately without
per-window event fan-out. As of September 11, 2026, that PR is still open and
the API is not present in upstream niri.
