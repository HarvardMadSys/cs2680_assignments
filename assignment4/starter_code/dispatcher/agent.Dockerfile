# Agent container image for the CS2680 A3 harness (course code: every agent runs on this image,
# and the egress proxy too; the leaderboard builds it from its own copy).
# Build context = the assets dir holding wheels/ (see evaluation_scripts/prepare_images.sh):
#   docker build -f dispatcher/agent.Dockerfile -t cs2680-a3-agent "$A3_ASSETS"   (default ../.cache/cs2680_a3/assets)
# Offline-friendly: openai is installed from pre-downloaded wheels, no index access.
FROM python:3.11-slim
COPY wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links /wheels openai && rm -rf /wheels
