# Persistent Instructions

## Git Commit Format
- **Author:** Erik Taal <ET@debian> (always use `-m` with `--author="Erik Taal <ET@debian>"`)
- **Co-authored-by:** Add at end of commit message body
- **Directed-by:** Add at end of commit message body

Example:
```bash
git commit \
  --author="Erik Taal <ET@debian>" \
  -m "feat: describe the change

Longer explanation here.

Co-authored-by: OpenCode <opencode@local>
Co-authored-by: unsloth-Qwen3.6-35B-A3B-standard <unsloth@local>
Directed-by: Erik Taal"
```

Always use Erik Taal as the author. The co-authors and directed-by are additional attribution lines.
