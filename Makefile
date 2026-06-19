.PHONY: build up down logs gateway-logs status restart shell health clean rebuild dev

# Build the Docker image
build:
	docker compose build

# Start the gateway in the background
up:
	docker compose up -d

# Build and start with live logs (foreground)
dev:
	docker compose up --build

# Stop and remove containers
down:
	docker compose down

# Tail combined logs
logs:
	docker compose logs -f

# Tail just the gateway process log inside the container
gateway-logs:
	docker exec silvaengine-gateway supervisorctl tail -f silvaengine-gateway

# Supervisor process status
status:
	docker exec silvaengine-gateway supervisorctl status

# Restart the gateway process without rebuilding the container
restart:
	docker exec silvaengine-gateway supervisorctl restart silvaengine-gateway

# Open a shell in the gateway container
shell:
	docker exec -it silvaengine-gateway /bin/bash

# Hit the public health endpoint
health:
	curl -f http://localhost:$${CONTAINER_PORT:-8000}/health

# Stop containers and drop volumes + dangling images
clean:
	docker compose down -v
	docker image prune -f

# Full rebuild from scratch
rebuild: clean build up
