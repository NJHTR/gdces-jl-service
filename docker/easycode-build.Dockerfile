# easyCode 构建环境：Rust 1.95 + 系统依赖
# 宿主机没有 gcc/pkg-config/protoc，所以编译必须在容器里
FROM rust:1.95-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential pkg-config libssl-dev libsqlite3-dev \
      protobuf-compiler cmake git perl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV CARGO_TERM_COLOR=never \
    CARGO_INCREMENTAL=0 \
    CARGO_NET_RETRY=5

WORKDIR /src