#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
출입처 지역 이슈 모니터링 봇 v2
------------------------------------------------------
사용법:
  python3 region_news_monitor.py          # 평소 실행 (스케줄러가 호출)
  python3 region_news_monitor.py --force  # 시간대 무시하고 즉시 1회 실행
  python3 region_news_monitor.py --test   # 웹훅 연결 테스트 (샘플 메시지 1건)

검색 주기:
  평일(월~금) 8~22시: 1시간마다
  그 외 / 주말(토,일): 2시간마다
"""

import os, sys, json, html, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

# ======================== 설정 ========================

WEBHOOK_URL = os.environ.get(
    "WEBHOOK_URL",
    "https://hook.eu1.make.com/a53bnv5ykvhwquj2rp62oeg1t7t3rxnw"
)

# ── AI 요약 설정 ──────────────────────────────────────
# False = 무료, API 키 불필요. 새 기사를 그대로 전송
# True  = Claude가 한 줄 요약 + 중요도 표시 (API 키 + 별도 과금)
USE_AI_SUMMARY = False
AI_MODEL       = "claude-sonnet-4-6"   # Haiku 쓰면 비용 1/20
API_KEY        = os.environ.get("ANTHROPIC_API_KEY", "")

# ── 검색 키워드 (2026.06 실제 현안 기반으로 정비) ────────
KEYWORDS = {
    # -------------------------------------------------------
    # 나주 | 핵심: 켄텍, 직류산업혁신특구, 빛가람혁신도시, 영산강 국가정원
    # -------------------------------------------------------
    "나주": [
        "윤병태 나주시장",
        "나주시",
        "한국에너지공과대학교",       # 켄텍 - 나주 최대 현안
        "직류산업 혁신특구",
        "빛가람 혁신도시",
        "영산강 국가정원",
        "나주 에너지 전문과학관",
    ],

    # -------------------------------------------------------
    # 보성 | 핵심: 율포항, 보성말차·K-Tea, 농어촌 기본소득(8월 시작)
    # -------------------------------------------------------
    "보성": [
        "김철우 보성군수",
        "보성군",
        "율포항 국가어항",
        "보성말차",
        "K-Tea",
        "벌교 세계자연유산",
        "농어촌 기본소득",             # 2026년 8월 전국 시범사업 선정
    ],

    # -------------------------------------------------------
    # 진도 | 핵심: 이재각 민선9기 인수위 막 출범, '진도 대전환' 화두
    # -------------------------------------------------------
    "진도": [
        "이재각 진도군수",
        "진도군",
        "진도 대전환",                 # 민선9기 키워드
        "J-르네상스",
        "신비의 바다길",
        "진도 꽃게",
        "팽목항",                      # 세월호 추모·관광 연계 현안
    ],

    # -------------------------------------------------------
    # 장흥 | 핵심: 사순문 조국혁신당 당선 (전남 2곳 중 1곳, 이슈 多)
    # -------------------------------------------------------
    "장흥": [
        "사순문 장흥군수",
        "장흥군",
        "정남진",                      # 장흥 관광 대표 브랜드
        "장흥 문화",
        "장흥 해양",
        "장흥 축제",
        "천관산",
    ],

    # -------------------------------------------------------
    # 해남 | 핵심: 명현관 3선 당선, 지속가능농업·관광·솔라시도 에너지
    # -------------------------------------------------------
    "해남": [
        "명현관 해남군수",
        "해남군",
        "명량해전",                    # 이순신·관광 핵심
        "해남 솔라시도",               # 에너지단지 대형 현안
        "해남 땅끝",
        "해남 농업",
        "해남 관광",
    ],

    # -------------------------------------------------------
    # 전남광주 | 핵심: 민형배 초대 시장 당선, 대전환기획위 출범
    #            반도체 팹 투자 임박, 여수세계섬박람회, 통합대학 의대
    # -------------------------------------------------------
    "전남광주": [
        "민형배",
        "전남광주통합특별시",
        "전남광주대전환기획위원회",
        "전남광주 반도체",             # 초대형 투자 발표 임박 (민형배 직접 언급)
        "여수세계섬박람회",
        "전남 통합대학교",             # 국립의과대학 신설 추진
        "5극3특",                      # 국가균형발전 어젠다
    ],
}

# 최근 몇 시간 이내 기사만 '새 이슈'로 처리
RECENT_HOURS    = 36
MAX_PER_KEYWORD = 5
SEEN_FILE       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json")

# ======================== 이하 로직 ========================

KST = timezone(timedelta(hours=9))
UA  = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
IMPORTANCE_EMOJI = {"높음": "🔴", "보통": "🟡", "낮음": "⚪"}


def should_run_now(now_kst=None):
    """평일 8~22시 → True(매시간 실행), 그 외·주말 → 2시간 간격일 때만 True."""
    t = now_kst or datetime.now(KST)
    if t.weekday() < 5 and 8 <= t.hour < 22:
        return True
    return t.hour % 2 == 0


def fetch_rss(query):
    url = ("https://news.google.com/rss/search?q="
           + urllib.parse.quote(query)
           + "&hl=ko&gl=KR&ceid=KR:ko")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read()


def parse_rss(xml_bytes, region, query, now_utc=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    cutoff  = now_utc - timedelta(hours=RECENT_HOURS)
    items   = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return items

    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link  = (item.findtext("link")  or "").strip()
        if not title or not link:
            continue

        pub_raw = item.findtext("pubDate")
        pub_dt  = None
        if pub_raw:
            try:
                pub_dt = parsedate_to_datetime(pub_raw)
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pub_dt = None
        if pub_dt and pub_dt < cutoff:
            continue

        source      = html.unescape((item.findtext("source") or "").strip())
        clean_title = html.unescape(title)
        if " - " in clean_title:
            base, tail = clean_title.rsplit(" - ", 1)
            tail = tail.strip()
            if not source:
                source = tail
            if source and tail == source:
                clean_title = base
        clean_title = clean_title.strip()

        items.append({
            "region":  region,
            "keyword": query,
            "title":   clean_title,
            "source":  source,
            "url":     link,
            "pub":     pub_dt.astimezone(KST).strftime("%m/%d %H:%M") if pub_dt else "",
        })
        if len(items) >= MAX_PER_KEYWORD:
            break
    return items


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return {}
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_seen(seen):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
    seen   = {k: v for k, v in seen.items() if v >= cutoff}
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(seen, f, ensure_ascii=False)
    except OSError as e:
        print(f"[경고] seen.json 저장 실패: {e}")


def collect_new_items():
    seen       = load_seen()
    first_run  = (len(seen) == 0)
    now_ts     = datetime.now(timezone.utc).timestamp()
    new_items  = []
    seen_run   = set()

    for region, queries in KEYWORDS.items():
        for query in queries:
            try:
                xml_bytes = fetch_rss(query)
            except Exception as e:
                print(f"[경고] '{query}' 검색 실패: {e}")
                continue
            for it in parse_rss(xml_bytes, region, query):
                key = it["url"]
                if key in seen or key in seen_run:
                    continue
                seen_run.add(key)
                seen[key] = now_ts
                new_items.append(it)

    save_seen(seen)
    return new_items, first_run


def ai_enrich(items):
    if not items or not API_KEY:
        if not API_KEY:
            print("[경고] ANTHROPIC_API_KEY 없음. AI 요약 건너뜀.")
        return items

    listing = "\n".join(
        f'{i}. [{it["region"]}] {it["title"]} ({it["source"]})'
        for i, it in enumerate(items)
    )
    prompt = (
        "당신은 전남 지역 신문 데스크입니다. 아래 기사 제목 목록을 보고 "
        "각 기사에 한국어 한 줄 요약과 취재 중요도를 매기세요.\n"
        "중요도: '높음'(단독/사건사고/정책변화/예산), '보통'(일반 행정·행사), '낮음'(홍보·동정)\n"
        "설명이나 백틱 없이 오직 JSON 배열만 출력:\n"
        '[{"i":0,"summary":"한 줄 요약","importance":"높음"}, ...]\n\n'
        f"기사 목록:\n{listing}"
    )
    body = json.dumps({
        "model": AI_MODEL, "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"content-type": "application/json",
                 "x-api-key": API_KEY,
                 "anthropic-version": "2023-06-01"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        text  = "".join(b.get("text", "") for b in data.get("content", []))
        s, e  = text.find("["), text.rfind("]")
        parsed = json.loads(text[s:e + 1]) if s != -1 and e != -1 else []
        for row in parsed:
            idx = row.get("i")
            if isinstance(idx, int) and 0 <= idx < len(items):
                items[idx]["summary"]    = row.get("summary", "")
                items[idx]["importance"] = row.get("importance", "")
    except Exception as e:
        print(f"[경고] AI 요약 실패(기사 그대로 전송): {e}")
    return items


def build_message(items):
    """카카오톡/Gmail 알림용 메시지 생성 (모바일 가독성 최우선)."""
    stamp = datetime.now(KST).strftime("%m/%d %H:%M")

    # 상단: 지역별 건수 요약
    region_counts = {}
    for it in items:
        region_counts[it["region"]] = region_counts.get(it["region"], 0) + 1
    summary_parts = [f"{r} {n}건" for r, n in region_counts.items()]
    header = f"📰 새 이슈 {len(items)}건 ({stamp})\n"
    header += "  " + " · ".join(summary_parts)

    lines = [header, "─" * 28]

    # 지역 순서 고정
    region_order = ["나주", "보성", "진도", "장흥", "해남", "전남광주"]
    grouped = {r: [] for r in region_order}
    for it in items:
        grouped.setdefault(it["region"], []).append(it)

    for region in region_order:
        region_items = grouped.get(region, [])
        if not region_items:
            continue
        lines.append(f"\n▣ {region}")
        for it in region_items:
            imp = it.get("importance", "")
            emoji = IMPORTANCE_EMOJI.get(imp, "")

            # 제목 (중요도 이모지 앞에)
            title_line = f"{emoji} {it['title']}" if emoji else f"• {it['title']}"
            lines.append(title_line)

            # 메타: 매체 · 시간 (한 줄)
            meta = []
            if it.get("source"): meta.append(it["source"])
            if it.get("pub"):    meta.append(it["pub"])
            if meta:
                lines.append("  " + " · ".join(meta))

            # AI 요약 (있을 때만)
            if it.get("summary"):
                lines.append(f"  → {it['summary']}")

            lines.append(f"  {it['url']}")

    return "\n".join(lines)


def post_webhook(payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req  = urllib.request.Request(
        WEBHOOK_URL, data=body, method="POST",
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print(f"[전송완료] HTTP {r.status}")
            return True
    except Exception as e:
        print(f"[오류] 웹훅 전송 실패: {e}")
        return False


def run():
    items, first_run = collect_new_items()

    if first_run:
        post_webhook({
            "text": (
                "✅ 출입처 이슈 모니터링 시작!\n\n"
                "📍 모니터링 지역\n"
                "나주 · 보성 · 진도 · 장흥 · 해남 · 전남광주\n\n"
                "⏰ 검색 주기\n"
                "평일 8~22시: 1시간마다\n"
                "그 외 / 주말: 2시간마다\n\n"
                "지금부터 새 이슈가 생기면 이곳으로 알려드립니다."
            ),
            "count": 0,
        })
        print(f"[첫 실행] 기준선 저장 완료. 다음 실행부터 새 이슈만 알립니다.")
        return

    if not items:
        print("새 이슈 없음.")
        return

    if USE_AI_SUMMARY:
        items = ai_enrich(items)

    text = build_message(items)
    post_webhook({
        "text":      text,
        "count":     len(items),
        "timestamp": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
        "items":     items,
    })
    print(f"새 이슈 {len(items)}건 전송.")


def main():
    args = set(sys.argv[1:])

    if "--test" in args:
        ok = post_webhook({
            "text": (
                "🔔 [연결 테스트]\n\n"
                "이 메시지가 도착했다면 설정이 완료된 겁니다!\n\n"
                "📍 나주 · 보성 · 진도 · 장흥 · 해남 · 전남광주\n"
                "⏰ 평일 8~22시 1시간 / 그 외 2시간 간격으로 검색합니다."
            ),
            "count": 1,
        })
        print("테스트 전송 " + ("성공 ✅" if ok else "실패 ❌"))
        return

    if "--force" not in args and not should_run_now():
        print("대기 중 (실행 시간 아님: 평일 8~22시 매시간 / 그 외·주말 2시간 간격)")
        return

    run()


if __name__ == "__main__":
    main()
