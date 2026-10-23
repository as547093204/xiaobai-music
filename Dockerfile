FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 app
WORKDIR /home/app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=app:app server.py .
USER app
EXPOSE 7860
CMD ["python", "server.py", "--port", "7860", "--home", "/home/app/music"]
