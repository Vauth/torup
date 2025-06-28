FROM python:3.9-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    git \
    libtorrent-rasterbar2.0 && \
    rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/vauth/torup .

RUN pip3 install --no-cache-dir -r requirements.txt

RUN mkdir ./downloads && chmod -R 777 /app

EXPOSE 8080
EXPOSE 6881-6891/tcp
EXPOSE 6881-6891/udp

# CMD python3 main.py > /dev/null 2>&1
CMD ["python3", "main.py"]
