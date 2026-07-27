# Command Message Limits Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split dynamic Telegram command responses safely instead of failing when they exceed text or caption limits.

**Architecture:** Add a shared bot-local UTF-16 splitter using the existing digest length helper. Route every response that grows with configuration or harvested data through it. `/info` uses a 1,024-unit photo caption and emits report overflow as text replies.

**Tech Stack:** Python 3.12, python-telegram-bot, pytest.

## Global Constraints

- Text parts never exceed 4,096 UTF-16 units; photo captions never exceed 1,024.
- Preserve all content and order; do not truncate responses.
- Preserve HTML parsing and leave fixed-size confirmations alone.
- Keep `.claude/` local settings out of commits.

---

### Task 1: Add the shared splitter and reply helper

**Files:**
- Modify: `livery_tracker/bot.py`
- Test: `tests/test_bot_maintenance.py`

**Interfaces:**
- Produces: `_split_message(text: str, limit: int = SAFE_LIMIT) -> list[str]`.
- Produces: `_reply_parts(message, text: str, *, parse_mode=None, limit: int = SAFE_LIMIT) -> None`.

- [ ] Write tests for a 77-line response and one 5,000-unit line. Assert all output parts fit and joining them retains every character.
- [ ] Run `pytest tests/test_bot_maintenance.py -q`; verify failure because `_split_message` is absent.
- [ ] Implement line-boundary splitting with character-level fallback for a single oversized line; implement `_reply_parts` as one `reply_text` call per part.
- [ ] Re-run the targeted tests and verify they pass.
- [ ] Commit only `livery_tracker/bot.py` and `tests/test_bot_maintenance.py` as `Add shared Telegram-safe reply splitting`.

### Task 2: Route dynamic handlers through the shared utility

**Files:**
- Modify: `livery_tracker/bot.py`
- Test: `tests/test_bot_maintenance.py`

**Interfaces:**
- Consumes: `_split_message()` and `_reply_parts()` from Task 1.
- Produces: safe dynamic output from harvest reports, `/watchlist`, `/airports`, `/status`, and `/info`.

- [ ] Write command-level tests for 100 verbose airports, many active legs, a long harvest report, and a 5,000-unit photo-backed `/info` report.
- [ ] Run the tests and verify each fails through the existing direct send path.
- [ ] Route text replies through `_reply_parts`. For `/info`, send the first 1,024-unit part as the caption and send remaining parts as HTML text replies; if the photo fails, send every part as normal text.
- [ ] Run targeted tests and `pytest tests/ -q`; verify every emitted text/caption fits Telegram's limit and all content remains present.
- [ ] Commit the production and test changes as `Guard dynamic bot replies against Telegram limits`.
