FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY meshmonitor_bridge_homeassistant.py .

RUN useradd --system --no-create-home bridge
USER bridge

ENTRYPOINT ["python", "meshmonitor_bridge_homeassistant.py"]
