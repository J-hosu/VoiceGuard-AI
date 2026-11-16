/**
 * 발표용 "AI 분석 과정" 패널 (웹 넓은 화면 전용).
 *
 * 휴대폰 폭의 앱 화면 옆에 붙어, 통화가 진행되는 동안 서버가 무엇을 판단하고 있는지를 보여준다:
 * 위험도 추이, 사기 단계, 발화별 수법 태그, 추출된 단서, AI 대응. 앱 화면과 달리 사용자를 위한
 * 화면이 아니라 청중에게 내부 동작을 설명하기 위한 화면이다.
 */
import { useEffect, useRef, useState } from 'react';
import { ScrollView, View } from 'react-native';
import { AppText } from '@/components/AppText';
import { DARK_AGENT_PALETTE, StageStepper, TACTIC_LABELS, stageLabel } from '@/components/agentCards';
import type { CallTimeline, ConversationState } from '@/data/models/types';
import { fetchCallState, fetchTimeline } from '@/data/services/callsApi';
import { useDemoStore } from '@/state/demoStore';

const P = DARK_AGENT_PALETTE;
const PANEL_BG = '#10141c';
const CARD_BG = '#171c26';
const BORDER = 'rgba(255,255,255,0.08)';
const POLL_MS = 2000;
const HISTORY_MAX = 48;

const COMPLIANCE_LABELS: Record<string, string> = {
  none: '따르는 정황 없음',
  hesitant: '망설이는 중',
  complying: '요구에 응하는 중',
  completed: '이미 따랐다고 말함',
};
const ENTITY_LABELS: Record<string, string> = {
  account: '계좌번호',
  phone: '전화번호',
  url: '웹 주소',
  org: '기관·회사',
  person: '이름·직함',
  amount: '요구 금액',
  app: '설치 요구 앱',
  case_number: '사건번호',
};
const ROLE_LABELS: Record<string, string> = { suspect: '상대방', user: '사용자', unknown: '미상' };
const PIPELINE = ['음성', 'Whisper 전사', '분류기 + Claude 상태 추적', '위험도 융합', '대응 에이전트'];

function riskColor(score: number): string {
  if (score >= 0.85) return P.danger;
  if (score >= 0.7) return P.warning;
  return P.safe;
}

function Card({ title, right, children, flex }: { title: string; right?: string; children: React.ReactNode; flex?: number }) {
  return (
    <View
      style={{ backgroundColor: CARD_BG, borderRadius: 16, borderWidth: 1, borderColor: BORDER, padding: 16, gap: 12, flex }}
    >
      <View style={{ flexDirection: 'row', alignItems: 'baseline', justifyContent: 'space-between' }}>
        <AppText weight="800" color={P.text} style={{ fontSize: 14 }}>
          {title}
        </AppText>
        {right ? (
          <AppText weight="600" color={P.muted} style={{ fontSize: 12 }}>
            {right}
          </AppText>
        ) : null}
      </View>
      {children}
    </View>
  );
}

function Chip({ label, color }: { label: string; color: string }) {
  return (
    <View style={{ paddingHorizontal: 8, paddingVertical: 3, borderRadius: 999, backgroundColor: `${color}22` }}>
      <AppText weight="700" color={color} style={{ fontSize: 11 }}>
        {label}
      </AppText>
    </View>
  );
}

