/**
 * 백엔드 통신·화면 공용 타입.
 *
 * v1 이벤트는 docs/API_SPEC.md, v2(능동형 대응 루프) 이벤트는 docs/API_SPEC_V2.md 4절 기준.
 */

/** 백엔드 위험 등급 표기 */
export type BackendRiskLevel = 'low' | 'medium' | 'high';

/** 통화 로그 (call_started.call) */
export interface CallLog {
  id: number;
  device_id: number | null;
  name: string;
  file_type?: string;
  status: string;
  risk_score: number;
  risk_level: BackendRiskLevel;
  detected_label: number;
  phishing_type?: string;
  core_evidence: string;
  created_at: string;
  updated_at: string;
}

/** 화면에 그리는 발화 1건 */
export interface TranscriptTurn {
  turnIndex: number;
  role: string;
  isMine: boolean;
  content: string;
  atSec: number;
  riskScore?: number;
  keywords?: string[];
  // 마이크 통화: 이 말풍선에 합쳐진 서버 발화 번호(turn_index). 추적기의 발화별 태그를 찾을 때 쓴다.
  // 대본(텍스트) 통화는 turnIndex가 곧 서버 번호라 비워 둔다.
  sourceTurns?: number[];
}

/**
 * 화자 번호가 어느 쪽 말풍선인지. other=상대방(왼쪽), me=나(오른쪽), unknown=아직 모름(가운데).
 * 서버가 Claude 추적기의 발화별 역할과 겹친 시간으로 정한다(transcript_refined.speaker_sides).
 */
export type SpeakerSide = 'other' | 'me' | 'unknown';

/**
 * 실시간 조각 하나(말풍선으로 합치기 전 원본).
 * 정리본이 붙기 시작하면 정리본이 끝난 시각 이후의 조각만 '받아 적는 중'으로 정리본 뒤에 보인다.
 * 그래서 합친 말풍선(TranscriptTurn)이 아니라 조각 단위 시각이 필요하다.
 */
export interface LiveChunk {
  text: string;
  tStart: number; // 통화 시작부터 초(서버가 받은 오디오 누적 길이 기준)
  tEnd: number;
  keywords: string[];
}

/** 다시 받아 적어 화자를 나눈 발화 하나(transcript_refined.segments를 화면용으로 바꾼 것) */
export interface RefinedTurn {
  id: string; // "<log_id>-<n>". relabels가 이 id로 화자를 고친다.
  tStart: number;
  tEnd: number;
  speaker: number; // 통화 전체에서 같은 사람은 같은 번호
  text: string;
  keywords: string[];
}

/** 통화 중 정리본(뒤따라 다시 받아 적은 전사) 누적 상태 */
export interface RefinedTranscript {
  turns: RefinedTurn[]; // tStart 순
  sides: Record<string, SpeakerSide>; // 화자 번호(문자열) → 쪽
  until: number; // 이 시각(초)보다 앞은 정리본이 전사다
}

/** 통화 종료 후 요약(로컬 구성) */
export interface CallResult {
  id: number;
  name: string;
  category: string;
  finalScore: number;
  matchedPatterns: string[];
  coreEvidence: string;
  keywords: string[];
  turns: TranscriptTurn[];
  source: 'realtime' | 'file';
  createdAt: string;
  durationSec: number;
}

/** 텍스트 발화 응답에 실려 오는 저장된 발화 */
export interface CallMessagePayload {
  id: number;
  log_id: number;
  turn_index: number;
  role?: string;
  content: string;
  created_at: string;
}

/** 오디오 청크 응답의 전사 조각 */
export interface TranscriptPayload {
  message_id?: number;
  turn_index?: number;
  role?: string;
  content?: string;
  start_time?: number;
  end_time?: number;
  // 통화 시작부터 초(서버가 이 통화에서 받은 오디오 누적 길이로 계산). start_time/end_time은 청크 안 기준이라 따로 둔다.
  t_start?: number;
  t_end?: number;
}

/** 다시 받아 적은 발화 1건 (transcript_refined.segments) */
export interface RefinedSegmentPayload {
  id: string; // "<log_id>-<n>"
  t_start: number; // 통화 시작부터 초
  t_end: number;
  speaker: number; // 0, 1, 2 … (통화 전체 기준 번호)
  text: string;
}

// ───────────────────────── v2: 능동형 대응 루프 ─────────────────────────

/** 사기 단계 코드 (stage_index 순) */
export type ScamStage =
  | 'none'
  | 'approach'
  | 'impersonation'
  | 'pressure'
  | 'isolation'
  | 'extraction'
  | 'payment';

export type AgentActionType =
  | 'warn'
  | 'verify_guide'
  | 'official_number'
  | 'guardian_alert'
  | 'block_guide'
  | 'evidence_preserve'
  | 'report_guide'
  | 'followup_check'
  | 'linked_case_alert'; // 통화 중 단서 즉시 대조: 같은 계좌·번호가 다른 의심 통화에 있음, 또는 위험한 주소

