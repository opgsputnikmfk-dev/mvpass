FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd -m analyst && mkdir /data && chown analyst:analyst /data
COPY --chown=analyst:analyst . .
USER analyst
ENV DATA_DIR=/data PYTHONUNBUFFERED=1 MPLCONFIGDIR=/tmp/matplotlib
EXPOSE 8765
CMD ["python", "bot.py", "serve", "--host", "0.0.0.0", "--telegram"]
