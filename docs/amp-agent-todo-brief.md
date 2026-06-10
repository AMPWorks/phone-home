# Handoff brief — add todo via todo-handler skill

**For:** active amp-agent coding session
**Action:** Use the `todo-handler` skill to append the todo below to the amp-agent todo list. Do not implement the task itself — just file it.

## Todo to add

> **Title:** Wire up Action Button → Claude deep-link shortcut
>
> **Detail:** Integrate `make-claude-shortcut.sh` into infra (commit to the dotfiles/infra repo), then generate + sign the `amp-agent` shortcut and install it on the iPhone, bound to the Action Button.
>
> - Resume target: `claude://code/d6e0d8ac-a5e3-4048-8c39-89fb9884d835`
> - Generator: `./make-claude-shortcut.sh -n amp-agent -s d6e0d8ac-a5e3-4048-8c39-89fb9884d835`
> - Durable fallback (when the session ID is cleaned up server-side): `./make-claude-shortcut.sh -n amp-agent --new -r ampworksstudio/amp-agent -b main`
> - Signing is macOS-only (`shortcuts sign`); import via `open amp-agent.shortcut`, then Settings → Action Button → Shortcut.
>
> **Tags:** infra, ios, shortcuts
> **Priority:** low

## Notes

- The generator script already exists (produced this session); the todo covers committing it and doing the device-side install, not rewriting it.
- If the `todo-handler` skill expects a different field schema than the title/detail/tags/priority above, map accordingly — those are the semantics, not a fixed format.
