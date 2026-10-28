import base64
import json
import re
import tempfile
import time
from pathlib import Path

from android_use import bridge, fcm, store

from .common import CliError

ACTIONS = {
    "tap", "long_press", "double_tap", "type", "scroll", "swipe", "drag", "pinch", "gesture",
    "key", "open", "apps", "wait", "clipboard", "node_action",
}
INT_KEYS = {"ref", "index", "times", "duration_ms", "hold_ms", "timeout_ms", "settle_ms", "max_nodes"}
FLOAT_KEYS = {"x", "y", "x2", "y2", "value", "from_px", "to_px"}
BOOL_KEYS = {"exact", "append", "clear", "submit", "gesture", "gone", "hidden", "screen", "screenshot", "shot", "show_pairing", "full", "bounds", "keys", "diff"}
JSON_KEYS = {"intent", "strokes"}
ALIASES = {"shot": "screenshot", "pairing": "show_pairing", "dir": "direction", "app": "app", "name": "name"}
SHOT_DIR = Path(tempfile.gettempdir()) / "ca"

HELP_ANDROID = """android status [-pairing] | pair      phones, accessibility state, push wake-up, pairing code
android screen [-shot] [-hidden] [-bounds] [-keys] [max_nodes=N]   compact UI tree: `[ref] role "label" flags`; role omitted for plain text; id/@x,y only when unlabeled; clickable containers show inner text; keyboard keys hidden (-keys shows them)
      refs stay stable while an element is on screen. `-screen` on an action returns only what changed (new/changed lines, `gone:` refs; unlisted refs stay valid); -full forces the whole tree
android screenshot                                    image of the phone (Android 11+)
android ACTION [args]   args are key=value (quote values with spaces) or bare -flags; add device=NAME to pick a phone
  target: ref=N | match=TEXT (substring, exact=1 for equality) | id=RESOURCE_ID (+ index=N) | x=.. y=.. pixels
  tap | long_press | double_tap   gesture=1 forces a touch gesture instead of a click action
  type text=TEXT [ref|match|id] [append=1 clear=1 submit=1]   focused field when no target
  scroll direction=up|down|left|right [ref|match|id] [times=N]   (down reveals content below)
  swipe x=.. y=.. x2=.. y2=.. [duration_ms=N] | direction=.. [x= y=]
  drag x= y= x2= y2= [hold_ms= duration_ms=]
  pinch [x= y=] from_px=N to_px=N      (to_px > from_px zooms in)
  gesture strokes='[{"points":[[x,y],[x,y]],"start_ms":0,"duration_ms":300}]'   any multi-finger path
  key name=back|home|recents|notifications|quick_settings|power_menu|split_screen|lock_screen|take_screenshot|dismiss_shade|enter|wake|volume_up|volume_down|mute
  open app=NAME_OR_PACKAGE | uri=URL | intent='{"action":..,"data":..,"package":..,"class":..,"type":..,"extras":{}}'
  apps [match=TEXT]     launchable apps
  wait duration_ms=N | match=TEXT|id=ID timeout_ms=N [gone=1]
  clipboard name=get|set|paste|copy|cut|select_all [text=..]
  node_action name=click|long_click|focus|clear_focus|select|expand|collapse|dismiss|scroll_forward|scroll_backward|show_on_screen|context_click|set_progress|set_selection|ime_enter [ref|match|id] [value=N | x=start x2=end]
  -screen on any action also returns the updated UI (settle_ms=N waits first); -shot adds a screenshot
  shorthand: first bare token 12 = ref=12, 540,1200 = x,y; `android type hello` = text=hello
Phone offline -> push (FCM) wakes the app automatically."""


def _convert(key: str, value: str):
    if key in JSON_KEYS:
        try:
            return json.loads(value)
        except ValueError as error:
            raise CliError(f"{key} must be valid JSON: {error}")
    if key in BOOL_KEYS:
        return value.lower() not in ("0", "false", "no", "off", "")
    try:
        if key in INT_KEYS:
            return int(float(value))
        if key in FLOAT_KEYS:
            return float(value)
    except ValueError:
        raise CliError(f"{key} must be a number, got '{value}'")
    if key == "text":
        return value.replace("\\n", "\n").replace("\\t", "\t")
    return value


