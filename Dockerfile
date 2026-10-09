# Pozemni stanice: telemetrie PX4 z CSV pres REST.
#
# Drive to byl obraz ros:humble-ros-base, protoze vedle telemetrie bezel i
# ROS2 streamer, co posilal MP4 jako komprimovane snimky. Ten odpadl - video
# ted chodi jako MPEG-TS s KLV a zpracovava ho converter/ - takze z obrazu
# zmizel cely ROS2, OpenCV i cv_bridge (a s nimi ~2 GB) a staci cisty Python.
FROM python:3.12-slim

WORKDIR /app

# numpy uz neni potreba drzet na 1.x; ten zamek byl kvuli cv_bridge, ktery
# tady uz neni
RUN pip install --no-cache-dir fastapi uvicorn pandas

COPY server.py .

EXPOSE 8001

CMD ["python3", "server.py"]
