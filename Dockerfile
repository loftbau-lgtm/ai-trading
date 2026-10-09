FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY engine.py server.py terminal.py scanner.py ./
COPY adaptive.py adaptive_portfolio.py adaptive_runner.py adaptive_validation.py adaptive_live.py adaptive_config.json adaptive_matrix.py adaptive_matrix_config.json ./
COPY microstructure.py shadow_execution.py public_streams.py microstructure_config.json ./
COPY directional_model.py directional_statistics.py directional_paper.py directional_config.json ./
COPY public ./public
RUN useradd -m quantlab && mkdir /data && chown quantlab:quantlab /data
USER quantlab
ENV PORT=8000 DATABASE_PATH=/data/quantlab.sqlite3 PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8000
CMD ["python", "server.py"]