def _parse(action: str, tokens: list[str]) -> dict:
    args: dict = {}
    positional: list[str] = []
    for tok in tokens:
        flag = re.fullmatch(r"--?([A-Za-z_][A-Za-z0-9_-]*)", tok)
        if flag:
            key = ALIASES.get(flag.group(1), flag.group(1))
            args[key] = True
        elif re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
            key, _, value = tok.partition("=")
            key = ALIASES.get(key, key)
            args[key] = _convert(key, value)
        else:
            positional.append(tok)
    if positional:
        first = positional[0]
        if action == "wait" and re.fullmatch(r"\d+", first):
            args.setdefault("duration_ms", int(first))
            positional = positional[1:]
        elif re.fullmatch(r"\d+", first) and action not in ("type", "wait", "clipboard", "key") and "ref" not in args:
            args["ref"] = int(first)
            positional = positional[1:]
        elif re.fullmatch(r"-?\d+(\.\d+)?,-?\d+(\.\d+)?", first) and "x" not in args:
            args["x"], args["y"] = (float(v) for v in first.split(","))
            positional = positional[1:]
        if positional:
            rest = " ".join(positional)
            target = {"type": "text", "open": "app", "key": "name", "clipboard": "name", "node_action": "name",
                      "scroll": "direction", "swipe": "direction", "apps": "match", "wait": "match"}.get(action)
            if target is None:
                target = "match"
            args.setdefault(target, _convert(target, rest))
    return args


def _save_image(b64: str) -> str:
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    old = sorted(SHOT_DIR.glob("*.jpg"), key=lambda p: p.stat().st_mtime)[:-15]
    for path in old:
        try:
            path.unlink()
        except OSError:
            pass
    path = SHOT_DIR / f"phone-{int(time.time() * 1000)}.jpg"
    path.write_bytes(base64.b64decode(b64))
    return str(path)


def _format(res: dict) -> str:
    text = res.get("text", "")
    if not res.get("ok"):
        text = "ERR " + text
    image = res.get("image")
    if image:
        try:
            text += f"\nok screenshot {_save_image(image)}"
        except Exception:
            pass
    return text


def _call(op: str, args: dict, device: str) -> str:
    try:
        return _format(bridge.call(op, args, device, 90.0))
    except bridge.BridgeError as error:
        raise CliError(str(error))


def _status(args: dict, device: str) -> str:
    lines = []
    online = bridge.online_devices()
    if online:
        for d in online:
            lines.append(f"online: {d.name or d.model} ({d.id[:8]}) {d.model} Android {d.android} accessibility={'on' if d.a11y else 'OFF'}")
    else:
        lines.append("no phone connected")
    online_ids = {d.id for d in online}
    for did, info in store.devices().items():
        if did not in online_ids:
            lines.append(f"known offline: {info.get('name') or info.get('model')} push={'yes' if info.get('fcm_token') else 'no'}")
    lines.append(f"push wake-up (FCM): {'configured' if fcm.configured() else 'not configured (optional)'}")
    if online:
        try:
            lines.append(bridge.call("status", {}, device, 20).get("text", ""))
        except bridge.BridgeError as error:
            lines.append(f"status unavailable: {error}")
    show = bool(args.get("show_pairing"))
    if show or not online:
        urls = bridge.pairing_urls()
        for url in urls:
            lines.append(f"server url: {url}")
        if show:
            lines.append(f"token: {store.token()}")
            lines.append("pairing code (paste in the app): " + bridge.pairing_code(urls[0]))
        else:
            lines.append("run `android pair` for the pairing code")
    return "\n".join(lines)


def _pair() -> str:
    lines = []
    for url in bridge.pairing_urls():
        lines.append(f"server url: {url}")
        lines.append("pairing code: " + bridge.pairing_code(url))
    return "\n".join(lines)


def cmd_android(args: list[str]) -> str:
    if not args or args[0] in ("help", "-h", "--help"):
        return HELP_ANDROID
    action, rest = args[0], args[1:]
    parsed = _parse(action, rest)
    device = str(parsed.pop("device", ""))
    if action == "status":
        return _status(parsed, device)
    if action == "pair":
        return _pair()
    if action == "screen":
        parsed.pop("screen", None)
        return _call("screen", parsed, device)
    if action == "screenshot":
        return _call("screenshot", {}, device)
    if action not in ACTIONS:
        raise CliError(f"unknown android action '{action}'. try: status pair screen screenshot {' '.join(sorted(ACTIONS))}")
    if parsed.pop("screen", False) or parsed.get("screenshot"):
        parsed["observe"] = True
        parsed["screenshot"] = bool(parsed.get("screenshot"))
    return _call(action, parsed, device)

