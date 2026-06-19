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
ADD .ssh /root/.ssh
RUN chmod 700 /root/.ssh && \
    (chmod 600 /root/.ssh/* 2>/dev/null || true) && \
    ssh-keyscan github.com >> /root/.ssh/known_hosts

# Add uv to PATH for all users
ENV PATH="/root/.local/bin:$PATH"

# Copy project dependency file
COPY requirements.txt ./

# Create virtual environment and install dependencies using uv.
# The engine modules are NOT installed (they run from the /app/src bind mount);
# requirements.txt provides their deps. The gateway is then installed --no-deps
# because its metadata lists the engines by bare name (not on PyPI); its real
# deps are already satisfied by requirements.txt.
RUN uv venv /opt/venv && \
    uv pip install --python /opt/venv/bin/python -r requirements.txt && \
    uv pip install --python /opt/venv/bin/python --no-deps \
        "git+ssh://git@github.com/ideabosque/silvaengine_gateway.git@main#egg=silvaengine_gateway"

# Add virtual environment to PATH
ENV PATH="/opt/venv/bin:$PATH"

# Create supervisor log directory
RUN mkdir -p /var/log/supervisor

# Copy supervisor configuration
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Create non-root user and hand over the app + data directories
RUN useradd -m -u 1000 gateway && \
    mkdir -p /app/data && \
    chown -R gateway:gateway /app

EXPOSE 8000

# Start supervisor as root (it drops privileges for the gateway process)
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