export function DemoPanel() {
  const logId = useDemoStore((s) => s.logId);
  const running = useDemoStore((s) => s.running);
  const halted = useDemoStore((s) => s.halted);
  const [state, setState] = useState<ConversationState | null>(null);
  const [timeline, setTimeline] = useState<CallTimeline | null>(null);
  const [history, setHistory] = useState<number[]>([]);
  const transcriptRef = useRef<ScrollView>(null);

  // 통화가 바뀌면 이전 통화의 표시를 지운다.
  useEffect(() => {
    setState(null);
    setTimeline(null);
    setHistory([]);
  }, [logId]);

  // 통화 중에는 주기적으로, 끝난 뒤에는 한 번 더 조회해 최종 상태(재전사 결과)를 반영한다.
  useEffect(() => {
    if (logId <= 0) return;
    let alive = true;
    const load = async () => {
      try {
        const [nextState, nextTimeline] = await Promise.all([fetchCallState(logId), fetchTimeline(logId)]);
        if (!alive) return;
        setState(nextState);
        setTimeline(nextTimeline);
        setHistory((prev) => [...prev, nextState.risk_score].slice(-HISTORY_MAX));
      } catch {
        // 일시적 조회 실패는 다음 주기에 다시 시도한다.
      }
    };
    void load();
    const timer = setInterval(load, running ? POLL_MS : POLL_MS * 4);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [logId, running]);

  const messageCount = timeline?.messages.length ?? 0;
  useEffect(() => {
    transcriptRef.current?.scrollToEnd({ animated: true });
  }, [messageCount]);

  const score = state?.risk_score ?? 0;
  const color = riskColor(score);
  const actions = (timeline?.actions ?? []).filter((a) => a.action_type !== 'followup_check');

  return (
    <View style={{ flex: 1, backgroundColor: PANEL_BG, padding: 20, gap: 14 }}>
      <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' }}>
        <View style={{ gap: 2 }}>
          <AppText weight="800" color={P.text} style={{ fontSize: 20 }}>
            AI 분석 과정
          </AppText>
          <AppText color={P.muted} style={{ fontSize: 12.5 }}>
            통화가 진행되는 동안 서버가 판단하는 내용을 그대로 보여줍니다.
          </AppText>
        </View>
        <View style={{ flexDirection: 'row', alignItems: 'center', gap: 7 }}>
          <View
            style={{ width: 8, height: 8, borderRadius: 4, backgroundColor: halted ? P.warning : running ? P.safe : P.muted }}
          />
          <AppText weight="700" color={halted ? P.warning : running ? P.safe : P.muted} style={{ fontSize: 12.5 }}>
            {halted ? '분석 멈춤' : running ? '분석 중' : logId > 0 ? '통화 종료' : '대기 중'}
          </AppText>
        </View>
      </View>

      <View style={{ flexDirection: 'row', alignItems: 'center', flexWrap: 'wrap', gap: 6 }}>
        {PIPELINE.map((step, i) => (
          <View key={step} style={{ flexDirection: 'row', alignItems: 'center', gap: 6 }}>
            <View style={{ paddingHorizontal: 9, paddingVertical: 4, borderRadius: 8, borderWidth: 1, borderColor: BORDER }}>
              <AppText weight="600" color={P.sub} style={{ fontSize: 11.5 }}>
                {step}
              </AppText>
            </View>
            {i < PIPELINE.length - 1 ? (
              <AppText color={P.muted} style={{ fontSize: 12 }}>
                →
              </AppText>
            ) : null}
          </View>
        ))}
      </View>

      {logId <= 0 ? (
        <View style={{ flex: 1, alignItems: 'center', justifyContent: 'center', gap: 8 }}>
          <AppText weight="700" color={P.sub} style={{ fontSize: 16 }}>
            왼쪽 화면에서 분석을 시작해 보세요
          </AppText>
          <AppText color={P.muted} style={{ fontSize: 13 }}>
            위험도 추이, 사기 단계, 발화별 수법, 추출된 단서가 여기에 실시간으로 표시됩니다.
          </AppText>
        </View>
      ) : (
        <>
          <View style={{ flexDirection: 'row', gap: 14 }}>
            <Card title="위험도" right={state ? `상태 v${state.state_version}` : undefined} flex={1}>
              <View style={{ flexDirection: 'row', alignItems: 'flex-end', gap: 14 }}>
                <AppText weight="800" color={color} style={{ fontSize: 44, lineHeight: 48 }}>
                  {Math.round(score * 100)}
                  <AppText weight="700" color={color} style={{ fontSize: 20 }}>
                    %
                  </AppText>
                </AppText>
                <View style={{ flex: 1, height: 44, flexDirection: 'row', alignItems: 'flex-end', gap: 2 }}>
                  {history.map((value, i) => (
                    <View
                      key={i}
                      style={{
                        flex: 1,
                        maxWidth: 10,
                        height: Math.max(2, Math.round(value * 44)),
                        borderRadius: 2,
                        backgroundColor: riskColor(value),
                        opacity: 0.35 + 0.65 * ((i + 1) / history.length),
                      }}
                    />
                  ))}
                </View>
              </View>
              <AppText color={P.muted} style={{ fontSize: 11.5 }}>
                분류기 점수 30% + Claude 추적기 점수 70% · 70% 이상 주의, 85% 이상 강한 경고
              </AppText>
            </Card>
            <Card title="판단" flex={1}>
              <View style={{ gap: 8 }}>
                <Row label="사기 유형" value={state?.phishing_type || '판단 중'} />
                <Row label="도달 단계" value={stageLabel(state?.stage)} />
                <Row label="다음 예상" value={state?.predicted_next ? stageLabel(state.predicted_next) : '-'} />
                <Row label="사용자 순응도" value={COMPLIANCE_LABELS[state?.compliance ?? 'none']} />
              </View>
            </Card>
          </View>

          <Card title="사기 진행 단계">
            <StageStepper
              stageIndex={state?.stage_index ?? 0}
              predictedNext={state?.predicted_next}
              color={color}
              palette={P}
            />
            {state?.summary ? (
              <AppText color={P.sub} style={{ fontSize: 13.5, lineHeight: 21 }}>
                {state.summary}
              </AppText>
            ) : null}
          </Card>

          <View style={{ flex: 1, flexDirection: 'row', gap: 14, minHeight: 0 }}>
            <Card title="발화별 분석" right={`${messageCount}개 발화`} flex={1.5}>
              <ScrollView ref={transcriptRef} style={{ flex: 1 }} contentContainerStyle={{ gap: 9, paddingBottom: 4 }}>
                {(timeline?.messages ?? []).map((message) => {
                  const mine = message.role === 'user';
                  return (
                    <View key={message.message_id} style={{ gap: 4 }}>
                      <View style={{ flexDirection: 'row', gap: 8 }}>
                        <AppText
                          weight="700"
                          color={mine ? P.accent : message.role === 'suspect' ? P.warning : P.muted}
                          style={{ fontSize: 11.5, width: 42, marginTop: 2 }}
                        >
                          {ROLE_LABELS[message.role] ?? '미상'}
                        </AppText>
                        <AppText color={P.text} style={{ fontSize: 13.5, lineHeight: 20, flex: 1 }}>
                          {message.content}
                        </AppText>
                      </View>
                      {message.tactics.length > 0 ? (
                        <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 5, marginLeft: 50 }}>
                          {message.tactics.map((tactic) => (
                            <Chip
                              key={tactic}
                              label={TACTIC_LABELS[tactic]?.label ?? tactic}
                              color={TACTIC_LABELS[tactic]?.safe ? P.safe : P.danger}
                            />
                          ))}
                        </View>
                      ) : null}
                    </View>
                  );
                })}
                {messageCount === 0 ? (
                  <AppText color={P.muted} style={{ fontSize: 13 }}>
                    아직 전사된 발화가 없습니다.
                  </AppText>
                ) : null}
              </ScrollView>
            </Card>

            <View style={{ flex: 1, gap: 14, minHeight: 0 }}>
              <Card title="추출된 단서" right={`${state?.entities.length ?? 0}개`} flex={1}>
                <ScrollView style={{ flex: 1 }} contentContainerStyle={{ gap: 8 }}>
                  {(state?.entities ?? []).map((entity, i) => (
                    <View key={`${entity.entity_type}-${i}`} style={{ gap: 1 }}>
                      <AppText weight="600" color={P.muted} style={{ fontSize: 11 }}>
                        {ENTITY_LABELS[entity.entity_type] ?? entity.entity_type}
                      </AppText>
                      <AppText weight="700" color={P.text} style={{ fontSize: 13.5 }}>
                        {entity.value}
                      </AppText>
                    </View>
                  ))}
                  {(state?.entities.length ?? 0) === 0 ? (
                    <AppText color={P.muted} style={{ fontSize: 13 }}>
                      계좌·기관명·사건번호 등이 나오면 여기에 모입니다.
                    </AppText>
                  ) : null}
                </ScrollView>
              </Card>
              <Card title="AI 대응" right={`${actions.length}건`} flex={1}>
                <ScrollView style={{ flex: 1 }} contentContainerStyle={{ gap: 8 }}>
                  {actions.map((action) => (
                    <View key={action.id} style={{ gap: 1 }}>
                      <AppText weight="700" color={P.text} style={{ fontSize: 13 }}>
                        {action.title}
                      </AppText>
                      {action.reason ? (
                        <AppText color={P.muted} style={{ fontSize: 11.5, lineHeight: 17 }}>
                          {action.reason}
                        </AppText>
                      ) : null}
                    </View>
                  ))}
                  {actions.length === 0 ? (
                    <AppText color={P.muted} style={{ fontSize: 13 }}>
                      위험도가 주의 기준을 넘으면 경고와 확인 방법을 냅니다.
                    </AppText>
                  ) : null}
                </ScrollView>
              </Card>
            </View>
          </View>
        </>
      )}
    </View>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <View style={{ flexDirection: 'row', justifyContent: 'space-between', gap: 12 }}>
      <AppText color={P.muted} style={{ fontSize: 12.5 }}>
        {label}
      </AppText>
      <AppText weight="700" color={P.text} style={{ fontSize: 13, flexShrink: 1, textAlign: 'right' }}>
        {value}
      </AppText>
    </View>
  );
}
