from __future__ import annotations

import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def fetch_bytes(
    request: Request,
    *,
    timeout: int = 60,
    retries: int = 3,
    max_bytes: int = 20_000_000,
) -> bytes:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = response.read(max_bytes + 1)
            if len(payload) > max_bytes:
                raise ValueError(f"Response exceeds the {max_bytes}-byte limit")
            return payload
        except HTTPError as exc:
            if exc.code != 429 and exc.code < 500:
                raise RuntimeError(
                    f"HTTP {exc.code} while fetching {request.full_url}"
                ) from exc
            last_error = exc
        except (URLError, TimeoutError) as exc:
            last_error = exc

        if attempt + 1 < retries:
            time.sleep(min(2**attempt, 8))

    raise RuntimeError(
        f"Could not fetch {request.full_url} after {retries} attempts: {last_error}"
    ) from last_error
