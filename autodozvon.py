#!/usr/bin/env python3
"""Автодозвон через IP-телефон Yealink (T30 и аналогичные).

Отдаёт страницу обзвона (index.html), передаёт команды набора и сброса на телефон
и принимает его события (Action URL: исходящий звонок, разговор, завершение, занято).

Запуск:  python autodozvon.py          -> страница на http://localhost:8000
Нужен config.ini рядом с файлом (см. config.example.ini). Только стандартная библиотека Python 3.8+.
"""
import configparser
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.ini")

# Все события, которые телефон отправляет на Action URL (страница Features -> Action URL).
EVENT_NAMES = frozenset({
    "setup_completed", "registered", "unregistered", "register_failed",
    "offhook", "onhook", "incoming", "outgoing", "established", "terminated",
    "open_dnd", "close_dnd",
    "always_fwd_on", "always_fwd_off", "busy_fwd_on", "busy_fwd_off", "noanswer_fwd_on", "noanswer_fwd_off",
    "transfer_call", "blind_transfer", "attended_transfer", "transfer_failed", "transfer_finished",
    "forward_incoming", "hold", "unhold", "mute", "unmute", "missed_call", "ip_changed",
    "idle_to_busy", "busy_to_idle", "reject_incoming", "answer_new_incoming",
    "autop_finish", "callwaiting_on", "callwaiting_off", "headset", "handsfree",
    "cancel_callout", "remote_busy", "remote_canceled", "peripheral_info", "vpn_ip",
})
# Короткие имена из первой версии: принимаем и приводим к основным
EVENT_ALIASES = {"busy": "remote_busy", "remotecanceled": "remote_canceled", "cancelout": "cancel_callout"}
NUMBER_RE = re.compile(r"[0-9]{1,32}")
# Клавиши для servlet?key=... (Action URI). Reboot и AutoP намеренно не входят: перезагрузка и автопровижн.
KEY_NAMES = frozenset(
    ["OK", "ENTER", "CANCEL", "HANDFREE", "MUTE", "F_HOLD", "F_TRANSFER", "F_CONFERENCE",
     "F1", "F2", "F3", "F4", "MSG", "HEADSET", "RD", "UP", "DOWN", "LEFT", "RIGHT",
     "VOLUME_UP", "VOLUME_DOWN", "DNDOn", "DNDOff", "POUND", "*"]
    + [str(i) for i in range(10)]
    + [f"L{i}" for i in range(1, 7)]
    + [f"D{i}" for i in range(1, 11)]
)
# Удобные имена с кнопок страницы: «#» — это POUND, «SPEAKER» — это кнопка громкой связи HANDFREE
KEY_ALIASES = {"#": "POUND", "SPEAKER": "HANDFREE"}
LOCAL_ADDRS = ("127.0.0.1", "::1")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/vendor/xlsx.full.min.js": ("vendor/xlsx.full.min.js", "application/javascript; charset=utf-8"),
}


def load_config(path=CONFIG_PATH):
    """Читает config.ini. Пароль можно задать переменной окружения AUTODOZVON_PASSWORD (как MAIL_PASSWORD)."""
    if not os.path.exists(path):
        raise SystemExit("Нет config.ini. Скопируйте config.example.ini в config.ini и заполните.")
    cp = configparser.ConfigParser()
    cp.read(path, encoding="utf-8")
    cfg = {
        "phone_host": cp.get("phone", "host", fallback="").strip(),
        "phone_port": cp.getint("phone", "port", fallback=80),
        "phone_user": cp.get("phone", "user", fallback="admin").strip(),
        "phone_password": os.environ.get("AUTODOZVON_PASSWORD") or cp.get("phone", "password", fallback=""),
        "server_port": cp.getint("server", "port", fallback=8000),
    }
    if not cfg["phone_host"]:
        raise SystemExit("В config.ini не заполнено [phone] host (IP телефона).")
    if not cfg["phone_password"]:
        raise SystemExit("Задайте пароль: [phone] password или переменную AUTODOZVON_PASSWORD.")
    return cfg


class Phone:
    """Команды servlet на телефон. Понимает и Basic, и Digest авторизацию."""

    def __init__(self, host, port, user, password):
        self.base = f"http://{host}:{port}"
        pwd = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        pwd.add_password(None, self.base, user, password)
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPBasicAuthHandler(pwd),
            urllib.request.HTTPDigestAuthHandler(pwd),
        )

    def command(self, query):
        """Возвращает (код HTTP, текст ответа). Код 0 означает, что телефон не ответил."""
        try:
            with self.opener.open(f"{self.base}/servlet?{query}", timeout=5) as resp:
                return resp.status, resp.read(200).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:  # раньше OSError, т.к. HTTPError его наследует
            return e.code, e.reason
        except OSError as e:
            return 0, str(e)


