FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev iputils-ping traceroute nmap \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x entrypoint.sh

# Standardized on 8000 everywhere (README, docker-compose host mapping,
# Traefik labels, VITE_API_BASE_URL) -- this used to say 7001 here while
# every other reference to the backend's port said 8000, so the port
# mapping in docker-compose silently pointed at nothing inside the
# container. Fixed to match the rest of the stack rather than changing
# every other reference to 7001.
EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]