"""Тесты без телефона: телефон подменён локальным HTTP-сервером с проверкой Basic-авторизации.

Запуск:  python -m unittest -v
"""
import base64
import json
import os
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import autodozvon as a


def _serve(httpd):
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url, headers=None):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=5) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def make_fake_phone(seen):
    """Телефон: без правильного Basic-пароля отвечает 401, с ним — 200 и запоминает путь."""
    expect = "Basic " + base64.b64encode(b"admin:secret").decode()

    class FakePhone(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Authorization") != expect:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="Yealink"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            seen.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, *args):
            pass

    return FakePhone


class ConfigTests(unittest.TestCase):
    def _write(self, text):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = os.path.join(d.name, "config.ini")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_reads_ini_and_env_password_overrides_file(self):
        path = self._write("[phone]\nhost = 172.26.47.19\nuser = admin\npassword = fromfile\n[server]\nport = 8100\n")
        with mock.patch.dict(os.environ, {"AUTODOZVON_PASSWORD": "fromenv"}):
            cfg = a.load_config(path)
        self.assertEqual(cfg["phone_host"], "172.26.47.19")
        self.assertEqual(cfg["phone_password"], "fromenv")
        self.assertEqual(cfg["phone_port"], 80)
        self.assertEqual(cfg["server_port"], 8100)

    def test_missing_host_exits(self):
        path = self._write("[phone]\npassword = x\n")
        with self.assertRaises(SystemExit):
            a.load_config(path)

    def test_missing_password_exits(self):
        path = self._write("[phone]\nhost = 10.0.0.5\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit):
                a.load_config(path)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.phone_seen = []
        cls.phone_srv = _serve(ThreadingHTTPServer(("127.0.0.1", 0), make_fake_phone(cls.phone_seen)))
        phone = a.Phone("127.0.0.1", cls.phone_srv.server_address[1], "admin", "secret")
        cls.httpd = _serve(a.make_server("127.0.0.1", 0, phone, a.EventLog()))
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        for srv in (cls.httpd, cls.phone_srv):
            srv.shutdown()
            srv.server_close()

    def test_index_page_and_vendor_script_are_served(self):
        code, body = _get(self.base + "/")
        self.assertEqual(code, 200)
        self.assertIn("Автодозвон", body)
        self.assertEqual(_get(self.base + "/vendor/xlsx.full.min.js")[0], 200)

    def test_dial_sends_servlet_key_number_with_auth(self):
        code, body = _get(self.base + "/api/dial?num=89161234567")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertIn("/servlet?key=number=89161234567", self.phone_seen)

    def test_dial_rejects_non_digits(self):
        code, _ = _get(self.base + "/api/dial?num=8916abc")
        self.assertEqual(code, 400)

    def test_keys_are_whitelisted_and_aliased(self):
        self.assertEqual(_get(self.base + "/api/key?k=CANCEL")[0], 200)
        self.assertEqual(_get(self.base + "/api/key?k=%23")[0], 200)  # «#» уходит как POUND
        self.assertIn("/servlet?key=POUND", self.phone_seen)
        self.assertEqual(_get(self.base + "/api/key?k=SPEAKER")[0], 200)  # громкая связь = HANDFREE
        self.assertIn("/servlet?key=HANDFREE", self.phone_seen)
        self.assertEqual(_get(self.base + "/api/key?k=F_TRANSFER")[0], 200)
        self.assertEqual(_get(self.base + "/api/key?k=VOLUME_UP")[0], 200)
        # перезагрузка и автопровижн намеренно запрещены
        self.assertEqual(_get(self.base + "/api/key?k=Reboot")[0], 400)
        self.assertEqual(_get(self.base + "/api/key?k=AutoP")[0], 400)
        self.assertEqual(_get(self.base + "/api/key?k=reboot")[0], 400)

    def test_keys_list_is_served(self):
        _, body = _get(self.base + "/api/keys")
        keys = json.loads(body)["keys"]
        self.assertIn("F1", keys)
        self.assertIn("POUND", keys)
        self.assertNotIn("Reboot", keys)
        self.assertNotIn("AutoP", keys)

    def test_events_come_back_after_cursor(self):
        _, body = _get(self.base + "/api/events?after=0")
        before = json.loads(body)["last"]
        _get(self.base + "/phone/terminated?remote=111&local=1001")
        _get(self.base + "/established?remote=111")
        _, body = _get(self.base + f"/api/events?after={before}")
        names = [e["event"] for e in json.loads(body)["events"]]
        self.assertEqual(names, ["terminated", "established"])

    def test_new_events_and_short_aliases(self):
        _, body = _get(self.base + "/api/events?after=0")
        before = json.loads(body)["last"]
        _get(self.base + "/phone/registered")
        _get(self.base + "/offhook?remote=222")
        _get(self.base + "/busy?remote=222")  # короткое имя приводится к remote_busy
        _, body = _get(self.base + f"/api/events?after={before}")
        names = [e["event"] for e in json.loads(body)["events"]]
        self.assertEqual(names, ["registered", "offhook", "remote_busy"])

    def test_api_refuses_foreign_host_and_cross_site_pages(self):
        before = len(self.phone_seen)
        # DNS rebinding: имя чужого сайта, но соединение идёт на 127.0.0.1
        self.assertEqual(_get(self.base + "/api/dial?num=89161234567", {"Host": "evil.example:8000"})[0], 403)
        # страница другого сайта в соседней вкладке
        self.assertEqual(_get(self.base + "/api/dial?num=89161234567", {"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(_get(self.base + "/api/key?k=CANCEL", {"Sec-Fetch-Site": "same-site"})[0], 403)
        self.assertEqual(_get(self.base + "/api/key?k=CANCEL", {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(len(self.phone_seen), before)  # ни одна команда до телефона не дошла
        # свои запросы проходят: со своей страницы и при открытии по localhost
        self.assertEqual(_get(self.base + "/api/status", {"Sec-Fetch-Site": "same-origin"})[0], 200)
        self.assertEqual(_get(self.base + "/api/status", {"Host": "localhost:8000", "Origin": "http://localhost:8000"})[0], 200)

    def test_phone_events_are_accepted_from_any_host(self):
        # события присылает телефон со своего адреса, поэтому Host и Sec-Fetch-Site здесь не проверяются
        self.assertEqual(_get(self.base + "/established?remote=5", {"Host": "10.0.0.7:8000"})[0], 200)

    def test_unreachable_phone_gives_502(self):
        dead = a.Phone("127.0.0.1", _free_port(), "admin", "secret")
        httpd = _serve(a.make_server("127.0.0.1", 0, dead, a.EventLog()))
        try:
            code, body = _get(f"http://127.0.0.1:{httpd.server_address[1]}/api/dial?num=1234")
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual(code, 502)
        self.assertFalse(json.loads(body)["ok"])


if __name__ == "__main__":
    unittest.main()
