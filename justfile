# --- container ---

[group("container")]
up:
    #!/usr/bin/env sh
    command -v devcontainer >/dev/null 2>&1 || { echo "Run this from the host, not inside the container."; exit 1; }
    devcontainer up --workspace-folder .

[group("container")]
down:
    #!/usr/bin/env sh
    command -v docker >/dev/null 2>&1 || { echo "docker not found on PATH."; exit 1; }
    docker ps -q --filter label=devcontainer.local_folder="$(pwd -P)" | xargs -r docker stop

[group("container")]
destroy:
    #!/usr/bin/env sh
    command -v docker >/dev/null 2>&1 || { echo "docker not found on PATH."; exit 1; }
    docker ps -aq --filter label=devcontainer.local_folder="$(pwd -P)" | xargs -r docker rm -f

[group("container")]
shell:
    #!/usr/bin/env sh
    command -v devcontainer >/dev/null 2>&1 || { echo "Run this from the host, not inside the container."; exit 1; }
    devcontainer up --workspace-folder . && devcontainer exec --workspace-folder . bash

[group("container")]
claude *args:
    #!/usr/bin/env sh
    command -v devcontainer >/dev/null 2>&1 || { echo "Run this from the host, not inside the container."; exit 1; }
    devcontainer up --workspace-folder . && devcontainer exec --workspace-folder . claude

[group("container")]
rebuild:
    #!/usr/bin/env sh
    command -v devcontainer >/dev/null 2>&1 || { echo "Run this from the host, not inside the container."; exit 1; }
    devcontainer build --workspace-folder .
