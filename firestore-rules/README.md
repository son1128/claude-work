# ClientBase CRM Firestore 보안 규칙

「Google Antigravity로 풀스택 CRM 만들기」 인포그래픽 분석 보고서에서 정리한 ClientBase CRM의 역할별 Firestore 보안 규칙과, 이를 검증하는 에뮬레이터 테스트입니다.

| 파일 | 내용 |
| --- | --- |
| [`firestore.rules`](firestore.rules) | 역할별(관리자·영업·열람자) 보안 규칙. 딜은 담당 영업만 수정·삭제 |
| [`rules.test.js`](rules.test.js) | 에뮬레이터 테스트 20개 |
| [`firebase.json`](firebase.json) | 에뮬레이터 설정 |
| [`vitest.config.mjs`](vitest.config.mjs) | 테스트 실행 설정 |

## 권한 요약

| 권한 | 관리자 | 영업 | 열람자 |
| --- | --- | --- | --- |
| 팀 CRM 데이터 읽기 | 가능 | 가능 | 가능 |
| 연락처·회사·노트 작성·수정 | 가능 | 가능 | 불가 |
| 연락처·회사·노트 삭제 | 모든 문서 | 본인이 작성한 문서만 | 불가 |
| 딜 작성 | 팀원 누구에게나 배정 | 본인을 담당자로만 | 불가 |
| 딜 수정·삭제 | 모든 딜 | 본인이 담당인 딜만 | 불가 |
| 딜 담당자 변경 | 가능 | 불가 | 불가 |
| 팀원 역할 변경 | 가능(본인 제외) | 불가 | 불가 |
| 팀 배정·계정 추가 | 콘솔/Admin SDK에서만 | 불가 | 불가 |

데이터 구조: `users/{uid}`에 `teamId`·`role`(`admin`/`sales`/`viewer`), 모든 CRM 문서(`contacts`·`companies`·`deals`·`notes`)에 `teamId`·`createdBy`, 딜에는 담당자 `ownerUid`가 있어야 합니다.

## 테스트 실행

Node.js 20 이상과 Java 11 이상(에뮬레이터용)이 필요합니다.

```
cd firestore-rules
npm install
npm test
```

`npm test`는 Firestore 에뮬레이터를 띄워 테스트를 돌린 뒤 종료합니다. 프로젝트 ID가 `demo-`로 시작해 실제 Firebase 프로젝트에는 연결되지 않습니다.

규칙을 고칠 때마다 배포 전에 `npm test`를 다시 실행하세요. 권한을 의도적으로 바꿨다면 해당 테스트의 기대값(`assertSucceeds`/`assertFails`)도 함께 바꿔야 합니다.

## 실제 프로젝트에 배포

```
npx firebase deploy --only firestore:rules --project <프로젝트 ID>
```
