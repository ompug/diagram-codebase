# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - Unreleased

### Added

- `/diagram-codebase` Claude Code skill with phased orchestration (scan, analysis,
  merge, plan, review, confirm, publish, summary) and options `--depth`, `--focus`,
  `--type`, `--dry-run`, `--resume`, `--update`, `--yes`, `--verify-visual`, `--out`.
- Deterministic scanners (standard library only): Python AST; JavaScript/TypeScript,
  C/C++ regex analyzers; ROS 2 (rclpy, rclcpp, launch files); FastAPI, Flask, Django,
  Express, Next.js routes; SQLAlchemy/Django ORM/SQL; Celery, pub/sub and asyncio
  queues; docker-compose, Dockerfile and Kubernetes manifests.
- Evidence-backed architecture model with per-evidence status, derived confidence, and
  an evidence checker that rejects Claude findings without verifiable citations.
- Diagram planner for master, subsystem, dataflow, execution, sequence, algorithm,
  state, dependency, infrastructure, ROS 2 and ERD diagrams, with readability limits,
  splitting and explicit notes for anything omitted.
- Mermaid builder and linter for Figma's `generate_diagram` rules, plus an optional
  Node-based Mermaid parser check.
- Label sanitizer that redacts credentials, e-mail addresses, home paths and private IPs,
  and a `publish/review.md` listing everything that leaves the machine.
- Figma driver (`next`/`record`) that places diagrams as FigJam sections with a legend
  and index, reconciles uncertain calls, and verifies the board.
- Local rate-limit budget (8/min, 160/day rolling windows by default) enforced by a
  global ledger, Claude Code hooks and the driver; resumable budget stops.
- Run manifest with resume and file-diff based incremental updates.
- Test fixtures, unit and integration tests (mocked Figma driver), CI workflow.
