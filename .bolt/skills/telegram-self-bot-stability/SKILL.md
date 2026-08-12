---
name: telegram-self-bot-stability
description: Stability engineering rules for the Telegram Self Bot. Use when investigating freezes, stuck asyncio operations, AI/Save stability, watchdogs, task cancellation, diagnostics, or runtime recovery.
---

# Telegram Self-Bot Stability Engineering Skill

## Purpose
Use this skill whenever working on the Telegram Self Bot stability/freezing project.

The goal is to find and isolate operations that can become stuck without damaging legitimate long-running work.

## Mandatory workflow
1. Inspect the CURRENT repository before changing anything.
2. Verify existing files, functions, imports, and architecture. Never assume a previous change still exists.
3. Determine the MINIMUM file scope required.
4. Make only the requested stability changes.
5. Validate syntax and build.
6. Review the final diff.
7. Push the completed change to the TARGET repository and verify the remote commit.
8. Report exactly what was changed. Never claim a push succeeded unless it was verified.

## Core architecture
Preserve the existing architecture.

The RuntimeSupervisor / failsafe / watchdog system remains the recovery architecture.

Do NOT create:
- a second supervisor
- duplicate recovery systems
- blanket timeout systems
- random retry systems
- unnecessary background tasks
- duplicate handlers
- unrelated refactors

## Timeout principle
Only SHORT-LIVED operations that are expected to finish promptly may receive bounded execution.

NEVER add lifetime timeouts to intentionally long-lived tasks such as:
- Telethon update loops
- Telethon send/receive loops
- heartbeat
- keepalive
- failsafe monitor
- scheduler supervisors
- helper supervisor
- web server
- main runtime task
- other permanent service loops

Do not kill a task merely because it has existed for a long time.

## Save / large-file rule
Large file uploads/downloads are legitimate long-running operations.

NEVER put a blanket total-operation timeout around a large Save operation or large Telegram file transfer.

Instead distinguish:
1. legitimate long-running file transfer
2. short internal operation that unexpectedly becomes stuck

Protect only the second category.

Do not sacrifice Save reliability for stability.

## AI rule
AI is a suspected trigger, NOT a proven cause.

Inspect the AI path before modifying it.

Prefer bounding individual operations that should finish promptly, such as:
- provider/network calls when appropriate
- database operations
- individual Telegram API operations
- short-lived helper operations

Do NOT automatically put one timeout around the entire AI request if that could kill legitimate long responses.

The purpose is both protection and diagnostic isolation.

## Cancellation rules
When a bounded operation times out:
1. emit a structured diagnostic
2. cancel the stuck operation safely
3. allow normal asyncio stack unwinding
4. allow async context managers and locks to release normally
5. let existing recovery architecture handle actual runtime failure

NEVER manually release asyncio locks.

NEVER swallow CancelledError.

Do not turn cancellation into a fake success.

## Diagnostics
Diagnostics should identify failures precisely without flooding Render logs.

Useful fields include:
- operation name
- elapsed time
- configured timeout
- runtime state
- client generation
- cancellation requested
- cancellation completed
- AI stage/provider when relevant
- Save operation context when relevant

Prefer the existing tracer/diagnostics architecture.

## Existing watchdog
If `backend/runtime/operation_watchdog.py` exists, inspect and reuse it instead of creating another watchdog.

If `backend/db/client.py` already routes short DB operations through the watchdog, preserve that architecture.

Do not duplicate watchdog implementations.

## File scope
Only modify files genuinely required for the requested chunk.

Do NOT touch unrelated:
- commands
- UI
- Supabase schema
- environment configuration
- deployment configuration
- Telegram handlers
- Save logic
- AI logic

unless the requested stability fix specifically requires them.

## Validation checklist
Before reporting completion:
- Python syntax validation passes for every modified Python file
- production build passes if available
- imports/signatures are valid
- no duplicate functions were introduced
- no long-lived task received a lifetime timeout
- no blanket total timeout was added to large-file Save
- CancelledError is not swallowed
- final diff is minimal and relevant
- remote branch contains the intended commit

## GitHub
When the task specifies a repository, work ONLY in that repository.

For this project, the repository is supplied explicitly in each task. Never silently switch accounts/repositories.

Push to the requested branch, normally `main`, and verify the remote commit afterward.

## Interaction style
Do the work instead of merely describing how to do it.

Do not stop after creating files locally if the task explicitly requires a push.

Do not claim "done" based only on syntax validation.

If a tool limitation prevents a required action, state the exact limitation instead of pretending it happened.
