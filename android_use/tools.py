from starlette.routing import WebSocketRoute

from android_use import bridge


def register(mcp) -> None:
    mcp._additional_http_routes.append(WebSocketRoute(bridge.WS_PATH, bridge.ws_endpoint))
