# Persistent Instructions

## Git Commit Format
- **Author:** Erik Taal <ET@debian> (always use `-m` with `--author="Erik Taal <ET@debian>"`)
- **Co-authored-by (LLM model):** Use `Opencode/{modelfamily} <opencode@local>` — adapt to current model family (e.g. `Opencode/Qwen3.6` for Qwen3.6-based models, `Opencode/Claude` for Claude-based models)
- **Co-authored-by (OpenCode):** `OpenCode <opencode@local>`
- **Directed-by:** `Directed-by: Erik Taal`

Example:
```bash
git commit \
  --author="Erik Taal <ET@debian>" \
  -m "feat: describe the change

Longer explanation here.

Co-authored-by: OpenCode <opencode@local>
Co-authored-by: Opencode/Qwen3.6 <opencode@local>
Directed-by: Erik Taal"
```

Always use Erik Taal as the author. The co-authors and directed-by are additional attribution lines.
