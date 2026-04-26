FROM ubuntu:latest

RUN apt-get update && apt-get install -y \
    build-essential \
    ninja-build \
    linux-tools-common \
    linux-tools-generic \
    cmake \
    sudo \
    ccache \
    libkrb5-3 \
    zlib1g-dev \
    libssl-dev \
    libicu-dev \
    gawk \
    bison \
    wget \
    flex \
    curl \
    jq \
    git \
    ca-certificates \
    python3 \
    python3-venv \
    software-properties-common
RUN (type -p wget >/dev/null || (sudo apt update && sudo apt install wget -y)) \
	&& sudo mkdir -p -m 755 /etc/apt/keyrings \
	&& out=$(mktemp) && wget -nv -O$out https://cli.github.com/packages/githubcli-archive-keyring.gpg \
	&& cat $out | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
	&& sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
	&& sudo mkdir -p -m 755 /etc/apt/sources.list.d \
	&& echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
	&& sudo apt update \
	&& sudo apt install gh -y

RUN useradd -u 1001 -m llvm-hackme
USER llvm-hackme
WORKDIR /home/llvm-hackme

RUN curl -LsSf https://astral.sh/uv/install.sh | sh
