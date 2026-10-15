---
name: tt
description: "Time Tracker CLI: report / status / map / add / undo / pause / resume / help. Runs in-plugin with no model turn; bare /time-tracker:tt prints help"
argument-hint: "[report|status|map|add|undo|pause|resume|help] [args]"
---

# Time Tracker — tt

This command is the palette-discoverable twin of the typed `tt ` sentinel. It is
normally **intercepted by the plugin's `UserPromptExpansion` hook** and executed
in-plugin (report / status / map / add / undo / pause / resume / help) with **no model turn** — the
same way `tt <cmd>` works when typed directly. Running `/time-tracker:tt` with no
argument prints the help.

## Fallback (only if the hook did not run)

If you are reading this, the `UserPromptExpansion` hook did not intercept the
command (e.g. the plugin's hooks are not active). Unlike the normal in-plugin
path, this fallback **does cost a model turn** — you are the model running it.
Run the dispatcher yourself and relay its `reason` field to the user:

```bash
TT_PROJECT="$PWD" bash "${CLAUDE_PLUGIN_ROOT}/scripts/tt-dispatch.sh" "$ARGUMENTS"
```

`TT_PROJECT` names the current project, which `add`, `map` and `status` use.
`pause` and `resume` need the session id, which only the hook receives. From
this fallback the dispatcher refuses them and says to type `tt pause` at the
prompt. Relay that message as it is.

For `report` specifically you may instead run the engine directly:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py" $ARGUMENTS
```
