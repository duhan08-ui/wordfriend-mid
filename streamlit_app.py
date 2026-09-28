# 단어친구 웹 (WordFriend Web)
# - 학습/퀴즈: 아이용. 저장소 안의 words.csv + audio/*.mp3 를 그대로 사용
# - 관리: 보호자용. 단어 목록 저장 시 새 단어의 원어민 MP3(edge-tts)를 자동 생성해
#   words.csv 와 함께 GitHub 에 한 번의 커밋으로 반영 → 웹/안드로이드 앱 모두에 적용됨

import asyncio
import base64
import csv
import io
import os
import random
import re
import tempfile

import requests
import streamlit as st

# 앱별 설정 (Secrets). 없으면 초등판 기본값 → 같은 코드로 초등판/중등판 둘 다 운영 가능
def _secret(key, default):
    try:
        return st.secrets.get(key, default)
    except Exception:   # secrets.toml 이 아예 없는 로컬 실행
        return default


APP_TITLE = _secret("APP_TITLE", "단어친구")
SUPA_TABLE = _secret("SUPA_TABLE", "wf_stats")   # 중등판: wf_stats_mid
MAX_TTS_PER_SAVE = int(_secret("MAX_TTS_PER_SAVE", 250))  # 한 번 저장에 만드는 발음 최대 수

SHOW_STARS = not SUPA_TABLE.endswith("_mid")   # 중등판은 별 제도 없음 → 리포트·설정에서 별 숨김
APP_ICON = _secret("APP_ICON", "📘" if SUPA_TABLE.endswith("_mid") else "🦓")

st.set_page_config(page_title=APP_TITLE, page_icon=APP_ICON, layout="centered")

PRAISES = ["Good job!", "Excellent!", "Great!", "Perfect!", "Wonderful!"]

SUPA_URL = "https://mztadmbkbrsvqmsqojcp.supabase.co"
SUPA_KEY = "sb_publishable_Za91TlCA4yg1crtS2-VHHg_r6BOi0an"

VOICES = [
    "en-US-JennyNeural",   # 여성, 또렷하고 자연스러움 (기본)
    "en-US-AnaNeural",     # 어린이 목소리
    "en-US-GuyNeural",     # 남성
    "en-GB-SoniaNeural",   # 영국식
]


# ---------- 공통 ----------

