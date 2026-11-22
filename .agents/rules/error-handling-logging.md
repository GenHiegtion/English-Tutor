---
trigger: model_decision
description: Error handling and logging standard for Python files — use logging instead of print, never swallow exceptions silently, log messages must carry context.
---

# Error Handling & Logging

- Use the standard `logging` module (`logging.getLogger(__name__)`) instead of `print()` for anything beyond a throwaway one-off script. Configure log level/format in one central place (e.g. `config/logging.py` or the app entrypoint), not per-module.
- Never write a bare `except:` or `except Exception: pass`. If an exception is genuinely expected and safe to ignore, catch the specific exception type and log why it's being ignored.
- Every caught exception that is logged or re-raised must include context: what operation was being attempted and with what relevant identifiers (e.g. `logger.error("Failed to fetch user %s from API", user_id, exc_info=True)`), not just the raw exception message.
- Don't catch exceptions only to re-raise the same exception with no added value (`except Exception as e: raise e`). Either let it propagate naturally, or wrap it in a more specific/domain exception type that adds meaning.
- Distinguish log levels correctly: `debug` for internal diagnostic detail, `info` for normal operational events, `warning` for recoverable/unexpected situations, `error` for failures that need attention, `critical` for failures that stop the whole process.
- Don't log and then also silently continue as if nothing happened when the failure should actually stop execution or be surfaced to the caller — decide explicitly whether to recover, retry, or propagate.