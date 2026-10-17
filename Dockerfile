FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 attendance \
    && useradd --uid 10001 --gid attendance --no-create-home attendance \
    && mkdir /app/data \
    && chown attendance:attendance /app/data
COPY attendance/ /app/attendance/
COPY main.py /app/main.py
USER 10001:10001
CMD ["python", "main.py"]
