import { create } from 'zustand';
import type { TranscriptTurn } from '@/data/models/types';
import { loadJson, saveJson, STORAGE_KEYS } from '@/data/services/storage';

/**
 * 통화별 전사(대화 전문) 로컬 저장소.
 *
 * 통화 종료 시 받은 전사를 logId별로 로컬(AsyncStorage)에 저장해 결과 화면에서 다시 볼 수 있게 한다.
 * 결과 화면은 서버 발화(GET /calls/{id}/timeline)를 먼저 쓰고, 이 저장분은 서버 발화가 없을 때 쓴다.
 * 빈 배열도 저장한다: 이 기기에서 끝낸 통화인데 받아 적은 말이 없었다는 뜻이다(히스토리에 '들은 말 없음'으로 보인다).
 */
interface TranscriptState {
  byLog: Record<number, TranscriptTurn[]>;
  hydrated: boolean;
  hydrate: () => Promise<void>;
  save: (logId: number, turns: TranscriptTurn[]) => void;
  /** 받아 적은 말이 없다고 기억한 통화인데 서버에 발화가 있으면 그 표시를 지운다. */
  clearEmpty: (logId: number) => void;
}

export const useTranscriptStore = create<TranscriptState>((set, get) => {
  // 디스크에 있던 통화를 먼저 합친 뒤 저장한다(불러오기 전에 저장하면 예전 통화가 덮어써져 지워진다).
  const persist = async () => {
    await get().hydrate();
    await saveJson(STORAGE_KEYS.transcripts, get().byLog);
  };

  return {
    byLog: {},
    hydrated: false,
    hydrate: async () => {
      if (get().hydrated) return;
      const saved = await loadJson<Record<number, TranscriptTurn[]>>(STORAGE_KEYS.transcripts);
      // 이미 메모리에 저장된 항목(이번 세션에서 방금 저장한 것)이 디스크보다 우선.
      set({ byLog: { ...(saved ?? {}), ...get().byLog }, hydrated: true });
    },
    save: (logId, turns) => {
      if (!logId) return;
      set({ byLog: { ...get().byLog, [logId]: turns } });
      void persist();
    },
    clearEmpty: (logId) => {
      void (async () => {
        await get().hydrate();
        if (get().byLog[logId]?.length !== 0) return;
        const byLog = { ...get().byLog };
        delete byLog[logId];
        set({ byLog });
        await saveJson(STORAGE_KEYS.transcripts, byLog);
      })();
    },
  };
});

/**
 * 받아 적은 말이 없어 위험도를 매기지 않은 통화인지.
 * - 서버 발화 수(serverMessageCount)를 알면 그것을 따른다(0개면 빈 통화). 모르면(null) 이 기기에 남긴 빈 통화 표시([])를 쓴다.
 * - 이 기기에 대화가 있거나 서버 점수가 0보다 크면 무언가를 분석한 것이므로 빈 통화가 아니다.
 */
export function isUnheardResult(
  localTurns: TranscriptTurn[] | undefined,
  riskScore: number,
  serverMessageCount: number | null,
): boolean {
  if ((localTurns?.length ?? 0) > 0 || riskScore !== 0) return false;
  return serverMessageCount !== null ? serverMessageCount === 0 : localTurns !== undefined;
}

/** 히스토리·홈 목록용: 서버 발화 수를 모르므로 이 기기의 기록으로 가린다. */
export function isUnheardCall(byLog: Record<number, TranscriptTurn[]>, logId: number, riskScore: number): boolean {
  return isUnheardResult(byLog[logId], riskScore, null);
}
