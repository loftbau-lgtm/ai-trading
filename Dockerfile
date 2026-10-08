FROM python:3.12-slim
WORKDIR /app
COPY engine.py server.py ./
COPY public ./public
RUN useradd -m quantlab && mkdir /data && chown quantlab:quantlab /data
USER quantlab
ENV PORT=8000 DATABASE_PATH=/data/quantlab.sqlite3 PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8000
CMD ["python", "server.py"]
