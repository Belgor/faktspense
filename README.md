# new-project template

Minimal skeleton for starting a Claude Code–ready project. Copy it, fill in the stubs, start working.

Rationale and the "rule of three" for what to grow inside `.claude/` later: [project-templates pattern](https://belgor.github.io/pawok/claude-code/patterns/project-templates/).

## Use

```bash
cp -r <template-source>/new-project /path/to/my-new-project
cd /path/to/my-new-project
git init
```

Then:

1. Open `CLAUDE.md`, replace `<PROJECT_NAME>`, fill in each section. Delete sections you don't need.
2. Extend `.gitignore` for your stack — it only covers the Claude-specific entry.
3. Delete this `README.md` (or replace with your project's real README).

## Post-copy checklist

- [ ] `CLAUDE.md` filled in (scope, stack, commands, hard rules)
- [ ] `.gitignore` extended for stack
- [ ] First real task attempted — let friction drive `.claude/` additions
- [ ] After ~1 week: audit `.claude/`, delete unused, promote 3×-repeated patterns
