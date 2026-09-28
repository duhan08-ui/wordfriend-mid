# 단어친구 중등 (wordfriend-mid)

중학생용 **별도 운영** 저장소. 초등판(duhan08-ui/wordfriend)과 파일·토큰·테이블을 하나도 공유하지 않음.

- 단어장: `words.csv` (954개 · 21단원, 중1-1 → 중3-7 순서로 해금)
- 원어민 발음: `audio/*.mp3` (웹 관리 페이지가 edge-tts로 자동 생성)
- 앱 원격 설정: `config.json` / 앱 업데이트: `app/version.json`, `app/wordfriend.apk`
- 웹: `streamlit_app.py` (초등판과 같은 코드 + Secrets 로 테이블/제목 전환)

## 처음 세팅 (순서대로)

1. **저장소**: github.com → New repository → `wordfriend-mid`, **Public** → 이 폴더 파일 업로드
   (`.github/workflows/keepalive.yml` 은 6번에서 웹 편집기로 따로 붙여넣기)
2. **토큰**: Settings → Developer settings → Fine-grained tokens → Generate new token
   - Repository access: **Only select repositories → wordfriend-mid 하나만**
   - Permissions: Contents **Read and write**
   - (초등판 토큰을 재사용하지 말 것 — 한 토큰이 두 저장소에 쓰면 실수로 섞일 수 있음)
3. **Supabase**: 단어친구 프로젝트 → SQL Editor → `supabase_wf_stats_mid.sql` 붙여넣기 → Run
   → 맨 아래 확인 표 3개에서 wf_stats / wf_stats_mid 가 같으면 OK
4. **Streamlit**: share.streamlit.io → Create app → 저장소 `duhan08-ui/wordfriend-mid`, 브랜치 `main`,
   파일 `streamlit_app.py`, App URL 원하는 이름(예: `wordmid0518`) → Advanced settings → Secrets:

   ```toml
   GH_REPO = "duhan08-ui/wordfriend-mid"
   GH_BRANCH = "main"
   GH_TOKEN = "github_pat_새로만든토큰"
   ADMIN_PASSWORD = "관리비밀번호"
   SUPA_TABLE = "wf_stats_mid"
   APP_TITLE = "단어친구 중등"
   ```
5. **발음 생성**: 웹 → ⚙️ 단어 관리 → 비밀번호 → 목소리 `en-US-JennyNeural`(기본, 성인 여성) → 💾 저장
   - 한 번에 250개씩 만들어짐 (끊겨도 앞부분은 저장됨). 끝나면 1~2분 기다렸다 **새로고침 → 저장**을
     "🔊 발음 파일: 전부 준비됨 ✅ (959/959)" 가 뜰 때까지 반복 (총 4번)
6. **잠들기 방지**: 저장소 → Add file → Create new file → 이름 `.github/workflows/keepalive.yml`
   → 내용 붙여넣기 → `URL = "https://여기에-새-앱-주소.streamlit.app/"` 한 줄을 4번의 주소로 수정 → Commit
   → Actions 탭 → keep-streamlit-awake → Run workflow 로 한 번 시험 (로그에 "앱 화면 확인됨")
7. **앱**: Android Studio 에서 중등판 프로젝트 폴더(초등판과 **다른 폴더**) 열기 →
   Build → Generate Signed APK → 기존 keystore `C:\keystore\wordfriend.jks` → release
   → 웹 📲 앱 업데이트 배포에 APK + versionName `1.0` / versionCode `1` 업로드
8. **친구 폰 첫 설치**: 폰 브라우저로
   `https://raw.githubusercontent.com/duhan08-ui/wordfriend-mid/main/app/wordfriend.apk`
   → 설치(출처 허용). 시작 PIN 어린이 1111 / 관리자 9999 → 관리자 화면에서 PIN 변경·기기 이름 입력.
   이후 업데이트는 웹에 새 APK(versionCode 2, 3 …) 올리면 앱이 스스로 안내.

## 평소 운영

- 단어 추가·수정: 웹 ⚙️ 단어 관리 (`영어,뜻,이모지,그룹` · **뜻에 쉼표 금지, 여러 뜻은 `·`**)
- 숫자 설정(묶음 크기, 하루 별 상한, 알파벳 버튼 등): 같은 화면 아래 "앱 원격 설정" — 재빌드 불필요
- 학습 기록: 웹 📊 학습 리포트 (wf_stats_mid 만 읽음)

## 참고

- 공개 저장소는 60일간 커밋이 없으면 GitHub가 예약 실행을 멈춤. keepalive.yml 마지막 단계가
  워크플로를 스스로 다시 활성화하도록 해 두었지만, 효과는 60일 뒤에야 확인 가능.
- `tools/gen_audio.py`: (예비용) PC에서 `pip install edge-tts` 후 `python tools/gen_audio.py` 로
  발음을 한꺼번에 만들어 git push 하는 방법.
