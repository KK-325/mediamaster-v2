# -*- coding: utf-8 -*-
"""
统一通知模块 - 支持 Bark + 钉钉双通道
供 check_subscr.py / downloader.py / sync.py 共同调用
"""
import sqlite3
import logging
import json
import time
import hashlib
import hmac
import base64
import urllib.parse
import requests

DB_PATH = '/config/data.db'


def _load_config():
    """从数据库加载通知相关配置"""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT OPTION, VALUE FROM CONFIG')
            return {option: value for option, value in cursor.fetchall()}
    except Exception as e:
        logging.error(f"加载通知配置失败: {e}")
        return {}


class Notifier:
    """统一通知器，支持 Bark 和钉钉双通道"""

    def __init__(self, config=None):
        self.config = config if config is not None else _load_config()
        self.enabled = str(self.config.get("notification", "")).strip().lower() == "true"
        self.bark_enabled = str(self.config.get("bark_enabled", "False")).strip().lower() == "true"
        self.dingtalk_enabled = str(self.config.get("dingtalk_enabled", "False")).strip().lower() == "true"
        self.bark_key = (self.config.get("notification_api_key", "") or "").strip()
        self.dingtalk_webhook = (self.config.get("dingtalk_webhook", "") or "").strip()
        self.dingtalk_secret = (self.config.get("dingtalk_secret", "") or "").strip()

    def send(self, title, body):
        """统一发送入口，根据配置分别发送到启用的通道"""
        if not self.enabled:
            logging.info("通知功能未启用，跳过发送通知。")
            return
        sent_any = False
        if self.bark_enabled and self.bark_key:
            self._send_bark(title, body)
            sent_any = True
        if self.dingtalk_enabled and self.dingtalk_webhook:
            self._send_dingtalk(title, body)
            sent_any = True
        if not sent_any:
            logging.warning("通知已启用但未开启任何通道（Bark/钉钉）或未配置，跳过发送。")

    def _send_bark(self, title, body):
        """Bark 通知"""
        try:
            api_url = f"https://api.day.app/{self.bark_key}"
            data = {"title": title, "body": body}
            headers = {'Content-Type': 'application/json'}
            response = requests.post(api_url, data=json.dumps(data), headers=headers, timeout=10)
            if response.status_code == 200:
                logging.info(f"Bark 通知发送成功: {title}")
            else:
                logging.error(f"Bark 通知发送失败: {response.status_code} {response.text}")
        except requests.RequestException as e:
            logging.error(f"Bark 通知网络请求异常: {e}")
        except Exception as e:
            logging.error(f"Bark 通知发送异常: {e}")

    def _send_dingtalk(self, title, body):
        """钉钉机器人通知（加签模式）"""
        try:
            webhook = self.dingtalk_webhook
            # 加签
            if self.dingtalk_secret:
                timestamp = str(round(time.time() * 1000))
                string_to_sign = f"{timestamp}\n{self.dingtalk_secret}"
                hmac_code = hmac.new(
                    self.dingtalk_secret.encode("utf-8"),
                    string_to_sign.encode("utf-8"),
                    digestmod=hashlib.sha256
                ).digest()
                sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
                webhook = f"{webhook}&timestamp={timestamp}&sign={sign}"

            # 钉钉 markdown 格式，带图标更直观
            icon_map = {
                "订阅通知": "📋",
                "下载通知": "📥",
                "文件转移": "📦",
            }
            icon = icon_map.get(title, "📢")
            data = {
                "msgtype": "markdown",
                "markdown": {
                    "title": f"{icon} {title}",
                    "text": f"### {icon} {title}\n\n{body}"
                }
            }
            headers = {'Content-Type': 'application/json'}
            response = requests.post(webhook, data=json.dumps(data), headers=headers, timeout=10)
            resp_json = response.json() if response.status_code == 200 else {}
            if response.status_code == 200 and resp_json.get("errcode") == 0:
                logging.info(f"钉钉通知发送成功: {title}")
            else:
                logging.error(f"钉钉通知发送失败: {response.status_code} {response.text}")
        except requests.RequestException as e:
            logging.error(f"钉钉通知网络请求异常: {e}")
        except Exception as e:
            logging.error(f"钉钉通知发送异常: {e}")
