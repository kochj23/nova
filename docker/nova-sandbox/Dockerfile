FROM python:3.12-slim
RUN pip install --no-cache-dir requests numpy pandas beautifulsoup4 httpx pyyaml lxml
RUN useradd -m sandbox
USER sandbox
WORKDIR /home/sandbox
ENTRYPOINT ["python3", "-c"]
