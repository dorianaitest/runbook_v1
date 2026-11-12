#!/bin/sh
# fetch-docs skips docs already on the volume; index embeds only new/changed chunks (no API call when nothing changed).
# Both must finish before serve: the local Qdrant index allows one process at a time.
set -e
voron-rag fetch-docs
voron-rag index
exec voron-rag serve --port 8765
