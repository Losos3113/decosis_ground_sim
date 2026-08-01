#!/bin/bash
source /opt/ros/humble/setup.bash

# Použije proměnnou SIMULATOR_PORT, pokud chybí, vypíše 8001
echo "=== Startuji FastAPI Telemetry Server na portu ${SIMULATOR_PORT:-8001} ==="
python3 /app/server.py &

echo "=== Startuji ROS2 Video Streamer ==="
python3 /app/video_streamer.py &

wait -n
