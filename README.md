# jay-brows-dashboard

인스타그램 + 페이스북 페이지의 통계와 댓글을 한 화면에서 보는 대시보드.

- `fetch.py` — Meta Graph API에서 데이터를 받아 `site/data.json` 생성 (표준 라이브러리만 사용)
- `site/index.html` — 대시보드 화면
- `.github/workflows/update.yml` — 5분마다 실행 → GitHub Pages에 배포

## 설정
1. 인스타그램을 프로페셔널 계정으로 전환하고 페이스북 페이지에 연결
2. Meta 앱에서 장기 **페이지 액세스 토큰** 발급
   (권한: `pages_show_list`, `pages_read_engagement`, `pages_read_user_content`, `read_insights`,
   `instagram_basic`, `instagram_manage_insights`, `instagram_manage_comments`)
3. Settings → Secrets and variables → Actions → `META_TOKEN` 시크릿에 토큰 저장
4. Actions → Update dashboard → Run workflow

페이지가 여러 개면 `PAGE_ID` 변수(Variables)로 지정.

로컬 테스트: `META_TOKEN=... python fetch.py && python -m http.server -d site`
