"""Resource namespaces — thin wrappers that build paths and delegate to the client."""


class _Resource:
    def __init__(self, client):
        self._client = client


class Charts(_Resource):
    def get(self, symbol, *, timeframe=None, indicators=None):
        """OHLCV + indicator data for a symbol (see /api/chart-internal)."""
        params = {}
        if timeframe:
            params["timeframe"] = timeframe
        if indicators:
            params["indicators"] = indicators
        return self._client.get(f"/api/chart-internal/{symbol}", **params)


class Plays(_Resource):
    def list(self, *, status=None, asset=None, type=None,
             include_resolved=False, limit=50, offset=0):
        params = {"limit": limit, "offset": offset, "include_resolved": str(include_resolved).lower()}
        if status:
            params["status"] = status
        if asset:
            params["asset"] = asset
        if type:
            params["type"] = type
        return self._client.get("/api/plays", **params)

    def active(self):
        return self._client.get("/api/plays/active")

    def context(self):
        """Compact plays context for AI briefings."""
        return self._client.get("/api/plays/context")

    def get(self, play_id):
        return self._client.get(f"/api/plays/{play_id}")

    def create(self, **fields):
        return self._client.post("/api/plays", json=fields)

    def update(self, play_id, **fields):
        return self._client.put(f"/api/plays/{play_id}", json=fields)

    def delete(self, play_id):
        return self._client.delete(f"/api/plays/{play_id}")


class Alerts(_Resource):
    def list(self, *, active=True, triggered_since=None):
        params = {"active": str(active).lower()}
        if triggered_since:
            params["triggered_since"] = triggered_since
        return self._client.get("/api/alerts", **params)

    def create(self, **fields):
        return self._client.post("/api/alerts", json=fields)

    def delete(self, alert_id):
        return self._client.delete(f"/api/alerts/{alert_id}")

    def toggle(self, alert_id):
        return self._client.post(f"/api/alerts/{alert_id}/toggle")


class Composite(_Resource):
    def asset_detail(self, symbol, *, format=None):
        params = {"format": format} if format else {}
        return self._client.get(f"/api/asset/{symbol}/detail", **params)

    def live_positions(self, *, asset=None, format=None):
        params = {}
        if asset:
            params["asset"] = asset
        if format:
            params["format"] = format
        return self._client.get("/api/positions/live", **params)

    def batch(self, assets, *, include=None, format=None):
        if isinstance(assets, (list, tuple, set)):
            assets = ",".join(assets)
        params = {"assets": assets}
        if include:
            params["include"] = ",".join(include) if not isinstance(include, str) else include
        if format:
            params["format"] = format
        return self._client.get("/api/batch", **params)


class Scanner(_Resource):
    def list(self, *, verdict=None, direction="both"):
        params = {"direction": direction}
        if verdict:
            params["verdict"] = verdict
        return self._client.get("/api/scanner", **params)


class Usage(_Resource):
    def get(self, *, history=True, days=30):
        return self._client.get("/api/usage", history=str(history).lower(), days=days)


class Keys(_Resource):
    def list(self):
        return self._client.get("/api/keys")

    def create(self, name, *, scope="full"):
        return self._client.post("/api/keys", json={"name": name, "scope": scope})

    def revoke(self, key_id):
        return self._client.delete(f"/api/keys/{key_id}")
