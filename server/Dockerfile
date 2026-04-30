FROM python:3.12-slim

WORKDIR /app

# net-snmp + iputils-ping are required by the SNMP probe / discovery code
# in app/api/v1/snmp.py. Without them, _snmpget()/_ping_once() silently
# return None and every Test SNMP attempt looks like a timeout.
RUN apt-get update \
    && apt-get install -y --no-install-recommends snmp iputils-ping \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
