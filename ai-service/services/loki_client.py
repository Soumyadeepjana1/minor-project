import logging
import os
from datetime import UTC, datetime, timedelta

import aiohttp

logger = logging.getLogger(__name__)

# Promtail labels pods with app / namespace / pod (see promtail-daemonset.yaml)
ERROR_PATTERN = "(?i)(error|fatal|critical|exception|traceback)"


class LokiClient:
    def __init__(self):
        self.base_url = os.getenv("LOKI_URL", "http://loki:3100")
        self.session = None

    async def _get_session(self):
        if self.session is None:
            self.session = aiohttp.ClientSession()
        return self.session

    def build_query(
        self,
        app_label: str,
        namespace: str | None = None,
        pod: str | None = None
    ) -> str:
        """Build a LogQL query: a stream selector plus a regex line filter.

        `|~` is the only valid way to match several patterns in one filter -
        chaining `|=` with `or` is not LogQL and Loki rejects it with a 400.
        """
        selectors = [f'app="{app_label}"']
        if namespace:
            selectors.append(f'namespace="{namespace}"')
        if pod:
            selectors.append(f'pod="{pod}"')

        return '{' + ", ".join(selectors) + '} |~ "' + ERROR_PATTERN + '"'

    async def fetch_recent_logs(
        self,
        app_label: str,
        minutes: int = 5,
        namespace: str | None = None,
        pod: str | None = None,
        limit: int = 100
    ) -> list[str]:
        """Fetch recent error logs from Loki, oldest first"""
        try:
            session = await self._get_session()

            # Calculate time range
            end_time = datetime.now(UTC)
            start_time = end_time - timedelta(minutes=minutes)

            query = self.build_query(app_label, namespace, pod)

            # Loki wants nanosecond timestamps, and aiohttp only accepts strings
            params = {
                "query": query,
                "start": str(int(start_time.timestamp() * 1_000_000_000)),
                "end": str(int(end_time.timestamp() * 1_000_000_000)),
                "limit": str(limit),
                "direction": "backward"
            }

            async with session.get(
                f"{self.base_url}/loki/api/v1/query_range",
                params=params
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    logs = self._parse_logs(data)
                    logger.info(
                        f"Fetched {len(logs)} log lines from Loki for query: {query}"
                    )
                    return logs
                else:
                    body = await response.text()
                    logger.error(
                        f"Loki query failed ({response.status}) for {query}: "
                        f"{body[:300]}"
                    )
                    return []

        except Exception as e:  # noqa: BLE001 - Loki problems must not stop the analysis
            logger.error(f"Failed to fetch logs from Loki: {e!s}")
            return []

    def _parse_logs(self, data: dict) -> list[str]:
        """Parse a Loki response into log lines ordered oldest to newest.

        The query runs `direction=backward` so Loki returns the newest entries
        first; callers slice the tail of this list to get the *most recent*
        lines, so the entries are re-sorted ascending here.
        """
        entries = []
        try:
            for stream in data.get("data", {}).get("result", []):
                for value in stream.get("values", []):
                    # value[0] is a nanosecond timestamp, value[1] is the log line
                    try:
                        timestamp = int(value[0])
                    except (TypeError, ValueError):
                        timestamp = 0
                    entries.append((timestamp, value[1]))
        except Exception as e:  # noqa: BLE001 - a bad response yields no logs
            logger.error(f"Failed to parse logs: {e!s}")

        entries.sort(key=lambda entry: entry[0])
        return [line for _, line in entries]

    async def health(self) -> dict[str, str]:
        """Check Loki is reachable and ready to serve queries"""
        try:
            session = await self._get_session()
            async with session.get(f"{self.base_url}/ready") as response:
                body = (await response.text()).strip()
                if response.status == 200:
                    return {"status": "healthy", "detail": body or "ready"}
                return {
                    "status": "unhealthy",
                    "detail": f"HTTP {response.status}: {body[:200]}"
                }
        except Exception as e:  # noqa: BLE001 - health check: any failure is unreachable
            return {"status": "unreachable", "detail": f"{self.base_url}: {e!s}"}

    async def close(self):
        """Release the shared HTTP session"""
        if self.session is not None:
            await self.session.close()
            self.session = None
