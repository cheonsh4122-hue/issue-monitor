#!/usr/bin/env python3
import os
import json
import requests
from datetime import datetime
import hashlib

WEBHOOK_URL = os.getenv('WEBHOOK_URL')
CACHE_FILE = '/tmp/news_cache.json'

def load_cache():
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_cache(cache):
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

def fetch_news():
    try:
        response = requests.get('https://news.google.com/rss?q=뉴스&ceid=KR:ko')
        items = []
        if response.status_code == 200:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(response.content)
            for item in root.findall('.//item')[:5]:
                title = item.find('title')
                link = item.find('link')
                pubDate = item.find('pubDate')
                if title is not None and link is not None:
                    items.append({
                        'title': title.text,
                        'link': link.text,
                        'date': pubDate.text if pubDate is not None else datetime.now().isoformat()
                    })
        return items
    except Exception as e:
        print(f"Error fetching news: {e}")
        return []

def check_new_news():
    cache = load_cache()
    news_items = fetch_news()
    new_items = []
    for item in news_items:
        item_hash = hashlib.md5(item['title'].encode()).hexdigest()
        if item_hash not in cache:
            new_items.append(item)
            cache[item_hash] = item['date']
    save_cache(cache)
    if new_items and WEBHOOK_URL:
        send_webhook(new_items)
    return new_items

def send_webhook(items):
    payload = {
        'text': f"새로운 뉴스 {len(items)}개 발견!",
        'items': items
    }
    try:
        response = requests.post(WEBHOOK_URL, json=payload)
        print(f"Webhook sent: {response.status_code}")
    except Exception as e:
        print(f"Error sending webhook: {e}")

if __name__ == "__main__":
    check_new_news()