class EventLog:
    """Последние события телефона. Страница забирает новые, передавая номер последнего известного."""

    def __init__(self, size=500):
        self._items = deque(maxlen=size)
        self._last = 0
        self._lock = threading.Lock()

    def add(self, name, remote="", local=""):
        name = EVENT_ALIASES.get(name, name)
        with self._lock:
            self._last += 1
            self._items.append({"id": self._last, "event": name, "remote": remote[:64], "local": local[:64]})

    def since(self, after):
        with self._lock:
            return [e for e in self._items if e["id"] > after], self._last


def servlet_key(name):
    """Имя клавиши для servlet?key=... или None, если клавиша не разрешена."""
    name = KEY_ALIASES.get(name, name)
    return name if name in KEY_NAMES else None


def make_handler(phone, log):
    class Handler(BaseHTTPRequestHandler):
        server_version = "autodozvon"

        def log_message(self, *args):
            pass  # без лога каждого запроса в консоли

        def _is_local(self):
            return self.client_address[0] in LOCAL_ADDRS

        def _is_own_page(self):
            """Страница и API — только с localhost и только из своей страницы.
            Host отсекает DNS rebinding (чужое имя, которое указывает на 127.0.0.1), Sec-Fetch-Site и Origin —
            запросы с других сайтов из соседней вкладки, которые могли бы набрать номер или нажать клавишу на телефоне."""
            if not self._is_local():
                return False
            host = urllib.parse.urlsplit("//" + self.headers.get("Host", "")).hostname
            if host not in LOCAL_HOSTS:
                return False
            if self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
                return False
            origin = self.headers.get("Origin")
            return origin is None or urllib.parse.urlsplit(origin).hostname in LOCAL_HOSTS

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, data):
            self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            path = url.path
            qs = urllib.parse.parse_qs(url.query)

            # Событие от телефона приходит с адреса телефона, поэтому пускаем с любого адреса сети
            name = path.lstrip("/")
            if name.startswith("phone/"):
                name = name[len("phone/"):]
            if name in EVENT_NAMES or name in EVENT_ALIASES:
                log.add(name, qs.get("remote", [""])[0], qs.get("local", [""])[0])
                return self._send(200, b"OK", "text/plain; charset=utf-8")

            # Страница и API — только с этого компьютера, по localhost и из своей страницы
            if not self._is_own_page():
                return self.send_error(403)
            if path in STATIC_FILES:
                rel, ctype = STATIC_FILES[path]
                with open(os.path.join(HERE, rel), "rb") as f:
                    return self._send(200, f.read(), ctype)
            if path.startswith("/api/"):
                return self._api(path, qs)
            self.send_error(404)

        def _api(self, path, qs):
            if path == "/api/dial":
                digits = qs.get("num", [""])[0]
                if not NUMBER_RE.fullmatch(digits):
                    return self._json(400, {"ok": False, "message": "Некорректный номер"})
                code, text = phone.command(f"key=number={digits}")
            elif path == "/api/key":
                key = servlet_key(qs.get("k", [""])[0])
                if key is None:
                    return self._json(400, {"ok": False, "message": "Эта клавиша не разрешена"})
                code, text = phone.command("key=" + key)
            elif path == "/api/keys":
                return self._json(200, {"keys": sorted(KEY_NAMES | set(KEY_ALIASES))})
            elif path == "/api/events":
                try:
                    after = int(qs.get("after", ["0"])[0] or 0)
                except ValueError:
                    after = 0
                items, last = log.since(after)
                return self._json(200, {"events": items, "last": last})
            elif path == "/api/status":
                return self._json(200, {"ok": True, "phone": phone.base})
            else:
                return self.send_error(404)

            if code == 0:
                return self._json(502, {"ok": False, "message": "Телефон не отвечает: " + text})
            ok = code == 200
            message = "Телефон принял команду" if ok else f"Телефон ответил кодом {code}: {text}"
            return self._json(200 if ok else 502, {"ok": ok, "status": code, "message": message})

    return Handler


def make_server(host, port, phone, log):
    return ThreadingHTTPServer((host, port), make_handler(phone, log))


def main():
    cfg = load_config()
    phone = Phone(cfg["phone_host"], cfg["phone_port"], cfg["phone_user"], cfg["phone_password"])
    httpd = make_server("0.0.0.0", cfg["server_port"], phone, EventLog())
    port = cfg["server_port"]
    print(f"Страница обзвона:  http://localhost:{port}")
    print(f"Телефон:           {cfg['phone_host']}")
    print("Action URL на телефоне (IP_ПК — адрес этого компьютера):")
    for ev in sorted(EVENT_NAMES):
        print(f"  {ev:14s} http://IP_ПК:{port}/{ev}?remote=$remote&local=$local")
    print("Остановить: Ctrl+C")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    sys.exit(main())
