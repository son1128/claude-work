import { readFileSync } from "node:fs";
import { afterAll, beforeAll, beforeEach, describe, test } from "vitest";
import {
  initializeTestEnvironment,
  assertSucceeds,
  assertFails,
} from "@firebase/rules-unit-testing";
import {
  doc, getDoc, getDocs, setDoc, updateDoc, deleteDoc,
  collection, query, where,
} from "firebase/firestore";

let env;

// 역할별 사용자에 맞는 Firestore 클라이언트
const as = (uid) => env.authenticatedContext(uid).firestore();
const anon = () => env.unauthenticatedContext().firestore();

beforeAll(async () => {
  env = await initializeTestEnvironment({
    projectId: "demo-clientbase",
    firestore: { rules: readFileSync("firestore.rules", "utf8") },
  });
});

afterAll(async () => {
  await env.cleanup();
});

// 테스트마다 데이터를 지우고 규칙을 우회해 기본 데이터를 다시 넣음
beforeEach(async () => {
  await env.clearFirestore();
  await env.withSecurityRulesDisabled(async (ctx) => {
    const db = ctx.firestore();
    const users = {
      kim:   { teamId: "team_seoul", role: "admin" },
      lee:   { teamId: "team_seoul", role: "sales" },
      jung:  { teamId: "team_seoul", role: "sales" },
      park:  { teamId: "team_seoul", role: "viewer" },
      other: { teamId: "team_busan", role: "admin" },
    };
    for (const [uid, data] of Object.entries(users)) {
      await setDoc(doc(db, "users", uid), data);
    }
    await setDoc(doc(db, "contacts", "ct_choi"),
      { teamId: "team_seoul", createdBy: "lee", name: "최고객" });
    await setDoc(doc(db, "deals", "deal_web"),
      { teamId: "team_seoul", createdBy: "lee", ownerUid: "lee",
        title: "웹사이트 리뉴얼", amount: 12000000, stage: "proposal" });
  });
});

describe("공통: 팀 경계", () => {
  test("비로그인 사용자는 읽을 수 없다", async () => {
    await assertFails(getDoc(doc(anon(), "contacts", "ct_choi")));
  });
  test("다른 팀은 읽을 수 없다", async () => {
    await assertFails(getDoc(doc(as("other"), "contacts", "ct_choi")));
  });
  test("teamId 조건이 있는 목록 쿼리는 허용된다", async () => {
    const q = query(collection(as("park"), "deals"),
      where("teamId", "==", "team_seoul"));
    await assertSucceeds(getDocs(q));
  });
  test("teamId 조건이 없는 목록 쿼리는 거부된다", async () => {
    await assertFails(getDocs(collection(as("park"), "deals")));
  });
});

describe("열람자", () => {
  test("팀 데이터를 읽을 수 있다", async () => {
    await assertSucceeds(getDoc(doc(as("park"), "deals", "deal_web")));
  });
  test("연락처를 만들 수 없다", async () => {
    await assertFails(setDoc(doc(as("park"), "contacts", "ct_new"),
      { teamId: "team_seoul", createdBy: "park", name: "신규" }));
  });
  test("딜을 수정할 수 없다", async () => {
    await assertFails(updateDoc(doc(as("park"), "deals", "deal_web"),
      { amount: 1 }));
  });
});

describe("영업", () => {
  test("연락처를 만들고 다른 영업의 연락처도 수정할 수 있다", async () => {
    await assertSucceeds(setDoc(doc(as("jung"), "contacts", "ct_new"),
      { teamId: "team_seoul", createdBy: "jung", name: "신규" }));
    await assertSucceeds(updateDoc(doc(as("jung"), "contacts", "ct_choi"),
      { name: "최고객(수정)" }));
  });
  test("다른 사람이 만든 연락처는 삭제할 수 없다", async () => {
    await assertFails(deleteDoc(doc(as("jung"), "contacts", "ct_choi")));
  });
  test("본인 담당 딜은 수정할 수 있다", async () => {
    await assertSucceeds(updateDoc(doc(as("lee"), "deals", "deal_web"),
      { stage: "negotiation" }));
  });
  test("다른 사람이 담당인 딜은 수정·삭제할 수 없다", async () => {
    await assertFails(updateDoc(doc(as("jung"), "deals", "deal_web"),
      { stage: "won" }));
    await assertFails(deleteDoc(doc(as("jung"), "deals", "deal_web")));
  });
  test("본인 담당 딜이라도 담당자를 바꿀 수 없다", async () => {
    await assertFails(updateDoc(doc(as("lee"), "deals", "deal_web"),
      { ownerUid: "jung" }));
  });
  test("딜을 만들 때 다른 사람을 담당자로 지정할 수 없다", async () => {
    await assertSucceeds(setDoc(doc(as("jung"), "deals", "deal_mine"),
      { teamId: "team_seoul", createdBy: "jung", ownerUid: "jung" }));
    await assertFails(setDoc(doc(as("jung"), "deals", "deal_theirs"),
      { teamId: "team_seoul", createdBy: "jung", ownerUid: "lee" }));
  });
  test("자기 역할이나 팀을 바꿀 수 없다", async () => {
    await assertFails(updateDoc(doc(as("lee"), "users", "lee"),
      { role: "admin" }));
  });
});

describe("관리자", () => {
  test("딜 담당자를 같은 팀 영업에게 넘길 수 있다", async () => {
    await assertSucceeds(updateDoc(doc(as("kim"), "deals", "deal_web"),
      { ownerUid: "jung" }));
  });
  test("열람자나 다른 팀 사람은 담당자로 지정할 수 없다", async () => {
    await assertFails(updateDoc(doc(as("kim"), "deals", "deal_web"),
      { ownerUid: "park" }));
    await assertFails(updateDoc(doc(as("kim"), "deals", "deal_web"),
      { ownerUid: "other" }));
  });
  test("모든 딜과 연락처를 삭제할 수 있다", async () => {
    await assertSucceeds(deleteDoc(doc(as("kim"), "deals", "deal_web")));
    await assertSucceeds(deleteDoc(doc(as("kim"), "contacts", "ct_choi")));
  });
  test("팀원의 역할은 바꿀 수 있지만 본인 역할은 바꿀 수 없다", async () => {
    await assertSucceeds(updateDoc(doc(as("kim"), "users", "park"),
      { role: "sales" }));
    await assertFails(updateDoc(doc(as("kim"), "users", "kim"),
      { role: "viewer" }));
  });
  test("팀원을 다른 팀으로 옮길 수 없다", async () => {
    await assertFails(updateDoc(doc(as("kim"), "users", "lee"),
      { teamId: "team_busan" }));
  });
  test("다른 팀의 관리자는 우리 팀원 역할을 바꿀 수 없다", async () => {
    await assertFails(updateDoc(doc(as("other"), "users", "park"),
      { role: "admin" }));
  });
});
