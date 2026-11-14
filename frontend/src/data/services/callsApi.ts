/**
 * 통화 기록(히스토리) 백엔드 조회 API.
 *
 * 히스토리는 로컬이 아니라 백엔드가 진실 소스다.
 *  - 목록/카운트: GET /calls        (API_SPEC 4)
 *  - 상세:        GET /calls/{id}   (API_SPEC 5)
 *
 * 저장은 분석 시점에 이미 백엔드에서 이뤄진다
 * (POST /calls/analyze-audio → log_id 발급, 실시간 WS start → 통화 로그 생성).
 */
import { BASE_URL_HTTP } from '@/core/config/env';
import type { AgentAction, BackendRiskLevel, CallTimeline, ConversationState } from '@/data/models/types';

/** GET /calls 목록 항목 */
export interface CallListItem {
  id: number;
  called_at: string; // ISO로 정규화됨
  risk_score: number; // 0..1
  risk_level: BackendRiskLevel;
  phishing_type: string; // 정상 통화면 '정상'
  file_type: string; // 'realtime' | 'recording'
}

/** GET /calls 응답 */
export interface CallsResponse {
  risk_level_counts: { low: number; medium: number; high: number };
  calls: CallListItem[];
}

/** GET /calls/{id} 상세 (얇음: 전사/점수 없음) */
export interface CallDetail {
  id: number;
  phishing_type: string;
  matched_patterns: string[];
  core_evidence: string;
}

/** 백엔드 file_type → 앱 source */
export function callSource(fileType: string | undefined): 'file' | 'realtime' {
  return fileType === 'recording' ? 'file' : 'realtime';
}

/** "2026-07-09 10:20:31" → "2026-07-09T10:20:31" (Date 파싱 안정화) */
function normDate(s: string | undefined): string {
  if (!s) return '';
  return s.includes('T') ? s : s.replace(' ', 'T');
}

/**
 * 조회(GET) 요청을 네트워크 오류에 한해 잠깐 쉬었다가 다시 시도한다.
 * 터널을 거치는 배포 환경에서 통화 종료 직후 연결이 한 번 끊기는 경우가 있어 넣었다.
 */
async function getWithRetry(url: string, retries = 2): Promise<Response> {
  for (let attempt = 0; ; attempt++) {
    try {
      return await fetch(url);
    } catch (error) {
      if (attempt >= retries) throw error;
      await new Promise((resolve) => setTimeout(resolve, 600 * (attempt + 1)));
    }
  }
}

export async function fetchCalls(limit = 100): Promise<CallsResponse> {
  const res = await getWithRetry(`${BASE_URL_HTTP}/calls?limit=${limit}`);
  if (!res.ok) throw new Error(`통화 목록 조회 실패 (${res.status})`);
  const data = (await res.json()) as Partial<CallsResponse>;
  const calls = (Array.isArray(data.calls) ? data.calls : []).map((c) => ({
    ...c,
    called_at: normDate(c.called_at),
  })) as CallListItem[];
  return {
    risk_level_counts: data.risk_level_counts ?? { low: 0, medium: 0, high: 0 },
    calls,
  };
}

export async function fetchCallDetail(id: number): Promise<CallDetail> {
  const res = await getWithRetry(`${BASE_URL_HTTP}/calls/${id}`);
  if (!res.ok) throw new Error(`통화 상세 조회 실패 (${res.status})`);
  const data = (await res.json()) as Partial<CallDetail>;
  return {
    id: data.id ?? id,
    phishing_type: data.phishing_type ?? '',
    matched_patterns: Array.isArray(data.matched_patterns) ? data.matched_patterns : [],
    core_evidence: data.core_evidence ?? '',
  };
}

// ───────────────────────── v2: 능동형 대응 루프 (API_SPEC_V2 5절) ─────────────────────────

/** 단계 전환·태그된 발화·에이전트 행동 타임라인 */
/** GET /calls/{id}/state — 최신 대화 상태(단계·유형·순응도·단서). 발표용 분석 패널이 쓴다. */
export async function fetchCallState(id: number): Promise<ConversationState> {
  const res = await getWithRetry(`${BASE_URL_HTTP}/calls/${id}/state`, 0);
  if (!res.ok) throw new Error(`통화 상태를 불러오지 못했습니다. (${res.status})`);
  return (await res.json()) as ConversationState;
}

export async function fetchTimeline(id: number): Promise<CallTimeline> {
  const res = await getWithRetry(`${BASE_URL_HTTP}/calls/${id}/timeline`);
  if (!res.ok) throw new Error(`타임라인 조회 실패 (${res.status})`);
  const data = (await res.json()) as Partial<CallTimeline>;
  return {
    log_id: data.log_id ?? id,
    stages: Array.isArray(data.stages) ? data.stages : [],
    messages: Array.isArray(data.messages) ? data.messages : [],
    actions: Array.isArray(data.actions) ? data.actions : [],
  };
}

/** 행동 카드 반응 또는 후속 확인 답. 갱신된 행동과 이어지는 새 행동을 돌려준다. */
export async function respondToAction(
  logId: number,
  actionId: number,
  body: { response: 'acknowledged' | 'dismissed' | 'answered'; answer?: string },
): Promise<{ action: AgentAction; next_actions: AgentAction[] }> {
  const res = await fetch(`${BASE_URL_HTTP}/calls/${logId}/actions/${actionId}/respond`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...body, source: 'app' }),
  });
  if (!res.ok) throw new Error(`응답 전송 실패 (${res.status})`);
  return (await res.json()) as { action: AgentAction; next_actions: AgentAction[] };
}

export type FeedbackVerdict = 'false_positive' | 'confirmed_phishing' | 'missed_phishing' | 'unsure';

/** 통화 판정에 대한 피드백(오탐 신고 등) */
export async function sendFeedback(logId: number, verdict: FeedbackVerdict, comment = ''): Promise<void> {
  const res = await fetch(`${BASE_URL_HTTP}/calls/${logId}/feedback`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ verdict, comment }),
  });
  if (!res.ok) throw new Error(`피드백 전송 실패 (${res.status})`);
}
