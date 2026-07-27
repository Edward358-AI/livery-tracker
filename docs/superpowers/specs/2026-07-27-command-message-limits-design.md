# Command Message Limits Design

## Goal

No Telegram command may fail or silently omit data because its response exceeds
Telegram's 4,096 UTF-16-unit text limit or its 1,024-unit photo-caption limit.

## Design

Add a small, bot-local response utility that accepts text, a parse mode, and a
limit. It measures UTF-16 units using the existing digest helper, preserves
line boundaries where possible, and emits one or more messages in original
order. A continuation label is added only when a response needs multiple
parts.

The utility will be used by every response whose size grows with configuration
or harvested data: watchlist, airports, status, harvest completion reports,
and aircraft dossiers. Fixed confirmations and errors remain direct replies.

For `/info`, a photo is still sent when available. Its caption is limited to
Telegram's caption size; any remainder is sent immediately afterward as normal
messages, so no dossier detail is lost.

## Error Handling

The splitter never sends a part above its supplied limit. A single pathological
line longer than the limit is split safely rather than causing the complete
command to fail.

## Tests

Tests will cover a large watchlist, large airport/status/harvest replies, and
a long photo-backed `/info` report. Each asserts every part fits the applicable
Telegram limit and the combined output retains all original content.
