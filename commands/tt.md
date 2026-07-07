---
name: tt
description: "Time Tracker CLI: report / status / add / pause / resume / help. Runs in-plugin with no model turn; bare /time-tracker:tt prints help"
argument-hint: "[report|status|add|pause|resume|help] [args]"
---

# Time Tracker — tt

This command is the palette-discoverable twin of the typed `tt ` sentinel. It is
normally **intercepted by the plugin's `UserPromptExpansion` hook** and executed
in-plugin (report / status / add / pause / resume / help) with **no model turn** — the
same way `tt <cmd>` works when typed directly. Running `/time-tracker:tt` with no
argument prints the help.

## Fallback (only if the hook did not run)

If you are reading this, the `UserPromptExpansion` hook did not intercept the
command (e.g. the plugin's hooks are not active). Unlike the normal in-plugin
path, this fallback **does cost a model turn** — you are the model running it.
Run the dispatcher yourself and relay its `reason` field to the user:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/tt-dispatch.sh" "$ARGUMENTS"
```

For `report` specifically you may instead run the engine directly:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py" $ARGUMENTS
```
