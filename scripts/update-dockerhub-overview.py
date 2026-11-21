"""Upload the README and retain useful, credential-free Hub API errors."""
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def request(path, payload, token=None):
    headers = {"Content-Type": "application/json", "User-Agent": "tapo-enhance-ci"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # Standard JSON escapes preserve README content while avoiding false
    # positives in Docker Hub's Cloudflare request-body filtering.
    encoded = json.dumps(payload)
    encoded = "".join(
        f"\\u{ord(char):04x}" if char in "<>&/|;$`" else char
        for char in encoded
    )
    req = Request(
        f"https://hub.docker.com/v2/{path}",
        data=encoded.encode(), headers=headers,
        method="PATCH" if token else "POST",
    )
    try:
        with urlopen(req, timeout=60) as response:
            return response.read()
    except HTTPError as error:
        body = error.read().decode(errors="replace")[:4000]
        for secret in (os.environ["DOCKERHUB_TOKEN"], token):
            if secret:
                body = body.replace(secret, "[REDACTED]")
        raise SystemExit(
            f"Docker Hub {req.method} failed: HTTP {error.code}; "
            f"server={error.headers.get('Server', '')}; "
            f"cf-ray={error.headers.get('CF-Ray', '')}; response={body}"
        ) from None


def main():
    auth = json.loads(request("auth/token", {
        "identifier": "aaronrohrbacher",
        "secret": os.environ["DOCKERHUB_TOKEN"],
    }))
    token = auth["access_token"]
    print(f"::add-mask::{token}")
    overview = Path("README.md").read_text()
    if len(overview.encode()) > 25000:
        raise SystemExit("README exceeds Docker Hub's 25000-byte overview limit")
    request(f"repositories/{os.environ['DOCKERHUB_IMAGE']}/", {
        "description": "Local-first web interface for viewing and downloading Tapo camera recordings.",
        "full_description": overview,
    }, token)
    print("Docker Hub overview updated successfully")


if __name__ == "__main__":
    main()
