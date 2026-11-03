# Small image that runs the Job Finder (finder.py).
# The program itself is NOT copied into the image: it lives in /opt/jobagent
# on the server and is mounted at /data, so you can edit it without rebuilding.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir requests

ENV TZ=Europe/London \
    PYTHONUNBUFFERED=1

CMD ["python", "/data/app/finder.py"]
