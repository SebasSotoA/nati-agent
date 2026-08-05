# sealed/

This directory holds a **commitment**, not a secret to read.

The exercise invites a teammate to find a place the SOP is incomplete. Exactly
one such gap was left open **on purpose**. To prove it was deliberate — planted
on day one, not rationalized after someone found it — the sha256 of a plaintext
description of the gap is committed here as `gap.sha256`, while the plaintext
itself is kept out of the repository until the exercise closes.

When the exercise ends, the plaintext (`gap-plaintext.md`) is published. Anyone
can then run:

```bash
sha256sum gap-plaintext.md        # must equal the hash in gap.sha256
```

A match proves the description existed, unchanged, from the start.

**No detector ships for this gap.** A `nati verdict` rule that caught it would
mean the SOP effectively covered it — which is the opposite of leaving an honest
gap. That absence is intentional and is itself part of the commitment.
