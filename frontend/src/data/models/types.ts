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
  | { type: 'call_ended'; log_id: number; final: Partial<ConversationState> };
