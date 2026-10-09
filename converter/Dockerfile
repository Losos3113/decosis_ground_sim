FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY retransmitter.py state_holder.py klv_core.py \
     ground_link.py unified.py snapshot_sender.py \
     snapshot_store.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

RUN useradd --create-home --uid 1000 grounduser && \
    chown -R grounduser:grounduser /app
USER grounduser

ENTRYPOINT ["/app/entrypoint.sh"]
