#!/bin/bash
# Build and test the kitchen sink OpenHands sandbox image
# Run this script from the oh-settings directory

set -e

echo "=== Building openhands-sandbox image ==="
docker build -f Dockerfile.sandbox -t openhands-sandbox:latest .

echo ""
echo "=== Verifying image ==="
echo "Image size:"
docker images openhands-sandbox:latest --format "{{.Size}}"

echo ""
echo "=== Testing runtimes in container ==="
docker run --rm openhands-sandbox:latest bash -c '
echo "Python: $(python3 --version)"
echo ".NET:   $(dotnet --version)"
echo "Node:   $(node --version)"
echo "npm:    $(npm --version)"
'

echo ""
echo "=== Build successful! ==="
echo ""
echo "Next steps:"
echo "1. Stop current OpenHands:  docker-compose down"
echo "2. Start with new image:    docker-compose up -d"
echo "3. Check logs:              docker-compose logs -f openhands"
