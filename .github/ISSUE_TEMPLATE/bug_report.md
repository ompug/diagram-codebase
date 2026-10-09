---
name: Bug report
about: Something went wrong in analysis, planning, Mermaid, or publishing
title: "[bug] "
labels: bug
---

<!--
Do NOT paste proprietary source code, secrets, or private Figma links.
Reproduce with a small synthetic repository where possible.
-->

**What happened**

**What you expected**

**Command**

```
/diagram-codebase ...
```

**Phase where it failed** (scan / analysis / merge / plan / confirm / publish / summary)

**Environment**

- Claude Code version:
- Python version (`python3 --version`):
- OS:
- Figma connection (official plugin / remote MCP server / none, dry run):
- Node installed for mermaid-check? (yes / no)

**Artifacts** (from `.diagram-codebase/`; redact anything sensitive)

- [ ] `validation.json` attached
- [ ] relevant part of `manifest.json` or `REPORT.md`
- [ ] for Mermaid issues: `mermaid/<id>.mmd`
- [ ] output of `dc.py status` and, for rate-limit issues, `dc.py budget`

**Minimal reproduction** (synthetic files that trigger the problem)
