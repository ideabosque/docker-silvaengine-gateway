# Use Python 3.12 slim image for smaller size
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies and uv
RUN apt-get update && apt-get install -y \
    supervisor \
    curl \
    openssh-client \
    git \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && rm -rf /var/lib/apt/lists/*

# SSH setup — the gateway pulls private ideabosque repos over git+ssh.
# Drop a deploy key into ./.ssh before building (see README).
# The key is provisioned for BOTH root (build-time pip installs) and the
# runtime `gateway` user (uid 1000) — mcp_git.py spawns git/pip as `gateway`,
# which would otherwise fail with "Permission denied (publickey)".
ADD .ssh /root/.ssh
RUN chmod 700 /root/.ssh && \
    (chmod 600 /root/.ssh/* 2>/dev/null || true) && \
    ssh-keyscan github.com >> /root/.ssh/known_hosts && \
    mkdir -p /home/gateway/.ssh && \
    cp /root/.ssh/id_* /home/gateway/.ssh/ 2>/dev/null || true && \
    cp /root/.ssh/known_hosts /home/gateway/.ssh/known_hosts && \
    (cp /root/.ssh/config /home/gateway/.ssh/config 2>/dev/null || true) && \
    chmod 700 /home/gateway/.ssh && \
    (chmod 600 /home/gateway/.ssh/* 2>/dev/null || true)

# Add uv to PATH for all users
ENV PATH="/root/.local/bin:$PATH"

# Copy project dependency files
COPY requirements.txt requirements-modules.txt ./

# Create virtual environment and install dependencies using uv.
# 1. requirements.txt        -> third-party deps + shared SilvaEngine libs.
# 2. requirements-modules.txt -> the gateway (and any opted-in engine modules)
#    installed --no-deps: their metadata lists engines by bare name (not on
#    PyPI); their real deps are already satisfied by requirements.txt. By
#    default the engine modules run from the /app/src bind mount instead.
RUN uv venv /opt/venv && \
    uv pip install --python /opt/venv/bin/python -r requirements.txt && \
    uv pip install --python /opt/venv/bin/python --no-deps -r requirements-modules.txt

# Add virtual environment to PATH
ENV PATH="/opt/venv/bin:$PATH"

# Create supervisor log directory
RUN mkdir -p /var/log/supervisor

# Copy supervisor configuration
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Create non-root user and hand over the app + data directories
RUN useradd -m -u 1000 gateway && \
    mkdir -p /app/data && \
    chown -R gateway:gateway /app /home/gateway/.ssh

EXPOSE 8000

# Start supervisor as root (it drops privileges for the gateway process)
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