export type AgentActionStatus = 'delivered' | 'acknowledged' | 'dismissed' | 'answered' | 'expired' | 'failed';

export interface FollowupOption {
  answer: string;
  label: string;
}

/** action_type별로 채워지는 필드가 다르다 (API_SPEC_V2 4.2) */
export interface AgentActionPayload {
  level?: 'caution' | 'danger';
  repeat?: boolean;
  questions?: string[];
  org?: string;
  phone?: string;
  guardian_count?: number;
  kind?: 'money' | 'info' | 'app';
  steps?: string[];
  phones?: string[];
  report_url?: string;
  phase?: 'in_call' | 'post_call';
  question?: string;
  options?: FollowupOption[];
  due_in_sec?: number;
}

/** 에이전트가 결정한 행동 1건 */
export interface AgentAction {
  id: number;
  log_id: number;
  action_type: AgentActionType;
  title: string;
  message: string;
  reason: string;
  payload: AgentActionPayload;
  status: AgentActionStatus;
  response: string;
  state_version: number;
  created_at: string;
  responded_at: string;
}

export interface UtteranceTag {
  message_id: number;
  turn_index: number;
  role: 'suspect' | 'user' | 'unknown';
  tactics: string[];
}

export interface CaseEntity {
  entity_type: string;
  value: string;
  turn_index: number | null;
}

/** 대화 상태 (state_update 이벤트 본문 · GET /calls/{id}/state) */
export interface ConversationState {
  log_id: number;
  state_version: number;
  stage: ScamStage;
  stage_index: number;
  scam_type: string;
  phishing_type: string;
  compliance: 'none' | 'hesitant' | 'complying' | 'completed';
  risk_score: number;
  risk_level: BackendRiskLevel;
  predicted_next: ScamStage | null;
  summary: string;
  utterance_tags: UtteranceTag[];
  entities: CaseEntity[];
}

/** GET /calls/{id}/timeline */
export interface CallTimeline {
  log_id: number;
  stages: { stage: ScamStage; stage_index: number; from_turn: number; state_version: number; at: string }[];
  messages: { message_id: number; turn_index: number; role: string; content: string; tactics: string[] }[];
  actions: AgentAction[];
}

// ───────────────────────── WebSocket 이벤트 ─────────────────────────

interface RiskFields {
  log_id: number;
  risk_score: number;
  risk_level: BackendRiskLevel;
  phishing_type?: string;
}

export type AnalysisEvent =
  | { type: 'call_started'; call: CallLog; audio_format?: string }
  | ({ type: 'analysis_ack'; is_phishing: false; message?: CallMessagePayload } & RiskFields)
  | ({
      type: 'phishing_detected';
      is_phishing: true;
      matched_patterns: string[];
      core_evidence: string;
      notification?: unknown;
      message?: CallMessagePayload;
    } & RiskFields)
  | ({ type: 'audio_analysis_ack'; chunk_index: number; transcripts?: TranscriptPayload[] } & RiskFields)
  | ({
      type: 'audio_phishing_detected';
      chunk_index: number;
      transcripts?: TranscriptPayload[];
      matched_patterns: string[];
      core_evidence: string;
      notification?: unknown;
    } & RiskFields)
  | { type: 'audio_chunk_ack'; log_id: number; chunk_index: number; message?: string }
  | { type: 'audio_chunk_error'; log_id?: number; chunk_index?: number; message: string }
  | { type: 'error'; message: string }
  | ({ type: 'state_update' } & ConversationState)
  | { type: 'agent_action'; log_id: number; action: AgentAction }
  | { type: 'call_ended'; log_id: number; final: Partial<ConversationState> }
  // 서버가 보내지 않고 앱(wsService)이 만드는 이벤트: 앱이 닫지 않았는데 연결이 끝났거나 제때 열리지 않았다.
  // opened=false면 한 번도 연결하지 못한 것, true면 연결됐다가 끊긴 것이다.
  | { type: 'connection_closed'; opened: boolean }
  // 정리본: 서버가 (기본) 15초마다 지난 구간을 화자 구분과 함께 다시 받아 적을 때마다 보낸다(STT_REFINE=off면 오지 않는다).
  | {
      type: 'transcript_refined';
      log_id: number;
      engine: string; // 지금은 'rtzr'
      refined_until: number; // 이 시각(초)까지 정리됐다
      segments: RefinedSegmentPayload[]; // 이번에 새로 정리된 발화만
      relabels: { id: string; speaker: number }[]; // 화자 번호가 바뀐 이전 발화
      speaker_sides: Record<string, SpeakerSide>; // 지금까지 나온 모든 화자 번호의 현재 쪽
    };
