# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's
**"Report a vulnerability"** button (Security tab → Advisories) on this repository.
Do not open a public issue for security problems.

Include what you observed, steps to reproduce with a synthetic repository, and the
affected version or commit. Do not include real secrets or proprietary code; describe
their shape instead (for example "a token of the form `xyz_...` in a config default").

This is a volunteer-maintained project; we aim to acknowledge reports within a week and
will coordinate a fix and disclosure with you.

## Scope

In scope:

- **Secrets or personal data reaching Figma**: any credential, token, private key,
  e-mail address, home-directory path or private IP from the analyzed repository that
  survives sanitizing and appears in a diagram label, `mermaid/*.mmd`, or a `use_figma`
  script.
- Content sent to Figma that is not listed in `publish/review.md`, or a publish without
  the user's confirmation (when `--yes` was not given).
- The skill executing code from, or writing into, the analyzed repository outside its
  output directory.
- Path traversal or injection through repository file names, labels or arguments
  (including into generated JavaScript for `use_figma` or hook commands).
- The rate-limit hook gating unrelated Figma use when no run is active.

Out of scope:

- Claude reading the analyzed repository's code during analysis (inherent to any Claude
  Code session).
- Figma's or Claude Code's own services; report those to the respective vendor.
- Rate-limit estimates differing from Figma's actual quota (documented limitation).

## Supported versions

Only the latest release and the `main` branch receive security fixes.
