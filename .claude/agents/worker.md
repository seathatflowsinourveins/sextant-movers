---
name: worker
description: The sextant-movers worker. Starts from the dispatch and task brief it is given, in its own worktree.
model: claude-opus-5-5
initialPrompt: "Read your dispatch and task brief under $MOVERS_STATE_DIR/tasks/, then report READY."
---