def slug(en: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", en.lower()).strip("_")


def load_words():
    words = []
    if not os.path.exists("words.csv"):
        return words
    with open("words.csv", encoding="utf-8") as f:
        for row in csv.reader(f):
            if not row or row[0].strip() in ("", "en"):
                continue
            row = [c.strip() for c in row] + ["", "", ""]
            words.append({"en": row[0], "ko": row[1], "emoji": row[2], "group": row[3]})
    return words


GRAMMAR_FILE = "grammar.json"


def load_grammar():
    """문법 콘텐츠 (중등판 저장소에만 있음). 없거나 깨졌으면 None"""
    import json as _j
    if not os.path.exists(GRAMMAR_FILE):
        return None
    try:
        return _j.load(open(GRAMMAR_FILE, encoding="utf-8"))
    except Exception:
        return None


def validate_grammar(g):
    """앱(Grammar.kt)과 같은 규칙으로 검사. 문제 목록(오류 문자열) 반환 — 비어 있으면 OK"""
    errs = []
    if not isinstance(g, dict) or not isinstance(g.get("units"), list) or not g["units"]:
        return ["units 목록이 없어요"]
    ids = set()
    for u in g["units"]:
        uid = u.get("id", "")
        if not uid:
            errs.append("id 없는 단원이 있어요"); continue
        if uid in ids:
            errs.append(f"{uid}: 단원 id 중복")
        ids.add(uid)
        for sec in ("basic", "drill"):
            for i, q in enumerate(u.get(sec) or []):
                w = f"{uid} {sec} {i + 1}번"
                t = q.get("t")
                if t == "c":
                    o = q.get("o") or []
                    if len(o) < 2 or not isinstance(q.get("a"), int) or not 0 <= q["a"] < len(o):
                        errs.append(f"{w}: 보기/정답 번호 확인")
                elif t == "b":
                    if not q.get("a") or not all(str(a).strip() for a in q["a"]):
                        errs.append(f"{w}: 빈칸 정답 없음")
                    if str(q.get("q", "")).count("___") != 1:
                        errs.append(f"{w}: 빈칸(___)이 정확히 1개여야 해요")
                elif t == "o":
                    a = str(q.get("a", "")).split("||")[0].strip()
                    if len(a.split()) < 2:
                        errs.append(f"{w}: 배열 정답 문장이 너무 짧아요")
                else:
                    errs.append(f"{w}: 유형(t)은 c/b/o 중 하나")
        for i, x in enumerate(u.get("sents") or []):
            w = f"{uid} 예문 {i + 1}번"
            e, k = _chunks(x.get("en", "")), _chunks(x.get("ko", ""))
            if not e:
                errs.append(f"{w}: 영어 없음")
            elif len(e) != len(k):
                errs.append(f"{w}: 덩어리 수가 달라요 (영어 {len(e)} / 우리말 {len(k)})")
            if not str(x.get("full", "")).strip():
                errs.append(f"{w}: 완성 해석 없음")
            if not [y for y in (x.get("wrong") or []) if str(y).strip()]:
                errs.append(f"{w}: 틀린 해석이 최소 1개 필요해요")
            errs += _detail_errs(x, w)
    return errs


READING_FILE = "reading.json"


def load_reading():
    """문장해석·독해 콘텐츠 (중등판 저장소에만 있음). 없거나 깨졌으면 None"""
    import json as _j
    if not os.path.exists(READING_FILE):
        return None
    try:
        return _j.load(open(READING_FILE, encoding="utf-8"))
    except Exception:
        return None


def _chunks(s):
    return [c.strip() for c in str(s).split(" / ") if c.strip()]


def _detail_errs(x, where):
    """자세히 보기 자료(w/ord/why/wr) 검사 — 없으면 통과(앱이 해당 칸을 건너뜀)"""
    errs = []
    n = len(_chunks(x.get("en", "")))
    o = x.get("ord")
    if o is not None and (not isinstance(o, list) or sorted(o) != list(range(n))):
        errs.append(f"{where}: 해석 순서(ord)는 0~{n - 1} 번호를 한 번씩 써야 해요")
    w = x.get("w")
    if w is not None and (not isinstance(w, list) or not all(isinstance(p, list) and len(p) == 2 for p in w)):
        errs.append(f"{where}: 단어 뜻(w) 형식 확인")
    if x.get("wr") is not None and len(x.get("wr") or []) > len(x.get("wrong") or []):
        errs.append(f"{where}: 틀린 이유(wr)가 틀린 해석보다 많아요")
    return errs


def validate_reading(d):
    """앱(Reading.kt)이 읽을 수 있는지 + 끊어 읽기 덩어리 수 검사. 오류 문자열 목록 — 비어 있으면 OK"""
    errs = []
    if not isinstance(d, dict):
        return ["JSON 최상위가 객체가 아니에요"]
    sets = d.get("sets") or []
    passages = d.get("passages") or []
    if not isinstance(sets, list) or not isinstance(passages, list):
        return ["sets / passages 는 목록이어야 해요"]
    if not sets and not passages:
        return ["글이 하나도 없어요"]
    ids = set()
    for s_ in sets:
        sid = s_.get("id", "")
        if not sid:
            errs.append("id 없는 문장 세트가 있어요"); continue
        if sid in ids:
            errs.append(f"{sid}: id 중복")
        ids.add(sid)
        for i, it in enumerate(s_.get("items") or []):
            e, k = _chunks(it.get("en", "")), _chunks(it.get("ko", ""))
            if not e or len(e) != len(k) or not str(it.get("full", "")).strip():
                errs.append(f"{sid} {i + 1}번 문장: 덩어리/해석 확인")
    for p_ in passages:
        pid = p_.get("id", "")
        if not pid:
            errs.append("id 없는 글이 있어요"); continue
        if pid in ids:
            errs.append(f"{pid}: id 중복")
        ids.add(pid)
        if not str(p_.get("title", "")).strip():
            errs.append(f"{pid}: 제목 없음")
        sents = p_.get("sents") or []
        if not sents:
            errs.append(f"{pid}: 문장이 없어요")
        for i, s2 in enumerate(sents):
            e, k = _chunks(s2.get("en", "")), _chunks(s2.get("ko", ""))
            if not e:
                errs.append(f"{pid} {i + 1}번 문장: 영어 없음")
            elif len(e) != len(k):
                errs.append(f"{pid} {i + 1}번 문장: 덩어리 수가 달라요 (영어 {len(e)} / 우리말 {len(k)})")
            if not str(s2.get("full", "")).strip():
                errs.append(f"{pid} {i + 1}번 문장: 완성 해석 없음")
            errs += _detail_errs(s2, f"{pid} {i + 1}번 문장")
        for i, q in enumerate(p_.get("qs") or []):
            o = q.get("o") or []
            if not str(q.get("q", "")).strip() or len(o) < 2 or not all(str(x).strip() for x in o) \
                    or not isinstance(q.get("a"), int) or not 0 <= q["a"] < len(o):
                errs.append(f"{pid} 문제 {i + 1}: 문제/보기/정답 번호 확인")
    return errs


def audio_path(en: str):
    p = os.path.join("audio", slug(en) + ".mp3")
    return p if os.path.exists(p) else None


def play(en: str, autoplay=False):
    p = audio_path(en)
    if p:
        st.audio(p, autoplay=autoplay)
    else:
        st.caption("🔇 아직 발음 파일이 없어요 (관리 페이지에서 저장하면 생성됩니다)")


def big_card(word):
    st.markdown(
        f"""
        <div style="background:#fff;border-radius:28px;padding:36px 16px;text-align:center;
                    box-shadow:0 4px 14px rgba(0,0,0,.08);margin-bottom:12px;">
          <div style="font-size:64px;line-height:1.1;">{word['emoji']}</div>
          <div style="font-size:52px;font-weight:800;color:#3E3A39;">{word['en']}</div>
          <div style="font-size:26px;color:#8A8580;margin-top:6px;">{word['ko']}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ---------- 학습 ----------

def page_learn(words):
    st.subheader("📖 단어 배우기")
    seen = []
    for w in words:
        if w["group"] and w["group"] not in seen:
            seen.append(w["group"])
    groups = ["🌈 전체"] + seen
    g = st.selectbox("범위", groups)
    cur = [w for w in words if g == "🌈 전체" or w["group"] == g]
    if not cur:
        st.info("단어가 없어요.")
        return

    key = f"learn_idx_{g}"
    idx = st.session_state.get(key, 0) % len(cur)
    w = cur[idx]

    st.caption(f"{idx + 1} / {len(cur)}")
    big_card(w)
    play(w["en"], autoplay=True)

    c1, c2 = st.columns(2)
    if c1.button("◀ 이전", use_container_width=True):
        st.session_state[key] = (idx - 1) % len(cur)
        st.rerun()
    if c2.button("다음 ▶", use_container_width=True, type="primary"):
        st.session_state[key] = (idx + 1) % len(cur)
        st.rerun()


# ---------- 퀴즈 ----------

def make_questions(words, mode):
    pool = random.sample(words, min(10, len(words)))
    qs = []
    for w in pool:
        m = mode if mode != "MIX" else random.choice(["LISTEN", "EN_KO", "KO_EN"])
        use_en = m == "KO_EN"
        answer = w["en"] if use_en else w["ko"]
        wrong = list({(x["en"] if use_en else x["ko"]) for x in words} - {answer})
        choices = random.sample(wrong, min(3, len(wrong))) + [answer]
        random.shuffle(choices)
        qs.append({"w": w, "mode": m, "choices": choices, "answer": answer})
    return qs


def page_quiz(words):
    st.subheader("🎯 퀴즈")
    if len(words) < 4:
        st.info("퀴즈를 하려면 단어가 4개 이상 필요해요.")
        return

    ss = st.session_state
    if "quiz" not in ss:
        mode = st.radio(
            "어떤 퀴즈로 할까?",
            ["LISTEN", "EN_KO", "KO_EN", "MIX"],
            format_func=lambda m: {
                "LISTEN": "🎧 듣고 뜻 고르기",
                "EN_KO": "👀 영어 보고 뜻 고르기",
                "KO_EN": "🇰🇷 한글 보고 영어 고르기",
                "MIX": "🌈 섞어서!",
            }[m],
        )
        if st.button("시작! 🚀", type="primary", use_container_width=True):
            ss.quiz = {"qs": make_questions(words, mode), "i": 0, "score": 0,
                       "wrong": [], "picked": None}
            st.rerun()
        return

    q = ss.quiz
    if q["i"] >= len(q["qs"]):
        st.markdown(f"## {q['score']} / {len(q['qs'])}")
        st.markdown("완벽해요! 🏆" if q["score"] == len(q["qs"]) else "참 잘했어요! 🌟")
        for w in {x["en"]: x for x in q["wrong"]}.values():
            st.write(f"🔍 **{w['en']}** — {w['ko']}")
            play(w["en"])
        if st.button("한 번 더! 🔁", type="primary", use_container_width=True):
            del ss.quiz
            st.rerun()
        return

    item = q["qs"][q["i"]]
    w = item["w"]
    st.caption(f"{q['i'] + 1} / {len(q['qs'])}")

    if item["mode"] == "LISTEN":
        st.markdown("### 🔊 잘 듣고 뜻을 골라요")
        play(w["en"], autoplay=q["picked"] is None)
    elif item["mode"] == "EN_KO":
        st.markdown(f"### {w['en']}  <span style='font-size:16px;color:#8A8580'>무슨 뜻일까요?</span>", unsafe_allow_html=True)
        play(w["en"], autoplay=q["picked"] is None)
    else:
        st.markdown(f"### {w['ko']}  <span style='font-size:16px;color:#8A8580'>영어로 뭘까요?</span>", unsafe_allow_html=True)

    if q["picked"] is None:
        for c in item["choices"]:
            if st.button(c, use_container_width=True, key=f"c_{q['i']}_{c}"):
                q["picked"] = c
                if c == item["answer"]:
                    q["score"] += 1
                else:
                    q["wrong"].append(w)
                st.rerun()
    else:
        ok = q["picked"] == item["answer"]
        if ok:
            st.success(f"⭕ 정답! **{item['answer']}**")
            p = audio_path(random.choice(PRAISES))
            if p:
                st.audio(p, autoplay=True)
        else:
            st.error(f"❌ 정답은 **{item['answer']}** 이에요")
        if not ok:
            play(w["en"], autoplay=True)
        if st.button("다음 문제 ▶", type="primary", use_container_width=True):
            q["i"] += 1
            q["picked"] = None
            st.rerun()


# ---------- 관리 (보호자) ----------

def gh(path, method="GET", **kw):
    """GitHub API 호출. 일시적 오류(5xx·429·시간 초과)는 잠깐 쉬었다가 최대 5번까지 다시 시도
    (발음 파일 수백 개를 올리다 한 번 504가 나도 그동안 만든 게 날아가지 않게)"""
    import time as _time
    last = None
    for attempt in range(5):
        try:
            r = requests.request(
                method,
                f"https://api.github.com/repos/{st.secrets['GH_REPO']}/{path}",
                headers={
                    "Authorization": f"Bearer {st.secrets['GH_TOKEN']}",
                    "Accept": "application/vnd.github+json",
                },
                timeout=60,
                **kw,
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last = e
            _time.sleep(2 * (attempt + 1))
            continue
        if r.status_code >= 500 or r.status_code == 429:
            last = requests.HTTPError(f"{r.status_code} {r.reason}", response=r)
            _time.sleep(2 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise last


def commit_files(files: dict, message: str):
    """여러 파일(경로→bytes)을 GitHub에 '한 번의 커밋'으로 반영"""
    branch = st.secrets.get("GH_BRANCH", "main")
    head = gh(f"git/ref/heads/{branch}")["object"]["sha"]
    base_tree = gh(f"git/commits/{head}")["tree"]["sha"]
    tree = []
    for path, data in files.items():
        blob = gh("git/blobs", "POST",
                  json={"content": base64.b64encode(data).decode(), "encoding": "base64"})
        tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    new_tree = gh("git/trees", "POST", json={"base_tree": base_tree, "tree": tree})
    commit = gh("git/commits", "POST",
                json={"message": message, "tree": new_tree["sha"], "parents": [head]})
    gh(f"git/refs/heads/{branch}", "PATCH", json={"sha": commit["sha"]})


def gen_mp3(text: str, voice: str) -> bytes:
    import edge_tts

    async def _run(path):
        await edge_tts.Communicate(text, voice).save(path)

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        path = f.name
    try:
        asyncio.run(_run(path))
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.unlink(path)


def check_admin() -> bool:
    if st.session_state.get("auth"):
        return True
    pw = st.text_input("비밀번호", type="password", key="admin_pw")
    if st.button("확인", key="admin_ok"):
        if pw == st.secrets.get("ADMIN_PASSWORD", ""):
            st.session_state.auth = True
            st.rerun()
        st.error("비밀번호가 달라요")
    return False


def kst(iso: str) -> str:
    """서버 UTC 시각을 한국 시간 문자열로"""
    try:
        from datetime import datetime, timedelta, timezone
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso[:16].replace("T", " ")


def page_report(words):
    st.subheader("📊 학습 리포트")
    if not check_admin():
        return

    try:
        r = requests.get(
            f"{SUPA_URL}/rest/v1/{SUPA_TABLE}?select=*&order=updated_at.desc",
            headers={"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}"},
            timeout=10,
        )
        r.raise_for_status()
        rows = r.json()
    except Exception as e:
        st.error(f"서버에서 기록을 불러오지 못했어요: {e}")
        return

    if not rows:
        st.info("아직 올라온 기록이 없어요. 아이 폰에서 학습하면 자동으로 올라옵니다.")
        return

    labels = {f"{x.get('child_name') or '기기'} ({x['device_id'][:6]}…)": x for x in rows}
    pick = st.selectbox("기기", list(labels.keys()))
    row = labels[pick]
    st.caption(f"마지막 기록: {kst(row.get('updated_at', ''))} (한국 시간)")

    stats = row.get("stats") or {}
    meta = stats.get("meta") or {}
    if meta:
        st.caption(
            f"📲 이 폰 적용 상태: 앱 v{meta.get('appVer','?')} · 단어 {meta.get('words',0)}개 · "
            f"배움 {meta.get('seen',0)}개 · 발음 {meta.get('audio',0)}개 · 마지막 동기화 {meta.get('lastSync','-')}"
        )
    days = stats.get("days") or {}
    keys = sorted(days.keys(), reverse=True)[:14]

    # ---- 주간 요약: 최근 7일 vs 그 전 7일 ----
    from datetime import datetime, timedelta, timezone
    def day_val(d0, field):
        return (days.get(d0.isoformat()) or {}).get(field, 0)
    # 서버는 UTC로 돌아서 date.today()를 쓰면 한국 새벽(0~9시)에 날짜가 하루 밀림 → KST 기준 오늘
    t = (datetime.now(timezone.utc) + timedelta(hours=9)).date()
    wk = [t - timedelta(days=i) for i in range(7)]
    prev = [t - timedelta(days=i) for i in range(7, 14)]
    def summary(ds):
        play = sum(day_val(d, "playSec") for d in ds)
        r = sum(day_val(d, "right") for d in ds)
        w = sum(day_val(d, "wrong") for d in ds)
        rate = round(r / (r + w) * 100) if (r + w) else 0
        return play // 60, rate, r + w
    p1, rate1, n1 = summary(wk)
    p0, rate0, n0 = summary(prev)

    c1, c2, c3 = st.columns(3)
    c1.metric("최근 7일 학습", f"{p1}분", delta=f"{p1 - p0:+d}분")
    c2.metric("최근 7일 정답률", f"{rate1}%" if n1 else "-",
              delta=(f"{rate1 - rate0:+d}%p" if n1 and n0 else None))
    c3.metric("최근 7일 문제 수", n1, delta=n1 - n0)

    # ---- 그래프 ----
    import pandas as pd
    ordered = sorted(days.keys())[-14:]
    if ordered:
        df = pd.DataFrame({
            "날짜": [k[5:] for k in ordered],
            "학습(분)": [round(days[k].get("playSec", 0) / 60, 1) for k in ordered],
        }).set_index("날짜")
        st.markdown("##### ⏱ 학습 시간 (최근 14일)")
        st.bar_chart(df, height=220)

        rated = [k for k in ordered
                 if days[k].get("right", 0) + days[k].get("wrong", 0) > 0]
        if rated:
            df2 = pd.DataFrame({
                "날짜": [k[5:] for k in rated],
                "정답률(%)": [
                    round(days[k]["right"] / (days[k]["right"] + days[k].get("wrong", 0)) * 100)
                    if (days[k].get("right", 0) + days[k].get("wrong", 0)) else 0
                    for k in rated
                ],
            }).set_index("날짜")
            st.markdown("##### 🎯 정답률 추이")
            st.line_chart(df2, height=220)

    # ---- 라이트너 상자 분포 ----
    wstats_all = stats.get("words") or {}
    tried = [s for s in wstats_all.values() if s.get("right", 0) + s.get("wrong", 0) > 0]
    if tried:
        dist = {f"상자{b}": sum(1 for s in tried if s.get("box", 0) == b) for b in range(5)}
        weak_n = dist["상자0"] + dist["상자1"]
        st.markdown(f"##### 📦 단어 상태 (약한 단어 {weak_n}개)")
        st.bar_chart(pd.DataFrame([dist]).T.rename(columns={0: "단어 수"}), height=200)
        st.caption("상자0~1 = 자주 나오게 되는 약한 단어 · 상자4 = 완전히 익힌 단어")

    # ===== 이번 주(월~일) 요약 =====
    from datetime import datetime, timedelta, timezone
    _kst = timezone(timedelta(hours=9))
    _today = datetime.now(_kst).date()
    _monday = _today - timedelta(days=(_today.weekday()))  # 월=0
    _wk_play = _wk_star = _wk_days = _wk_r = _wk_w = 0
    _wk_bar = []
    _dow_names = ["월","화","수","목","금","토","일"]
    for _i in range(7):
        _d = _monday + timedelta(days=_i)
        _key = _d.strftime("%Y-%m-%d")
        _o = days.get(_key) or {}
        _p = _o.get("playSec", 0); _r = _o.get("right", 0); _w = _o.get("wrong", 0)
        if _p > 0 or _r > 0 or _w > 0: _wk_days += 1
        _wk_play += _p; _wk_star += _o.get("stars", 0); _wk_r += _r; _wk_w += _w
        _wk_bar.append({"요일": _dow_names[_i], "공부(분)": round(_p/60, 1)})
    # 지금까지 모은 별 (전체 누적) — 앱 홈의 '이번 주 별'은 월요일에 0으로 리셋되므로 여기서만 볼 수 있음
    _star_all = sum((_o or {}).get("stars", 0) for _o in days.values())
    _star_today = (days.get(_today.isoformat()) or {}).get("stars", 0)

    if SHOW_STARS:
        st.markdown("##### ⭐ 모은 별")
        _sc1, _sc2, _sc3 = st.columns(3)
        _sc1.metric("오늘", f"⭐{_star_today}")
        _sc2.metric("이번 주 (월~일)", f"⭐{_wk_star}")
        _sc3.metric("지금까지 모두", f"⭐{_star_all}")

    st.markdown("##### 📅 이번 주 요약 (월~일)")
    _wc1, _wc2, _wc3, _wc4 = st.columns(4)
    _wc1.metric("공부 시간", f"{_wk_play//60}분")
    if SHOW_STARS:
        _wc2.metric("받은 별", f"⭐{_wk_star}")
    else:
        _wk_rate = round(_wk_r / (_wk_r + _wk_w) * 100) if (_wk_r + _wk_w) else 0
        _wc2.metric("정답률", f"{_wk_rate}%" if (_wk_r + _wk_w) else "-")
    _wc3.metric("학습한 날", f"{_wk_days}일")
    _wc4.metric("정답/오답", f"{_wk_r}/{_wk_w}")
    import pandas as _pd
    st.bar_chart(_pd.DataFrame(_wk_bar).set_index("요일"), height=200)

    # ===== 세션(시각별) — 머문/공부 둘 다 =====
    _sess_rows = []
    for k in sorted(days.keys(), reverse=True)[:7]:
        for s in (days[k].get("sessions") or []):
            _elapsed = max(0, int((s.get("endMs", 0) - s.get("startMs", 0)) / 1000))
            _sess_rows.append({
                "날짜": k[5:],
                "시작": s.get("start", ""),
                "종료": s.get("end", ""),
                "머문(분)": round(_elapsed / 60, 1),
                "공부(분)": round(s.get("playSec", 0) / 60, 1),
                "⭕": s.get("right", 0),
                "❌": s.get("wrong", 0),
                "🎤": s.get("speak", 0),
            })
    if _sess_rows:
        st.markdown("##### ⏰ 학습 시간대 (세션별 · 머문/공부)")
        st.dataframe(_sess_rows, use_container_width=True, hide_index=True)

    st.markdown("##### 📅 일자별 기록")
    st.dataframe(
        [
            {
                "날짜": k,
                "접속(분)": round(days[k].get("appSec", 0) / 60, 1),
                "학습(분)": round(days[k].get("playSec", 0) / 60, 1),
                "⭕": days[k].get("right", 0),
                "❌": days[k].get("wrong", 0),
                "🎤": days[k].get("speak", 0),
                **({"⭐": days[k].get("stars", 0)} if SHOW_STARS else {}),
            }
            for k in keys
        ],
        use_container_width=True, hide_index=True,
    )

    wmap = {w["en"]: w for w in words}
    wstats = stats.get("words") or {}
    # 상태 우선: 🔴(box 낮음) 먼저 → 그 안에서 틀린 횟수 많은 순 (위에서부터 도와줄 순서)
    weak = sorted(
        ((en, s) for en, s in wstats.items() if s.get("wrong", 0) > 0),
        key=lambda x: (x[1].get("box", 0), -x[1].get("wrong", 0)),
    )[:15]
    def _box_label(b):
        return {0: "🔴 아직 어려워요", 1: "🔴 조금 어려워요", 2: "🟡 외우는 중",
                3: "🟢 거의 외웠어요"}.get(int(b or 0), "🟢 다 외웠어요")

    st.markdown("##### ❗ 많이 틀린 단어 TOP")
    st.caption("위에서부터 지금 도와주면 좋은 순서예요. 🔴 = 아직 어려운 단어, 🟢 = 다 외운 단어.")
    if not weak:
        st.caption("틀린 단어가 아직 없어요 👍")
    else:
        st.dataframe(
            [
                {
                    "단어": en,
                    "뜻": wmap.get(en, {}).get("ko", ""),
                    "상태": _box_label(s.get("box", 0)),
                    "틀림": s.get("wrong", 0),
                    "맞힘": s.get("right", 0),
                    "최근": s.get("last", ""),
                }
                for en, s in weak
            ],
            use_container_width=True, hide_index=True,
        )

    grammar_report(stats)
    reading_report(stats)


def reading_report(stats):
    d = load_reading()
    if not d:
        return
    import pandas as pd
    rs = stats.get("reading") or {}
    days = stats.get("days") or {}
    from datetime import datetime, timedelta, timezone
    today = (datetime.now(timezone.utc) + timedelta(hours=9)).date()
    wk = [(today - timedelta(days=i)).isoformat() for i in range(7)]
    rr = sum((days.get(x) or {}).get("rRight", 0) for x in wk)
    rw = sum((days.get(x) or {}).get("rWrong", 0) for x in wk)
    passages = d.get("passages") or []
    st.markdown("##### 📄 짧은 글 읽기")
    c1, c2 = st.columns(2)
    c1.metric("읽은 글", f"{sum(1 for x in passages if x['id'] in rs)} / {len(passages)}")
    c2.metric("해석·내용 문제 7일 정답률", f"{round(rr / (rr + rw) * 100)}%" if (rr + rw) else "-",
              help=f"최근 7일 {rr + rw}문제 (문장 공부의 해석 고르기 포함)")
    rows = []
    for x in passages:
        r = rs.get(x["id"])
        if not r:
            continue
        rows.append({"글": f"{x.get('title', '')} ({x.get('tk', '')})",
                     "맞힘": f"{r.get('best', 0)}/{r.get('total', 0)}", "횟수": r.get("tries", 0), "최근": r.get("date", "-")})
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def grammar_report(stats):
    g = load_grammar()
    if not g:
        return
    import pandas as pd
    gs = stats.get("grammar") or {}
    days = stats.get("days") or {}
    from datetime import datetime, timedelta, timezone
    today = (datetime.now(timezone.utc) + timedelta(hours=9)).date()
    wk = [(today - timedelta(days=i)).isoformat() for i in range(7)]
    gr = sum((days.get(d) or {}).get("gRight", 0) for d in wk)
    gw = sum((days.get(d) or {}).get("gWrong", 0) for d in wk)
    known = len(stats.get("known") or {})

    def done(u):
        r = (gs.get(u["id"]) or {}).get("step") or {}
        return r.get("total", 0) >= 5 and r.get("best", 0) * 10 >= r.get("total", 0) * 7

    st.markdown("##### 🧩 문장 공부")
    c1, c2, c3 = st.columns(3)
    c1.metric("익힌 단어", known)
    c2.metric("완료한 단계", f"{sum(done(u) for u in g['units'])} / {len(g['units'])}",
              help="문제를 5개 이상 풀고 70% 이상 맞힌 단계")
    c3.metric("최근 7일 문법 정답률", f"{round(gr / (gr + gw) * 100)}%" if (gr + gw) else "-")

    def cell(u):
        r = (gs.get(u["id"]) or {}).get("step")
        if not r:
            return "-"
        return f"{r.get('best', 0)}/{r.get('total', 0)}" + (" ✅" if done(u) else "") + f" ({r.get('tries', 0)}회)"

    rows = [{"단계": f"{i + 1}. {u.get('name', u.get('title', ''))}", "문법": u.get("term", u.get("title", "")),
             "최고": cell(u), "최근": ((gs.get(u["id"]) or {}).get("step") or {}).get("date", "-")}
            for i, u in enumerate(g["units"])]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def page_grammar_admin():
    st.subheader("🧩 문장 공부 관리")
    if not check_admin():
        return
    import json as _j
    g = load_grammar()
    if not g:
        st.info("grammar.json 이 없어요 (문법은 중등판 저장소에서만 사용)")
        return
    units = g["units"]
    labels = [f"{i + 1}단계 · {u.get('name', u.get('title', ''))} ({u.get('term', u.get('title', ''))})"
              for i, u in enumerate(units)]
    pick = st.selectbox("단계", labels)
    u = units[labels.index(pick)]
    st.caption(f"문제 {len(u.get('basic') or []) + len(u.get('drill') or [])}개 · 덩어리 예문 {len(u.get('sents') or [])}개 "
               "(앱에서는 문제와 예문 해석 고르기가 섞여서 계속 나와요)")

    with st.expander("📖 이름 · 핵심 설명 수정"):
        c1, c2 = st.columns(2)
        name = c1.text_input("단계 이름 (쉬운 말)", u.get("name", u.get("title", "")), key=f"nm_{u['id']}")
        term = c2.text_input("문법 이름 (작게 표시)", u.get("term", u.get("title", "")), key=f"tm_{u['id']}")
        core_text = st.text_area(
            "핵심 (덩어리마다 빈 줄로 구분 · 첫 줄은 제목, 다음 줄들은 짧은 예시)",
            "\n\n".join("\n".join([b.get("h", "")] + list(b.get("lines") or [])) for b in u.get("core") or []),
            height=240, key=f"co_{u['id']}")
        concept = st.text_area("예전 개념 설명 (핵심이 비어 있을 때만 앱에 보임)", u.get("concept", ""), height=120,
                               key=f"cc_{u['id']}")
        ex_text = st.text_area("예전 예문 (한 줄에 하나: 영어 | 우리말)",
                               "\n".join(f"{e['en']} | {e['ko']}" for e in u.get("examples") or []),
                               height=100, key=f"ex_{u['id']}")

    sent_delete = []
    with st.expander(f"🧩 덩어리 예문 ({len(u.get('sents') or [])})"):
        for i, x in enumerate(u.get("sents") or []):
            if st.checkbox(f"{x['en']}  →  {x.get('full', '')}", key=f"sdel_{u['id']}_{i}", help="체크하면 저장할 때 삭제"):
                sent_delete.append(i)
        st.markdown("**➕ 예문 추가** (비워 두면 추가 안 함)")
        s_en = st.text_input("영어 (덩어리 / 로)", key=f"sen_{u['id']}", placeholder="I / lost / my umbrella / yesterday.")
        s_ko = st.text_input("우리말 덩어리 (같은 개수)", key=f"sko_{u['id']}", placeholder="나는 / 잃어버렸다 / 내 우산을 / 어제")
        s_ok = _chunk_check(s_en, s_ko)
        s_full = st.text_input("완성 해석", key=f"sfu_{u['id']}")
        s_wrong = st.text_area("틀린 해석 (한 줄에 하나, 1~3개 — 헷갈리지만 분명히 틀린 해석)", key=f"swr_{u['id']}")
        st.caption("아래는 선택 — 채우면 앱에서 '단어 하나씩 · 우리말 순서 · 왜 이렇게 읽나요 · 틀린 이유'가 보여요")
        s_words = st.text_input("단어 뜻 (순서대로, 쉼표로: I=나는, lost=잃어버렸다, …)", key=f"sww_{u['id']}")
        s_ord = st.text_input("해석 순서 (덩어리 번호, 1부터: 예 1 5 4 3 2)", key=f"sor_{u['id']}")
        s_why = st.text_area("왜 이렇게 읽나요? (1~3문장)", key=f"swy_{u['id']}", height=80)
        s_wr = st.text_area("틀린 이유 (틀린 해석과 같은 순서로 한 줄씩)", key=f"swx_{u['id']}", height=80)

    type_names = {"c": "고르기", "b": "빈칸", "o": "배열"}
    to_delete = []
    for sec, secname in (("basic", "기본 문제"), ("drill", "다지기")):
        with st.expander(f"📋 {secname} 목록 ({len(u.get(sec) or [])})"):
            for i, q in enumerate(u.get(sec) or []):
                if q["t"] == "c":
                    desc = f"{q['q']}  → {q['o'][q['a']]}"
                elif q["t"] == "b":
                    desc = f"{q['q']}  → {' / '.join(q['a'])}"
                else:
                    desc = f"{q['ko']}  → {q['a']}"
                if st.checkbox(f"[{type_names.get(q['t'], '?')}] {desc}", key=f"del_{u['id']}_{sec}_{i}",
                               help="체크하면 저장할 때 삭제"):
                    to_delete.append((sec, i))

    st.markdown("**➕ 새 문제 추가**")
    a1, a2 = st.columns(2)
    add_sec = a1.selectbox("넣을 곳", ["basic", "drill"], format_func=lambda s: "기본 문제" if s == "basic" else "다지기")
    add_t = a2.selectbox("유형", ["c", "b", "o"], format_func=lambda t: {"c": "고르기 (보기 4개)", "b": "빈칸 쓰기", "o": "단어 배열"}[t])
    new_q = None
    if add_t == "c":
        q = st.text_input("문제 (빈칸은 ___ )", key="nq_c")
        opts = [st.text_input(f"보기 {k + 1}", key=f"nq_o{k}") for k in range(4)]
        ans = st.radio("정답", [1, 2, 3, 4], horizontal=True, key="nq_a")
        x = st.text_input("해설", key="nq_x")
        if q.strip() and all(o.strip() for o in opts):
            new_q = {"t": "c", "q": q.strip(), "o": [o.strip() for o in opts], "a": int(ans) - 1, "x": x.strip()}
    elif add_t == "b":
        q = st.text_input("문제 (빈칸 ___ 1개, 힌트는 괄호로)", key="nq_bq")
        a = st.text_input("정답 (여러 개면 | 로 구분, 예: cancelled|canceled)", key="nq_ba")
        x = st.text_input("해설", key="nq_bx")
        if q.strip() and a.strip():
            new_q = {"t": "b", "q": q.strip(), "a": [s.strip() for s in a.split("|") if s.strip()], "x": x.strip()}
    else:
        ko = st.text_input("우리말 문장", key="nq_ok")
        a = st.text_input("영어 정답 문장 (끝 마침표 없이, 단어는 띄어쓰기로 나눔)", key="nq_oa")
        x = st.text_input("해설", key="nq_ox")
        if ko.strip() and len(a.split()) >= 2:
            new_q = {"t": "o", "ko": ko.strip(), "a": a.strip().rstrip(".?!"), "x": x.strip()}

    if st.button("💾 이 단계 저장 → 앱에 적용", type="primary", use_container_width=True):
        g2 = _j.loads(_j.dumps(g))
        u2 = next(x for x in g2["units"] if x["id"] == u["id"])
        u2["concept"] = concept.strip()
        exs = []
        for line in ex_text.splitlines():
            if "|" in line:
                en, ko = line.split("|", 1)
                if en.strip() and ko.strip():
                    exs.append({"en": en.strip(), "ko": ko.strip()})
        u2["examples"] = exs
        u2["name"] = name.strip() or u2.get("title", "")
        u2["term"] = term.strip() or u2.get("title", "")
        blocks = []
        for para in [x for x in core_text.split("\n\n") if x.strip()]:
            ls = [x.strip() for x in para.splitlines() if x.strip()]
            blocks.append({"h": ls[0], "lines": ls[1:]})
        u2["core"] = blocks
        sents = [x for i, x in enumerate(u2.get("sents") or []) if i not in sent_delete]
        if s_en.strip():
            if not s_ok:
                st.error("추가할 예문의 덩어리 수를 맞춰 주세요")
                return
            ns = {"en": s_en.strip(), "ko": s_ko.strip(), "full": s_full.strip(),
                  "wrong": [w.strip() for w in s_wrong.splitlines() if w.strip()][:3]}
            pairs = [t.split("=", 1) for t in s_words.split(",") if "=" in t]
            if pairs:
                ns["w"] = [[a.strip(), b.strip()] for a, b in pairs if a.strip()]
            if s_ord.strip():
                try:
                    ns["ord"] = [int(t) - 1 for t in s_ord.replace(",", " ").split()]
                except ValueError:
                    st.error("해석 순서는 숫자만 적어 주세요 (예: 1 5 4 3 2)")
                    return
            if s_why.strip():
                ns["why"] = s_why.strip()
            wr = [t.strip() for t in s_wr.splitlines() if t.strip()]
            if wr:
                ns["wr"] = wr[:len(ns["wrong"])]
            sents.append(ns)
        u2["sents"] = sents
        for sec, i in sorted(to_delete, key=lambda t: -t[1]):
            del u2[sec][i]
        if new_q:
            u2.setdefault(add_sec, []).append(new_q)
        for sec in ("basic", "drill"):
            for i, q in enumerate(u2.get(sec) or []):
                q["id"] = f"{u2['id']}{sec[0]}{i + 1}"
        errs = validate_grammar(g2)
        if errs:
            st.error("저장 안 됨 — 확인해 주세요:\n" + "\n".join(errs[:10]))
        else:
            try:
                commit_files({GRAMMAR_FILE: _j.dumps(g2, ensure_ascii=False, indent=1).encode("utf-8")},
                             f"grammar: {u2['id']} 수정")
                st.success(f"저장 완료! (삭제 {len(to_delete)}개" + (", 추가 1개" if new_q else "") +
                           ") 앱은 다음에 홈을 열 때 자동으로 받아요. 이 화면은 1~2분 뒤 새로고침하면 반영돼요.")
            except Exception as e:
                st.error(f"GitHub 저장 실패: {e}")

    with st.expander("🛠 전체 JSON 직접 편집 (고급)"):
        raw = st.text_area("grammar.json", _j.dumps(g, ensure_ascii=False, indent=1), height=300)
        if st.button("검사 후 저장", key="raw_save"):
            try:
                g3 = _j.loads(raw)
            except Exception as e:
                st.error(f"JSON 형식 오류: {e}")
                return
            errs = validate_grammar(g3)
            if errs:
                st.error("저장 안 됨:\n" + "\n".join(errs[:10]))
            else:
                commit_files({GRAMMAR_FILE: _j.dumps(g3, ensure_ascii=False, indent=1).encode("utf-8")},
                             "grammar: 전체 수정")
                st.success("저장 완료!")


def _save_reading(d, msg):
    import json as _j
    errs = validate_reading(d)
    if errs:
        st.error("저장 안 됨 — 확인해 주세요:\n" + "\n".join(errs[:10]))
        return False
    try:
        commit_files({READING_FILE: _j.dumps(d, ensure_ascii=False, indent=1).encode("utf-8")}, msg)
    except Exception as e:
        st.error(f"GitHub 저장 실패: {e}")
        return False
    st.success("저장 완료! 앱은 다음에 홈을 열 때 자동으로 받아요. 이 화면은 1~2분 뒤 새로고침하면 반영돼요.")
    return True


def _next_id(existing, prefix):
    n = 1
    ids = set(existing)
    while f"{prefix}{n:02d}" in ids:
        n += 1
    return f"{prefix}{n:02d}"


def _chunk_check(en, ko):
    e, k = _chunks(en), _chunks(ko)
    if en.strip() and ko.strip():
        if len(e) == len(k):
            st.caption("✅ 덩어리 " + str(len(e)) + "개: " + " · ".join(f"{a} → {b}" for a, b in zip(e, k)))
        else:
            st.warning(f"덩어리 수가 달라요: 영어 {len(e)}개 / 우리말 {len(k)}개 ( / 앞뒤 띄어쓰기 확인)")
    return bool(e) and len(e) == len(k)


def page_reading_admin():
    st.subheader("📄 짧은 글 관리")
    if not check_admin():
        return
    import json as _j
    d = load_reading()
    if d is None:
        st.info("reading.json 이 없어요 (중등판 저장소에서만 사용)")
        return
    d.setdefault("passages", [])
    st.caption("끊어 읽기는 영어와 우리말을 모두 ' / ' (띄어쓰기-슬래시-띄어쓰기)로 나누고, 덩어리 수를 똑같이 맞춰요.")
    kind = st.radio("무엇을 할까요?", ["📄 글 추가·수정", "🛠 전체 JSON"], horizontal=True)

    if kind.startswith("📄"):
        labels = [f"{x['id']} · {x.get('title', '')} ({x.get('tk', '')})" for x in d["passages"]]
        pick = st.selectbox("글", labels + ["➕ 새 글 만들기"])
        new = pick == "➕ 새 글 만들기"
        p_ = {"id": _next_id([x["id"] for x in d["passages"]], "p"), "title": "", "tk": "",
              "sents": [], "qs": [], "words": []} if new else d["passages"][labels.index(pick)]
        k = p_["id"]
        c1, c2 = st.columns(2)
        title = c1.text_input("제목 (영어)", p_.get("title", ""), key=f"pt_{k}")
        tk = c2.text_input("제목 (우리말)", p_.get("tk", ""), key=f"pk_{k}")
        body = st.text_area("본문 — 한 줄에 한 문장:  영어 덩어리 | 우리말 덩어리 | 완성 해석",
                            "\n".join(f"{s2['en']} | {s2['ko']} | {s2.get('full', '')}" for s2 in p_.get("sents") or []),
                            height=300, key=f"pb_{k}",
                            placeholder="Every morning, / I / walk to work. | 매일 아침 / 나는 / 걸어서 출근한다 | 나는 매일 아침 걸어서 출근한다.")
        sents, bad = [], []
        for n, line in enumerate(body.splitlines(), 1):
            if not line.strip():
                continue
            parts = [x.strip() for x in line.split("|")]
            if len(parts) < 3:
                bad.append(f"{n}번 줄: 영어 | 우리말 덩어리 | 완성 해석 세 칸이 필요해요"); continue
            en, ko, full = parts[0], parts[1], "|".join(parts[2:]).strip()
            if len(_chunks(en)) != len(_chunks(ko)):
                bad.append(f"{n}번 줄: 덩어리 수가 달라요 (영어 {len(_chunks(en))} / 우리말 {len(_chunks(ko))})")
            if not full:
                bad.append(f"{n}번 줄: 완성 해석이 비어 있어요")
            prev = next((x for x in (p_.get("sents") or []) if x.get("en") == en and x.get("ko") == ko), None)
            item = {"en": en, "ko": ko, "full": full}
            if prev:   # 영어·덩어리가 그대로면 자세히 보기 자료(단어 뜻·해석 순서·설명) 유지
                for key in ("w", "ord", "why"):
                    if key in prev:
                        item[key] = prev[key]
            sents.append(item)
        if bad:
            st.warning("\n".join(bad[:8]))
        elif sents:
            st.caption(f"✅ {len(sents)}문장, 덩어리 수 모두 맞음")
        words = st.text_area("어휘 (한 줄에 하나: 단어 | 뜻)",
                             "\n".join(f"{w['en']} | {w['ko']}" for w in p_.get("words") or []), height=100, key=f"pw_{k}")
        to_delete = []
        with st.expander(f"📋 문제 목록 ({len(p_.get('qs') or [])})"):
            for i, q in enumerate(p_.get("qs") or []):
                ans = q["o"][q["a"]] if 0 <= q.get("a", -1) < len(q.get("o") or []) else "?"
                if st.checkbox(f"{q['q']}  →  {ans}", key=f"pq_{k}_{i}", help="체크하면 저장할 때 삭제"):
                    to_delete.append(i)
        st.markdown("**➕ 문제 추가** (비워 두면 추가 안 함 · 유형은 [주제] [빈칸] 처럼 앞에 표시)")
        q = st.text_input("문제", key=f"nq_{k}")
        opts = [st.text_input(f"보기 {n + 1}" + (" (없어도 됨)" if n == 4 else ""), key=f"no_{k}_{n}") for n in range(5)]
        a = st.radio("정답", [1, 2, 3, 4, 5], horizontal=True, key=f"na_{k}")
        x = st.text_input("해설 (근거 문장 등)", key=f"nx_{k}")
        if st.button("💾 글 저장 → 앱에 적용", type="primary", use_container_width=True):
            used = [o.strip() for o in opts if o.strip()]
            if not title.strip() or not sents:
                st.error("제목과 본문을 입력해 주세요")
            elif bad:
                st.error("본문 줄을 먼저 고쳐 주세요")
            elif q.strip() and (len(used) < 4 or any(not o.strip() for o in opts[:4])):
                st.error("보기를 4개 이상(1~4번은 꼭) 입력해 주세요")
            elif q.strip() and int(a) > len(used):
                st.error("정답 번호에 해당하는 보기가 없어요")
            else:
                d2 = _j.loads(_j.dumps(d))
                wl = []
                for line in words.splitlines():
                    if "|" in line:
                        w1, w2 = [t.strip() for t in line.split("|", 1)]
                        if w1:
                            wl.append({"en": w1, "ko": w2})
                qs = [qq for i, qq in enumerate(p_.get("qs") or []) if i not in to_delete]
                if q.strip():
                    qs.append({"q": q.strip(), "o": used, "a": int(a) - 1, "x": x.strip()})
                np_ = {"id": k, "title": title.strip(), "tk": tk.strip(), "sents": sents, "qs": qs, "words": wl}
                if new:
                    d2["passages"].append(np_)
                else:
                    d2["passages"] = [np_ if pp["id"] == k else pp for pp in d2["passages"]]
                _save_reading(d2, f"reading: 글 {k} " + ("추가" if new else "수정"))
    else:
        st.caption("글 삭제·순서 바꾸기는 여기서 해요. 저장 전에 자동으로 검사해요.")
        raw = st.text_area("reading.json", _j.dumps(d, ensure_ascii=False, indent=1), height=360)
        if st.button("검사 후 저장", key="raw_reading"):
            try:
                d3 = _j.loads(raw)
            except Exception as e:
                st.error(f"JSON 형식 오류: {e}")
                return
            _save_reading(d3, "reading: 전체 수정")


def page_admin(words):
    st.subheader("⚙️ 단어 관리 (보호자)")

    if not check_admin():
        return

    st.markdown("한 줄에 한 단어씩 · 형식: `영어,뜻,이모지,그룹` — 그룹은 아이 화면에 그대로 보이니 읽기 쉬운 이름으로 (예: 중1-1 학교생활) · "
                "**뜻에 쉼표(,) 금지** — 여러 뜻은 `·` 로 (예: `lie,거짓말·눕다,🤥,중2-2 친구·관계`)")
    cur_text = "\n".join(
        ",".join([w["en"], w["ko"], w["emoji"], w["group"]]).rstrip(",") for w in words)
    text = st.text_area("단어 목록 (이 내용 전체가 그대로 저장됩니다)", cur_text, height=320)

    # 현재 입력칸 기준 단어 수 (저장 전 검증용)
    parsed = []
    for _line in text.splitlines():
        _p = [x.strip() for x in re.split(r"[,\t]", _line.strip(), maxsplit=3)]
        if len(_p) >= 2 and _p[0] and _p[1] and _p[0] != "en":
            parsed.append(_p + ["", ""])
    if parsed:
        from collections import Counter
        _g = Counter((p[3] or "그룹없음") for p in parsed)
        _detail = " · ".join(f"{k} {v}" for k, v in _g.items())
        st.caption(f"📚 지금 목록: **{len(parsed)}개** — {_detail}")
    else:
        st.caption("📚 지금 목록: 0개")
    # 쉼표가 4칸을 넘는 줄은 앱/웹에서 뜻·그룹이 잘려 들어감 → 저장 전에 경고
    _bad = [_l.strip() for _l in text.splitlines()
            if _l.strip() and len(re.split(r"[,\t]", _l.strip())) > 4]
    if _bad:
        st.warning(f"⚠ 쉼표가 너무 많은 줄 {len(_bad)}개 — 뜻 안의 쉼표는 `·` 로 바꿔 주세요: "
                   + " / ".join(_bad[:5]) + (" …" if len(_bad) > 5 else ""))

    # 서버 발음 파일 상태 — 저장 후 페이지가 새로고침돼도 여기서 항상 확인 가능
    _targets = [p[0] for p in parsed] + PRAISES
    _have = sum(1 for t in _targets if audio_path(t))
    _miss = len(_targets) - _have
    if _miss == 0 and _targets:
        st.success(f"🔊 발음 파일: 전부 준비됨 ✅ ({_have}/{len(_targets)})")
    else:
        st.info(f"🔊 발음 파일: {_have}/{len(_targets)} · 부족 {_miss}개 — 저장을 누르면 부족분만 자동 생성돼요")

    voice = st.selectbox("발음 목소리", VOICES)
    regen = st.checkbox("모든 발음을 이 목소리로 다시 생성 (목소리를 바꿨을 때 체크)")
    if regen:
        st.warning(f"⚠ 전체 {len(_targets)}개를 처음부터 다시 만듭니다 (10분 이상 소요). "
                   "새 단어만 추가하는 경우라면 체크를 해제하세요!")
        if len(_targets) > MAX_TTS_PER_SAVE:
            st.error(f"단어가 {len(_targets)}개라 전체 다시 만들기는 30분 이상 걸리고, 도중에 창을 닫으면 "
                     "처음부터 다시 해야 해요. 가능하면 PC에서 창을 켜 둔 채로 진행하세요.")

    if st.button("💾 저장 → 웹/앱에 적용", type="primary", use_container_width=True):
        new_words = []
        for line in text.splitlines():
            parts = [p.strip() for p in re.split(r"[,\t]", line.strip(), maxsplit=3)]
            if len(parts) >= 2 and parts[0] and parts[1] and parts[0] != "en":
                parts += ["", ""]
                new_words.append({"en": parts[0], "ko": parts[1],
                                  "emoji": parts[2], "group": parts[3]})
        if not new_words:
            st.error("저장할 단어가 없어요. 형식: 영어,뜻")
            return

        buf = io.StringIO()
        buf.write("en,ko,emoji,group\n")
        for w in new_words:
            buf.write(",".join([w["en"], w["ko"], w["emoji"], w["group"]]) + "\n")
        files = {"words.csv": buf.getvalue().encode("utf-8")}

        texts = [w["en"] for w in new_words] + PRAISES
        todo = [t for t in texts if regen or not audio_path(t)]
        # 한 번에 너무 많이 만들면(수백 개) 도중에 끊겼을 때 전부 날아감 → 나눠서 저장
        # (목소리 전체 교체(regen)는 나누면 옛 목소리와 섞이므로 예외 — 한 번에 전부)
        remain = 0 if regen else max(0, len(todo) - MAX_TTS_PER_SAVE)
        if not regen:
            todo = todo[:MAX_TTS_PER_SAVE]
        if regen:
            import time as _time
            files["audio/_v.txt"] = str(int(_time.time())).encode("utf-8")
        prog = st.progress(0.0, text="원어민 발음 생성 중…")
        for i, t in enumerate(todo):
            try:
                files[f"audio/{slug(t)}.mp3"] = gen_mp3(t, voice)
            except Exception as e:
                st.warning(f"{t} 발음 생성 실패: {e}")
            prog.progress((i + 1) / max(len(todo), 1), text=f"발음 생성: {t}")
        prog.empty()

        try:
            commit_files(files, f"words: {len(new_words)}개 저장 (신규 발음 {len(todo)}개)")
        except Exception as e:
            st.error(f"GitHub 저장 실패: {e}")
            return

        st.success(
            f"저장 완료! 단어 {len(new_words)}개 · 발음 {len(todo)}개 생성. "
            "웹은 1~2분 안에 자동 재시작되며, 앱은 다음 실행(또는 단어 관리의 서버 동기화) 때 반영됩니다.")
        if remain:
            st.warning(f"🔊 아직 발음 {remain}개가 남았어요. 1~2분 뒤 페이지를 새로고침하고 "
                       "💾 저장을 한 번 더 누르면 이어서 만들어요 (이미 만든 건 건너뜀).")

    st.divider()
    st.markdown("#### ⚙️ 앱 원격 설정 — 재빌드 없이 즉시 반영")
    import json as _cjson
    _cfg = {}
    if os.path.exists("config.json"):
        try:
            _cfg = _cjson.load(open("config.json", encoding="utf-8"))
        except Exception:
            pass
    st.markdown("**학습 흐름 (새 단어 묶음제)**")
    st.caption("새 단어 N개 배우기 → 그 N개로 퀴즈 → 다 맞으면 통과 · 틀리면 틀린 단어 배우고 다시"
               + (" → 하루 상한까지" if SHOW_STARS else ""))
    bc1, bc2 = st.columns(2)
    _batch = bc1.number_input("새 단어 묶음 크기 (= 퀴즈 문제 수)", 3, 50,
                              int(_cfg.get("batchSize", 10)))
    if SHOW_STARS:
        _cap = bc2.number_input("하루 별 상한 (0=무제한)", 0, 200, int(_cfg.get("dailyStarCap", 10)))
    else:
        _cap = 0
        bc2.caption("중등판은 별·하루 상한이 없어요 (진도는 5가지 퀴즈 통과로만)")
    _bstar = int(_cfg.get("batchStar", 5))  # (미사용 · 모드별 1개 고정)
    if SHOW_STARS:
        st.caption("퀴즈는 5가지(듣기·영어·한글·섞어서·철자)이고 각 만점마다 ⭐1개 → 한 묶음에서 최대 ⭐5개. "
                   f"하루 상한 {int(_cap)}개면 하루 최대 {int(_cap)//5 if _cap else '∞'}묶음까지 별을 받아요 (그 뒤엔 공부만).")
    else:
        st.caption("퀴즈 5가지(듣기·영어·한글·섞어서·철자)를 모두 만점으로 통과하면 다음 새 단어 묶음으로 넘어가요.")

    _stage = st.checkbox("📘 단원제 (단원을 순서대로 · 한 단원 익히면 다음 단원 해금)",
                         value=bool(_cfg.get("stageMode", 1)))
    if _stage:
        _ratio = st.slider("다음 단원 열리는 기준 (현재 단원 마스터 비율)",
                           50, 100, int(float(_cfg.get("stageUnlockRatio", 0.7)) * 100), step=5,
                           format="%d%%")
    else:
        _ratio = int(float(_cfg.get("stageUnlockRatio", 0.7)) * 100)
        st.caption("단원제를 끄면 배운 단어 전체에서 묶음이 나와요.")
    cc4, cc5, cc6 = st.columns(3)
    _retry = cc4.number_input("따라 말하기 재시도", 1, 9, int(_cfg.get("speakRetries", 3)))
    _slow = cc5.number_input("느린 발음 속도", 0.4, 1.0, float(_cfg.get("slowSpeed", 0.65)), step=0.05)
    _len = cc6.selectbox("말하기 판정 관대함", [0, 1, 2, 3],
        index=int(_cfg.get("speakLeniency", 2)),
        format_func=lambda v: {0:"0 엄격", 1:"1 보통", 2:"2 관대(기본)", 3:"3 매우 관대"}[v])
    st.caption("말하기 인식이 자꾸 실패하면 관대함을 3으로 올려보세요. 아무 소리나 통과되면 1로 낮추세요.")
    _rm_on, _rm_time, _rm_days, _rm_msg = _cfg.get("remind", 1), _cfg.get("remindTime", "20:00"), \
        _cfg.get("remindDays", "1234567"), _cfg.get("remindMsg", "")
    if not SHOW_STARS:   # 중등판: 하루 한 번 공부 알림
        import datetime as _dt
        st.markdown("**🔔 공부 알림** — 그날 아직 공부하지 않았을 때만, 알람 화면 + 알림으로 한 번 울려요")
        _rm_on = 1 if st.checkbox("알림 켜기", value=bool(int(_cfg.get("remind", 1)))) else 0
        ra, rb = st.columns([1, 2])
        try:
            _h, _m = [int(x) for x in str(_rm_time).split(":")[:2]]
            _t0 = _dt.time(_h, _m)
        except Exception:
            _t0 = _dt.time(20, 0)
        _t = ra.time_input("알림 시간", value=_t0, step=300)
        _rm_time = f"{_t.hour:02d}:{_t.minute:02d}"
        _names = ["월", "화", "수", "목", "금", "토", "일"]
        _sel = rb.multiselect("요일", _names, default=[_names[int(c) - 1] for c in str(_rm_days) if c in "1234567"] or _names)
        _rm_days = "".join(str(_names.index(n) + 1) for n in _names if n in _sel) or "1234567"
        _rm_msg = st.text_input("알림 문구 (비우면 '오늘 영어, 잠깐 들러 볼까요?')", value=str(_cfg.get("remindMsg", "")), max_chars=40)
        st.caption("'30분 뒤에 다시'를 누르면 30분 뒤 한 번 더 울려요. 시간을 바꾸면 친구 폰이 다음에 앱을 열 때 적용돼요.")
    _abc = st.checkbox("🔤 홈에 '알파벳 소리' 버튼 보이기 (파닉스용 · 중등판은 보통 끔)",
                       value=bool(_cfg.get("showAbc", 0)))
    _child = st.text_input("👤 아이 이름 (학습 리포트·관리자 앱에 표시 · 비우면 폰 기종 이름)",
                           value=str(_cfg.get("childName", "")), max_chars=20)
    st.caption("저장하면 아이 폰이 다음에 앱을 열 때 조용히 자동 적용돼요 (아이 화면에 안내 없음). "
               "적용 확인은 각 폰의 관리자 대시보드 상단에서.")
    if st.button("⚙️ 설정 저장 → 앱에 적용", use_container_width=True):
        # 이 화면에 없는 키도 그대로 보존 (예: quizAll, starMinCorrect) → 저장할 때 설정이 사라지지 않게
        body = _cjson.dumps({
            **_cfg,
            "showAbc": 1 if _abc else 0,
            "childName": _child.strip(),
            "batchSize": int(_batch),
            "batchStar": int(_bstar),
            "dailyStarCap": int(_cap),
            "stageMode": 1 if _stage else 0,
            "stageUnlockRatio": round(_ratio / 100, 2),
            "speakRetries": int(_retry),
            "speakLeniency": int(_len),
            "slowSpeed": round(float(_slow), 2),
            "ttsSlow": float(_cfg.get("ttsSlow", 0.45)),
            "ttsNormal": float(_cfg.get("ttsNormal", 0.8)),
            "weakFirst": int(_cfg.get("weakFirst", 7)),
            "remind": int(_rm_on),
            "remindTime": str(_rm_time),
            "remindDays": str(_rm_days),
            "remindMsg": str(_rm_msg).strip(),
        }).encode("utf-8")
        try:
            commit_files({"config.json": body}, "config: 원격 설정 변경")
            st.success("설정 저장 완료! 폰은 다음에 앱을 열 때 자동 적용됩니다.")
        except Exception as e:
            st.error(f"저장 실패: {e}")

    st.divider()
    st.markdown("#### 📲 앱 업데이트 배포")
    cur_v, cur_code = "없음", 0
    if os.path.exists("app/version.json"):
        try:
            import json as _json
            v = _json.load(open("app/version.json", encoding="utf-8"))
            cur_code = int(v.get("versionCode") or 0)
            cur_v = f"v{v.get('versionName')} (code {cur_code})"
        except Exception:
            pass
    st.caption(f"서버에 올라온 버전: {cur_v} · 올리면 앱의 관리자 화면 > 🔄 앱 업데이트에서 받을 수 있어요")

    c1, c2 = st.columns(2)
    vname = c1.text_input("versionName (예: 2.2)")
    vcode = c2.number_input(
        "versionCode — build.gradle의 versionCode와 같게, 이전보다 크게",
        min_value=1, step=1, value=max(cur_code + 1, 1),
    )
    if cur_code and vcode <= cur_code:
        st.warning(f"지금 서버 코드({cur_code})보다 커야 폰에서 업데이트 버튼이 켜져요")
    apk = st.file_uploader("서명된 APK 파일", type=["apk"])

    if st.button("📲 APK 업로드", use_container_width=True):
        if not apk or not vname.strip():
            st.error("APK 파일과 versionName 을 입력해 주세요")
        else:
            import json as _json
            data = apk.getvalue()
            meta = _json.dumps({"versionCode": int(vcode), "versionName": vname.strip(),
                                "size": len(data)}).encode("utf-8")
            try:
                with st.spinner(f"업로드 중… ({len(data)//1024} KB)"):
                    commit_files({"app/wordfriend.apk": data, "app/version.json": meta},
                                 f"app: v{vname.strip()} (code {int(vcode)})")
                st.success(f"업로드 완료! 앱에서 [관리자 → 🔄 앱 업데이트] 로 설치할 수 있어요")
            except Exception as e:
                st.error(f"GitHub 저장 실패: {e}")


# ---------- 메인 ----------

words = load_words()
st.markdown(f"## {APP_ICON} {APP_TITLE}")
_menu = ["📖 단어 배우기", "🎯 퀴즈", "📊 학습 리포트", "⚙️ 단어 관리"]
if os.path.exists(GRAMMAR_FILE):
    _menu.append("📐 문장 공부 관리")
if os.path.exists(READING_FILE):
    _menu.append("📄 짧은 글 관리")
tab = st.sidebar.radio("메뉴", _menu)
if tab.startswith("📖"):
    page_learn(words)
elif tab.startswith("🎯"):
    page_quiz(words)
elif tab.startswith("📊"):
    page_report(words)
elif tab.startswith("📐"):
    page_grammar_admin()
elif tab.startswith("📄"):
    page_reading_admin()
else:
    page_admin(words)
