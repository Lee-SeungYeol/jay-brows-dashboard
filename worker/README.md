# jay-brows-reply (Cloudflare Worker)

대시보드에서 인스타그램 댓글에 바로 답글을 다는 작은 프록시예요. Meta 토큰은 Worker 비밀값에만 저장되고, 브라우저에는 노출되지 않아요.

- `POST /check` — 비밀번호 확인 (`Authorization: Bearer <REPLY_PASSWORD>`) → `{ok:true}`
- `POST /reply` — `{comment_id, message}` → Graph API `/{comment_id}/replies` 로 답글 → `{ok:true,id}` / `{ok:false,error}`
- CORS는 `ALLOWED_ORIGIN`(https://lee-seungyeol.github.io)만 허용, 비밀번호 틀리면 1초 지연 후 401.

## 배포

```sh
cd worker
npx wrangler login
npx wrangler secret put META_TOKEN       # instagram_manage_comments 권한 있는 시스템 사용자 토큰
npx wrangler secret put REPLY_PASSWORD   # 대시보드에서 쓸 비밀번호
npx wrangler deploy
```

배포 후 나오는 주소(`https://jay-brows-reply.<계정>.workers.dev`)를 `site/index.html` 맨 위의 `const REPLY_API = "";` 에 넣으면 답글 기능이 켜져요.
