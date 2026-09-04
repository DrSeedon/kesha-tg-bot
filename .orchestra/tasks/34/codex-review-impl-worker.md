<!-- codex-review-metadata: {"reviewer_model": "gpt-5.6-luna"} -->

## Summary

Reviewed the exact pinned diff `32b6d76...ab30ab2` → `ab30ab2`, limited to the seven requested files. I found no blocking, suggestion, or nit-level findings.

The admitted batch remains owned by `_run_batch` throughout compaction:

```python
compact_result = await self._compact_admitted_batch(batch)
if compact_result.get("ok"):
    reserve = await self._check_batch_context(combined)
    ...
elif compact_result.get("reason") == "usage_limit":
    ...
else:
    return
```

Compaction failures notify before returning, and finalization always restores the phase:

```python
except Exception as exc:
    logger.error(
        "Chat %s: admission compact failed: %s",
        self.chat_id,
        exc,
        exc_info=True,
    )
    await self._send_batch_terminal(batch, "context_auto_compact_failed")
    return {"ok": False, "reason": "exception"}
finally:
    async with self._lock:
        self._compact_started = False
        if self.phase == ChatPhase.COMPACTING:
            self.phase = ChatPhase.PROCESSING
```

The usage-limit path now proceeds to exactly one `_ask_fn` call, allowing the runtime to emit its authoritative terminal result.

## Findings (blocking/suggestion/question)

None.

I found no path in the changed implementation that:

- loses an admitted batch silently;
- sends an admitted batch twice;
- leaves a chat stuck in `COMPACTING` or `PROCESSING`;
- bypasses deferred-message draining;
- retries after a context-limit response in a way that could duplicate provider actions.

## Verdict

APPROVE
