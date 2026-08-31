FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

# Section 7 hardening: don't write .pyc bytecode into the (now
# read_only) image filesystem, and don't buffer stdout/stderr -- logs
# must reach the container runtime immediately, not sit in a buffer
# that a read_only fs can't flush to disk as a backing file anyway.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev iputils-ping traceroute mtr-tiny nmap postgresql-client git libcap2-bin \
    && rm -rf /var/lib/apt/lists/* \
    # nmap's ping-sweep discovery (app.services.network_discovery_service)
    # needs raw-socket access. Rather than running as root or relying on
    # nmap's setuid bit (which `no-new-privileges` at the compose level
    # would neutralize anyway), grant the capability directly to the
    # binary via a file capability -- this works for the non-root
    # `netguard` user below as long as the `worker` service's compose
    # entry also adds NET_RAW/NET_ADMIN to the container's capability
    # bounding set (cap_add). Every other service keeps cap_drop: ALL
    # with no bounding-set exception, so this capability is inert there.
    && setcap cap_net_raw,cap_net_admin+eip "$(readlink -f $(command -v nmap))" \
    && setcap cap_net_raw+eip "$(readlink -f $(command -v ping))" \
    && setcap cap_net_raw+eip "$(readlink -f $(command -v traceroute))" \
    && setcap cap_net_raw+eip "$(readlink -f $(command -v mtr))"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x entrypoint.sh

# Section 7/8: run as a non-root, unprivileged user by default. The
# `worker` service (celery -Q celery, which runs nmap host-discovery
# sweeps -- see app.services.network_discovery_service) is the one
# exception that needs NET_RAW/NET_ADMIN added back at the compose
# level (cap_add), since nmap's ping-sweep techniques need it even as
# non-root; every other service using this same image keeps cap_drop:
# ALL with no exceptions. /app is owned by this user so entrypoint.sh's
# `alembic upgrade head` and the app itself can still run without root.
RUN groupadd -r netguard && useradd -r -g netguard -d /app -s /usr/sbin/nologin netguard \
    && chown -R netguard:netguard /app
USER netguard

# Standardized on 8000 everywhere (README, docker-compose host mapping,
# Traefik labels, VITE_API_BASE_URL) -- this used to say 7001 here while
# every other reference to the backend's port said 8000, so the port
# mapping in docker-compose silently pointed at nothing inside the
# container. Fixed to match the rest of the stack rather than changing
# every other reference to 7001.
EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]