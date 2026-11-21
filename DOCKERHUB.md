# Tapo Enhance

Local-first web interface for viewing and downloading Tapo camera recordings.

## Run with Docker Compose

Download the project's compose.yaml and .env.example from the GitHub repository.
Copy .env.example to .env and configure the camera settings for your network.

```sh
docker compose up -d
```

The Compose configuration uses host networking so camera discovery can scan
the local network. Persistent named volumes retain application data and keys.

## Images

Use aaronrohrbacher/tapo-enhance:latest for the current main build, or select
a version tag such as aaronrohrbacher/tapo-enhance:0.1.2.

## Documentation and source

Full installation instructions, configuration, and troubleshooting:
https://github.com/AaronRohrbacher/tapo-enhance
