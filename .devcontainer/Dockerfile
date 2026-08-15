FROM mcr.microsoft.com/devcontainers/python:3.12

RUN mkdir -p /home/vscode/.claude/projects/-workspaces-gns3-cloud-lab \
 && chown -R vscode:vscode /home/vscode/.claude

RUN apt-get update \
 && apt-get install -y --no-install-recommends shellcheck \
 && rm -rf /var/lib/apt/lists/*
